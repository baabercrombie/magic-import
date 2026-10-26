"""The main workflow: email/file in -> AI -> duplicate check -> calendar."""
import hashlib
import mimetypes
import os
import re
import threading
from datetime import datetime, timedelta

from . import ai, calendar_client, config, db, notify
from .mailparse import Attachment, parse_email

# One thing at a time, so two emails can't create the same event at the same moment
work_lock = threading.RLock()


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "file"


def _note(imp: dict) -> str:
    what = imp.get("subject") or "an upload"
    return f"Added by Magic Import from: {what}"


# ---------- intake ----------

def sender_allowed(from_addr: str) -> bool:
    if not config.ALLOWED_SENDERS:
        return True
    from_addr = (from_addr or "").lower()
    for allowed in config.ALLOWED_SENDERS:
        if allowed.startswith("@"):
            if from_addr.endswith(allowed):
                return True
        elif from_addr == allowed:
            return True
    return False


def process_email(raw: bytes) -> int | None:
    """Handle one raw email from the mailbox. Returns the import id (or None if already seen)."""
    parsed = parse_email(raw)
    if not parsed.message_id:
        parsed.message_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    if db.message_already_seen(parsed.message_id):
        return None

    names = [a.filename for a in parsed.attachments] + [f"skipped: {s}" for s in parsed.skipped]
    received = parsed.date_iso or db.now_iso()

    if not sender_allowed(parsed.from_addr):
        import_id = db.create_import("email", parsed.message_id, parsed.from_addr, parsed.subject,
                                     received, parsed.body_text[:20000], names, None, status="ignored")
        db.update_import(import_id, processed_at=db.now_iso(),
                         error=f"Sender {parsed.from_addr} is not in ALLOWED_SENDERS, so it was ignored.")
        db.log("ignored", f"Email from {parsed.from_addr} ignored (not an allowed sender)", import_id)
        return import_id

    import_id = db.create_import("email", parsed.message_id, parsed.from_addr, parsed.subject,
                                 received, parsed.body_text[:20000], names, None)
    raw_path = os.path.join(config.FILES_DIR, f"{import_id}.eml")
    with open(raw_path, "wb") as fh:
        fh.write(raw)
    db.update_import(import_id, raw_path=raw_path)
    db.log("received", f"Email received: {parsed.subject}", import_id)

    run_extraction(import_id, parsed.subject, parsed.from_addr, parsed.body_text, parsed.attachments)
    return import_id


def process_upload(filename: str, data: bytes, extra_text: str = "") -> int:
    """A PDF / image / text file uploaded on the web page."""
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    import_id = db.create_import("upload", None, "web upload", filename, db.now_iso(),
                                 extra_text[:20000], [filename], None)
    raw_path = os.path.join(config.FILES_DIR, f"{import_id}_{_safe_name(filename)}")
    with open(raw_path, "wb") as fh:
        fh.write(data)
    db.update_import(import_id, raw_path=raw_path)
    db.log("received", f"File uploaded: {filename}", import_id)

    run_extraction(import_id, filename, "web upload", extra_text, [_attachment_from_file(raw_path, filename, content_type)])
    return import_id


def process_text(text: str, title: str = "") -> int:
    """Text pasted on the web page."""
    subject = title.strip() or "Pasted text"
    import_id = db.create_import("text", None, "web paste", subject, db.now_iso(), text[:20000], [], None)
    db.log("received", f"Text pasted: {subject}", import_id)
    run_extraction(import_id, subject, "web paste", text, [])
    return import_id


def _attachment_from_file(path: str, filename: str, content_type: str) -> Attachment:
    with open(path, "rb") as fh:
        data = fh.read()
    if content_type.startswith("text/"):
        content_type = "text/plain"
    return Attachment(filename, content_type, data)


# ---------- AI + calendar ----------

def run_extraction(import_id: int, subject: str, from_addr: str, body_text: str,
                   attachments: list[Attachment]) -> None:
    with work_lock:
        db.update_import(import_id, status="processing", error=None)
        try:
            result = ai.extract_events(subject, from_addr, body_text, attachments)
        except Exception as exc:
            db.update_import(import_id, status="error", error=f"AI step failed: {exc}", processed_at=db.now_iso())
            db.log("error", f"AI step failed: {exc}", import_id)
            notify.send_import_update(import_id)
            return

        db.update_import(import_id, ai_summary=result["summary"])
        db.log("ai", f"AI found {len(result['events'])} event(s)", import_id)

        review = db.review_mode()
        imp = db.get_import(import_id)
        problems = []
        already_created = [row for row in db.events_for_import(import_id) if row["status"] == "created"]
        for ev in result["events"]:
            # When reading an email again, don't list events it already put on the calendar
            if _matches_any(ev, already_created):
                continue
            event_id = db.create_event(import_id, ev, status="pending")
            try:
                dup = find_duplicate(ev, exclude_event_id=event_id)
            except Exception as exc:
                dup = None
                problems.append(f"Could not check calendar for duplicates: {exc}")
                db.update_event(event_id, error=f"Duplicate check failed: {exc}")
            if dup:
                db.update_event(event_id, status="duplicate", duplicate_of=dup)
                db.log("duplicate", f'Skipped "{ev["title"]}" - already on calendar as {dup}', import_id, event_id)
                continue
            if review or problems:
                db.log("pending", f'"{ev["title"]}" is waiting for your approval', import_id, event_id)
                continue
            _create_on_calendar(event_id, imp)

        error = "; ".join(sorted(set(problems))) or None
        if problems:
            error += " (events were left for review instead of being created)"
        db.update_import(import_id, status="done", processed_at=db.now_iso(), error=error)
    notify.send_import_update(import_id)


