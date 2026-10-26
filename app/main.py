"""Web page + background mail checker."""
import imaplib
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, calendar_client, config, db, mailbox, notify, processor
from .display import fmt_when

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = os.path.dirname(__file__)
UPLOAD_TYPES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".ics")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    ai.start_model_download()
    mailbox.start()
    yield
    mailbox.stop()


security = HTTPBasic(auto_error=False)


def require_login(credentials: HTTPBasicCredentials | None = Depends(security)):
    if not config.UI_PASSWORD:
        return
    if credentials is None or not secrets.compare_digest(credentials.password.encode(), config.UI_PASSWORD.encode()):
        raise HTTPException(status_code=401, detail="Login required", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Magic Import", lifespan=lifespan, dependencies=[Depends(require_login)])
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# ---------- display helpers ----------

def fmt_ts(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(config.TIMEZONE)
        return f"{dt:%b %-d, %Y %-I:%M %p}"
    except ValueError:
        return value


templates.env.filters["when"] = fmt_when
templates.env.filters["ts"] = fmt_ts
templates.env.globals["status_labels"] = {
    "created": "on calendar", "pending": "waiting for approval", "duplicate": "duplicate - not added",
    "skipped": "skipped", "deleted": "removed from calendar", "failed": "failed",
}


def redirect(url: str, msg: str = "", error: bool = False) -> RedirectResponse:
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{'err' if error else 'msg'}={quote(msg)}"
    return RedirectResponse(url, status_code=303)


def in_background(fn, *args) -> None:
    def _run():
        try:
            fn(*args)
        except Exception as exc:
            logging.exception("Background job failed")
            db.log("error", f"Background job failed: {exc}")
    threading.Thread(target=_run, daemon=True).start()


def render(request: Request, name: str, **context):
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("err", request.query_params.get("err"))
    context["review_mode"] = db.review_mode()
    return templates.TemplateResponse(request, name, context)


# ---------- pages ----------

@app.get("/")
def index(request: Request):
    imports = db.query("SELECT * FROM imports ORDER BY id DESC LIMIT 200")
    counts = {row["import_id"]: row for row in db.query(
        "SELECT import_id, "
        "SUM(status = 'created') AS created, SUM(status = 'pending') AS pending, "
        "SUM(status = 'duplicate') AS duplicate, SUM(status IN ('skipped','deleted')) AS removed, "
        "SUM(status = 'failed') AS failed, COUNT(*) AS total "
        "FROM events GROUP BY import_id")}
    pending_total = db.query_one("SELECT COUNT(*) AS n FROM events WHERE status = 'pending'")["n"]
    upcoming = db.query(
        "SELECT e.*, i.subject FROM events e JOIN imports i ON i.id = e.import_id "
        "WHERE e.status IN ('created', 'pending') AND substr(e.end, 1, 10) >= ? "
        "ORDER BY e.start LIMIT 15", (date.today().isoformat(),))
    any_processing = any(i["status"] == "processing" for i in imports)
    return render(
        request, "index.html",
        imports=imports, counts=counts, pending_total=pending_total, upcoming=upcoming,
        any_processing=any_processing,
        mailbox_ready=mailbox.is_configured(), calendar_ready=calendar_client.is_configured(),
        ai_ready=bool(config.ANTHROPIC_API_KEY) if config.AI_PROVIDER == "claude" else db.get_setting("ai_model_status") == "ready",
        ai_status=db.get_setting("ai_model_status") if config.AI_PROVIDER == "ollama" else "ready",
        ai_label=f"{config.AI_MODEL} ({'on your server' if config.AI_PROVIDER == 'ollama' else 'Claude cloud'})",
        mail_address=config.IMAP_USERNAME, calendar_name=config.ICLOUD_CALENDAR_NAME,
        last_check=db.get_setting("last_mail_check"), last_error=db.get_setting("last_mail_error"),
        poll_seconds=config.POLL_SECONDS,
    )


@app.get("/imports/{import_id}")
def import_detail(request: Request, import_id: int):
    imp = db.get_import(import_id)
    if not imp:
        raise HTTPException(404, "Not found")
    events = db.events_for_import(import_id)
    activity = db.query("SELECT * FROM activity WHERE import_id = ? ORDER BY id DESC", (import_id,))
    return render(request, "import.html", imp=imp, events=events, activity=activity)


@app.get("/events/{event_id}/edit")
def edit_page(request: Request, event_id: int):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    return render(request, "event_edit.html", ev=ev, form=_form_values(ev), imp=db.get_import(ev["import_id"]))


@app.get("/imports/{import_id}/add")
def add_page(request: Request, import_id: int):
    imp = db.get_import(import_id)
    if not imp:
        raise HTTPException(404, "Not found")
    today = date.today().isoformat()
    form = {"title": "", "start_date": today, "end_date": today, "start_time": "", "end_time": "",
            "all_day": True, "location": "", "description": ""}
    return render(request, "event_edit.html", ev=None, form=form, imp=imp)


def _form_values(ev: dict) -> dict:
    all_day = bool(ev["all_day"])
    start, end = ev["start"], ev["end"] or ev["start"]
    return {
        "title": ev["title"],
        "start_date": start[:10], "end_date": end[:10],
        "start_time": "" if all_day else start[11:16],
        "end_time": "" if all_day else end[11:16],
        "all_day": all_day, "location": ev["location"] or "", "description": ev["description"] or "",
    }


@app.get("/activity")
def activity_page(request: Request):
    rows = db.query("SELECT * FROM activity ORDER BY id DESC LIMIT 500")
    return render(request, "activity.html", rows=rows)


@app.get("/setup")
def setup_page(request: Request):
    checks = []

    if config.AI_PROVIDER == "claude":
        if config.ANTHROPIC_API_KEY:
            checks.append(("AI (Claude)", True, f"Key is set. Model: {config.AI_MODEL}"))
        else:
            checks.append(("AI (Claude)", False, "ANTHROPIC_API_KEY is empty"))
    else:
        try:
            names = ai.ollama_installed_models()
            if ai._model_present(names):
                checks.append(("AI (local)", True, f"Ollama is running and {config.AI_MODEL} is downloaded"))
            else:
                status = db.get_setting("ai_model_status") or "not downloaded yet"
                checks.append(("AI (local)", False, f"Ollama is running. {config.AI_MODEL}: {status}"))
        except Exception as exc:
            checks.append(("AI (local)", False, f"Can't reach Ollama at {config.OLLAMA_URL}: {exc}"))

    if mailbox.is_configured():
        try:
            imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
            imap.login(config.IMAP_USERNAME, config.IMAP_PASSWORD)
            imap.logout()
            checks.append(("Mailbox", True, f"Logged in to {config.IMAP_USERNAME} on {config.IMAP_HOST}"))
        except Exception as exc:
            checks.append(("Mailbox", False, f"Could not log in: {exc}"))
    else:
        checks.append(("Mailbox", False, "IMAP_USERNAME / IMAP_PASSWORD are empty"))

    if config.ALLOWED_SENDERS:
        checks.append(("Allowed senders", True, ", ".join(config.ALLOWED_SENDERS)))
    else:
        checks.append(("Allowed senders", False, "ALLOWED_SENDERS is empty, so ANY email sent to the mailbox will be processed"))

    names = []
    if config.ICLOUD_USERNAME and config.ICLOUD_APP_PASSWORD:
        try:
            names = calendar_client.list_calendar_names()
            checks.append(("iCloud login", True, f"Logged in as {config.ICLOUD_USERNAME}"))
            try:
                calendar_client.get_calendar(force_reconnect=True)
                checks.append(("Calendar", True, f'Found "{config.ICLOUD_CALENDAR_NAME}"'))
            except Exception as exc:
                checks.append(("Calendar", False, str(exc)))
        except Exception as exc:
            checks.append(("iCloud login", False, f"Could not log in: {exc}"))
    else:
        checks.append(("iCloud login", False, "ICLOUD_USERNAME / ICLOUD_APP_PASSWORD are empty"))

    return render(request, "setup.html", checks=checks, calendar_names=names, config=config)


@app.get("/health")
def health():
    return PlainTextResponse("ok")


# ---------- actions ----------

@app.post("/check-mail")
def check_mail():
    mailbox.wake_up.set()
    return redirect("/", "Checking the mailbox now. Refresh in a minute to see new emails.")


@app.post("/review-mode")
def toggle_review(enabled: str = Form("0")):
    db.set_setting("review_mode", "1" if enabled == "1" else "0")
    state = "ON: new events will wait for your approval" if enabled == "1" else "OFF: new events go straight to the calendar"
    db.log("setting", f"Review mode {state}")
    return redirect("/", f"Review mode is {state}.")


@app.post("/upload")
async def upload(file: UploadFile = File(...), note: str = Form("")):
    name = file.filename or "upload"
    if not name.lower().endswith(UPLOAD_TYPES):
        return redirect("/", "Please upload a PDF, picture (JPG/PNG), or .txt file.", error=True)
    data = await file.read()
    if not data:
        return redirect("/", "That file was empty.", error=True)
    in_background(processor.process_upload, name, data, note)
    return redirect("/", f"Reading {name} now. It will appear below in a minute.")


@app.post("/paste")
def paste(text: str = Form(...), title: str = Form("")):
    if not text.strip():
        return redirect("/", "Paste some text first.", error=True)
    in_background(processor.process_text, text, title)
    return redirect("/", "Reading your text now. It will appear below in a minute.")


@app.post("/imports/{import_id}/approve-all")
def approve_all(import_id: int):
    errors = processor.approve_all(import_id)
    if errors:
        return redirect(f"/imports/{import_id}", "Some events failed: " + "; ".join(errors), error=True)
    return redirect(f"/imports/{import_id}", "All waiting events were added to the calendar.")


@app.post("/imports/{import_id}/undo")
def undo(import_id: int):
    errors = processor.undo_import(import_id)
    if errors:
        return redirect(f"/imports/{import_id}", "Some events could not be removed: " + "; ".join(errors), error=True)
    return redirect(f"/imports/{import_id}", "Everything from this email was removed from the calendar.")


@app.post("/imports/{import_id}/reprocess")
def reprocess(import_id: int):
    if not db.get_import(import_id):
        raise HTTPException(404, "Not found")
    db.update_import(import_id, status="processing")
    in_background(processor.reprocess, import_id)
    return redirect(f"/imports/{import_id}", "Reading it again. Refresh in a minute.")


@app.post("/imports/{import_id}/add")
def add_event(import_id: int, title: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
              start_time: str = Form(""), end_time: str = Form(""), all_day: str = Form(""),
              location: str = Form(""), description: str = Form("")):
    fields = dict(title=title, start_date=start_date, end_date=end_date, start_time=start_time,
                  end_time=end_time, all_day=all_day, location=location, description=description)
    err = processor.add_manual_event(import_id, fields)
    if err:
        return redirect(f"/imports/{import_id}/add", err, error=True)
    return redirect(f"/imports/{import_id}", "Event added.")


@app.post("/events/{event_id}/approve")
def approve(event_id: int):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    err = processor.approve_event(event_id)
    if err:
        return redirect(f"/imports/{ev['import_id']}", err, error=True)
    return redirect(f"/imports/{ev['import_id']}", f'"{ev["title"]}" was added to the calendar.')


@app.post("/events/{event_id}/remove")
def remove(event_id: int):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    err = processor.remove_event(event_id)
    if err:
        return redirect(f"/imports/{ev['import_id']}", err, error=True)
    return redirect(f"/imports/{ev['import_id']}", f'"{ev["title"]}" was removed.')


@app.post("/events/{event_id}/edit")
def edit(event_id: int, title: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
         start_time: str = Form(""), end_time: str = Form(""), all_day: str = Form(""),
         location: str = Form(""), description: str = Form("")):
    ev = db.get_event(event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    fields = dict(title=title, start_date=start_date, end_date=end_date, start_time=start_time,
                  end_time=end_time, all_day=all_day, location=location, description=description)
    err = processor.edit_event(event_id, fields)
    if err:
        return redirect(f"/events/{event_id}/edit", err, error=True)
    where = " and on the calendar" if ev["status"] == "created" else ""
    return redirect(f"/imports/{ev['import_id']}", f"Saved{where}.")


# ---------- API for Home Assistant and other tools (JSON) ----------

def _api_result(err: str | None, event_id: int | None = None) -> JSONResponse:
    body = {"ok": err is None}
    if err:
        body["error"] = err
    if event_id is not None:
        row = db.get_event(event_id)
        body["event"] = notify.event_payload(row) if row else None
    return JSONResponse(body, status_code=200 if err is None else 400)


@app.get("/api/pending")
def api_pending():
    rows = db.query(
        "SELECT e.*, i.subject FROM events e JOIN imports i ON i.id = e.import_id "
        "WHERE e.status = 'pending' ORDER BY e.start LIMIT 25")
    events = [{**notify.event_payload(row), "subject": row["subject"] or ""} for row in rows]
    total = db.query_one("SELECT COUNT(*) AS n FROM events WHERE status = 'pending'")["n"]
    return {"count": total, "review_mode": db.review_mode(), "events": events}


@app.get("/api/imports/{import_id}")
def api_import(import_id: int):
    if not db.get_import(import_id):
        raise HTTPException(404, "Not found")
    return notify.import_payload(import_id)


@app.post("/api/events/{event_id}/approve")
def api_approve(event_id: int):
    if not db.get_event(event_id):
        raise HTTPException(404, "Not found")
    return _api_result(processor.approve_event(event_id), event_id)


@app.post("/api/events/{event_id}/skip")
def api_skip(event_id: int):
    """Waiting event: skip it. Event already on the calendar: remove it (undo)."""
    if not db.get_event(event_id):
        raise HTTPException(404, "Not found")
    return _api_result(processor.remove_event(event_id), event_id)


@app.post("/api/pending/approve-all")
def api_approve_all():
    errors = processor.approve_all_pending()
    return _api_result("; ".join(errors) if errors else None)


@app.post("/api/pending/skip-all")
def api_skip_all():
    processor.skip_all_pending()
    return _api_result(None)


@app.post("/api/review-mode/{state}")
def api_review_mode(state: str):
    on = state.lower() in ("on", "1", "true")
    db.set_setting("review_mode", "1" if on else "0")
    db.log("setting", f"Review mode {'ON' if on else 'OFF'} (from Home Assistant)")
    return {"ok": True, "review_mode": on}


# ---------- compact page for a Home Assistant dashboard (Webpage card) ----------

@app.get("/ha")
def ha_page(request: Request):
    rows = db.query(
        "SELECT e.*, i.subject FROM events e JOIN imports i ON i.id = e.import_id "
        "WHERE e.status = 'pending' ORDER BY e.start")
    recent = db.query(
        "SELECT e.*, i.subject FROM events e JOIN imports i ON i.id = e.import_id "
        "WHERE e.status = 'created' ORDER BY e.updated_at DESC LIMIT 5")
    return render(request, "ha.html", pending=rows, recent=recent)


@app.post("/ha/events/{event_id}/{action}")
def ha_action(event_id: int, action: str):
    if action == "approve":
        err = processor.approve_event(event_id)
    elif action in ("skip", "undo"):
        err = processor.remove_event(event_id)
    else:
        raise HTTPException(404, "Not found")
    return redirect("/ha", err or "", error=bool(err))


@app.post("/ha/all/{action}")
def ha_all(action: str):
    if action == "approve":
        errors = processor.approve_all_pending()
        return redirect("/ha", "; ".join(errors), error=bool(errors))
    if action == "skip":
        processor.skip_all_pending()
        return redirect("/ha")
    raise HTTPException(404, "Not found")