def _matches_any(ev: dict, rows: list[dict]) -> dict | None:
    start, end = calendar_client.event_range_local(ev)
    for other in rows:
        o_start, o_end = calendar_client.event_range_local({**other, "all_day": bool(other["all_day"])})
        existing = {"title": other["title"], "start": o_start, "end": o_end, "all_day": bool(other["all_day"])}
        if calendar_client.is_same_event(ev, start, end, existing):
            return other
    return None


def find_duplicate(ev: dict, exclude_event_id: int | None = None) -> str | None:
    # 1) other events this app is holding for review (not on the calendar yet)
    pending = db.query("SELECT * FROM events WHERE status = 'pending' AND id != ?", (exclude_event_id or -1,))
    match = _matches_any(ev, pending)
    if match:
        return f'"{match["title"]}" (already waiting for review)'
    # 2) the real calendar
    if not calendar_client.is_configured():
        return None
    return calendar_client.find_duplicate(ev)


def _event_dict(row: dict) -> dict:
    return {
        "title": row["title"], "start": row["start"], "end": row["end"],
        "all_day": bool(row["all_day"]), "location": row["location"], "description": row["description"],
    }


def _create_on_calendar(event_id: int, imp: dict) -> bool:
    row = db.get_event(event_id)
    try:
        url, uid = calendar_client.create_event(_event_dict(row), _note(imp))
    except Exception as exc:
        db.update_event(event_id, status="failed", error=str(exc))
        db.log("error", f'Could not create "{row["title"]}": {exc}', row["import_id"], event_id)
        return False
    db.update_event(event_id, status="created", calendar_url=url, uid=uid, error=None, duplicate_of=None)
    db.log("created", f'Created "{row["title"]}" ({_when(row)})', row["import_id"], event_id)
    return True


def _when(row: dict) -> str:
    if row["all_day"]:
        if row["end"] and row["end"] != row["start"]:
            return f"{row['start']} to {row['end']}, all day"
        return f"{row['start']}, all day"
    return row["start"].replace("T", " ")


# ---------- actions from the web page ----------

def approve_event(event_id: int) -> str | None:
    """Put a pending / duplicate / skipped / deleted event on the calendar. Returns an error or None."""
    with work_lock:
        row = db.get_event(event_id)
        if not row:
            return "Event not found"
        if row["status"] == "created":
            return None
        imp = db.get_import(row["import_id"])
        if _create_on_calendar(event_id, imp):
            return None
        return db.get_event(event_id)["error"]


def remove_event(event_id: int) -> str | None:
    """Undo: delete from the calendar (if it was created) or just skip it."""
    with work_lock:
        row = db.get_event(event_id)
        if not row:
            return "Event not found"
        if row["status"] == "created" and row["calendar_url"]:
            try:
                calendar_client.delete_event(row["calendar_url"])
            except Exception as exc:
                db.log("error", f'Could not delete "{row["title"]}": {exc}', row["import_id"], event_id)
                return f"Could not delete from calendar: {exc}"
            db.update_event(event_id, status="deleted", calendar_url=None)
            db.log("deleted", f'Removed "{row["title"]}" from the calendar', row["import_id"], event_id)
        elif row["status"] in ("pending", "duplicate", "failed"):
            db.update_event(event_id, status="skipped")
            db.log("skipped", f'Skipped "{row["title"]}"', row["import_id"], event_id)
        return None


def edit_event(event_id: int, fields: dict) -> str | None:
    """Change an event. If it is already on the calendar, the calendar is updated too."""
    with work_lock:
        row = db.get_event(event_id)
        if not row:
            return "Event not found"
        try:
            new = _validated(fields)
        except ValueError as exc:
            return str(exc)

        if row["status"] == "created" and row["calendar_url"]:
            imp = db.get_import(row["import_id"])
            try:
                calendar_client.update_event(row["calendar_url"], row["uid"], new, _note(imp))
            except Exception as exc:
                return f"Could not update the calendar: {exc}"

        db.update_event(event_id, title=new["title"], start=new["start"], end=new["end"],
                        all_day=1 if new["all_day"] else 0, location=new["location"],
                        description=new["description"])
        db.log("edited", f'Edited "{new["title"]}" ({_when({**new, "all_day": new["all_day"]})})',
               row["import_id"], event_id)
        return None


def add_manual_event(import_id: int, fields: dict) -> str | None:
    """Add an event the AI missed to an import."""
    try:
        new = _validated(fields)
    except ValueError as exc:
        return str(exc)
    event_id = db.create_event(import_id, new, status="pending")
    db.log("added", f'Added "{new["title"]}" by hand', import_id, event_id)
    if not db.review_mode():
        return approve_event(event_id)
    return None


def _validated(fields: dict) -> dict:
    title = (fields.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required")
    all_day = str(fields.get("all_day", "")).lower() in ("1", "on", "true", "yes")
    start_date = (fields.get("start_date") or "").strip()
    end_date = (fields.get("end_date") or "").strip() or start_date
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Dates must look like 2026-10-31")
    if ed < sd:
        raise ValueError("End date is before start date")

    new = {
        "title": title[:200],
        "location": (fields.get("location") or "").strip()[:300],
        "description": (fields.get("description") or "").strip()[:2000],
        "all_day": all_day,
    }
    if all_day:
        new["start"], new["end"] = sd.isoformat(), ed.isoformat()
        return new

    start_time = (fields.get("start_time") or "").strip()
    end_time = (fields.get("end_time") or "").strip()
    try:
        st = datetime.strptime(start_time, "%H:%M").time()
    except ValueError:
        raise ValueError("Start time is required for an event that is not all day")
    start_dt = datetime.combine(sd, st)
    if end_time:
        try:
            end_dt = datetime.combine(ed, datetime.strptime(end_time, "%H:%M").time())
        except ValueError:
            raise ValueError("End time must look like 15:30")
        if end_dt <= start_dt:
            raise ValueError("End time must be after start time")
    else:
        end_dt = start_dt + timedelta(minutes=config.DEFAULT_EVENT_MINUTES)
    new["start"] = start_dt.strftime("%Y-%m-%dT%H:%M")
    new["end"] = end_dt.strftime("%Y-%m-%dT%H:%M")
    return new


def approve_all(import_id: int) -> list[str]:
    errors = []
    for row in db.events_for_import(import_id):
        if row["status"] == "pending":
            err = approve_event(row["id"])
            if err:
                errors.append(f'{row["title"]}: {err}')
    return errors


def undo_import(import_id: int) -> list[str]:
    errors = []
    for row in db.events_for_import(import_id):
        if row["status"] in ("created", "pending"):
            err = remove_event(row["id"])
            if err:
                errors.append(f'{row["title"]}: {err}')
    db.log("undo", "Undid everything from this import", import_id)
    return errors


def reprocess(import_id: int) -> str | None:
    """Run the AI again. Events already on the calendar are kept (and will show as duplicates)."""
    imp = db.get_import(import_id)
    if not imp:
        return "Import not found"
    with work_lock:
        db.execute("DELETE FROM events WHERE import_id = ? AND status != 'created'", (import_id,))
    db.log("reprocess", "Running the AI again", import_id)

    if imp["source"] == "email":
        if not imp["raw_path"] or not os.path.exists(imp["raw_path"]):
            return "The original email file is missing, so it can't be reprocessed"
        with open(imp["raw_path"], "rb") as fh:
            parsed = parse_email(fh.read())
        run_extraction(import_id, parsed.subject, parsed.from_addr, parsed.body_text, parsed.attachments)
    elif imp["source"] == "upload":
        if not imp["raw_path"] or not os.path.exists(imp["raw_path"]):
            return "The uploaded file is missing, so it can't be reprocessed"
        filename = imp["subject"]
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        run_extraction(import_id, filename, "web upload", imp["body_text"] or "",
                       [_attachment_from_file(imp["raw_path"], filename, ctype)])
    else:
        run_extraction(import_id, imp["subject"], imp["from_addr"], imp["body_text"] or "", [])
    return None


def approve_all_pending() -> list[str]:
    """Approve every waiting event, from every email."""
    errors = []
    for row in db.query("SELECT * FROM events WHERE status = 'pending' ORDER BY start"):
        err = approve_event(row["id"])
        if err:
            errors.append(f'{row["title"]}: {err}')
    return errors


def skip_all_pending() -> int:
    """Skip every waiting event, from every email. Returns how many were skipped."""
    rows = db.query("SELECT * FROM events WHERE status = 'pending'")
    for row in rows:
        remove_event(row["id"])
    return len(rows)
