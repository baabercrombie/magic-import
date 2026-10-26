"""Talks to the iCloud calendar over CalDAV: find duplicates, create, edit, delete."""
import re
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher

import caldav
from icalendar import Calendar, Event

from . import config

_lock = threading.Lock()
_calendar = None


class CalendarNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(config.ICLOUD_USERNAME and config.ICLOUD_APP_PASSWORD and config.ICLOUD_CALENDAR_NAME)


def _client() -> caldav.DAVClient:
    return caldav.DAVClient(
        url=config.CALDAV_URL,
        username=config.ICLOUD_USERNAME,
        password=config.ICLOUD_APP_PASSWORD,
        timeout=60,
    )


def list_calendar_names() -> list[str]:
    principal = _client().principal()
    names = []
    for cal in principal.calendars():
        try:
            names.append(cal.get_display_name() or "(no name)")
        except Exception:
            names.append(str(cal.url))
    return names


def get_calendar(force_reconnect: bool = False):
    global _calendar
    if not is_configured():
        raise CalendarNotConfigured(
            "iCloud is not set up. Fill in ICLOUD_USERNAME, ICLOUD_APP_PASSWORD and "
            "ICLOUD_CALENDAR_NAME in docker-compose.yml."
        )
    with _lock:
        if _calendar is not None and not force_reconnect:
            return _calendar
        principal = _client().principal()
        wanted = config.ICLOUD_CALENDAR_NAME.strip().lower()
        found_names = []
        for cal in principal.calendars():
            name = (cal.get_display_name() or "").strip()
            found_names.append(name)
            if name.lower() == wanted:
                _calendar = cal
                return cal
        raise CalendarNotConfigured(
            f'No calendar named "{config.ICLOUD_CALENDAR_NAME}" was found. '
            f"Calendars this Apple ID can see: {', '.join(found_names) or '(none)'}"
        )


def _with_retry(fn):
    """Run fn(calendar). If the connection went stale, reconnect once and retry."""
    try:
        return fn(get_calendar())
    except CalendarNotConfigured:
        raise
    except Exception:
        return fn(get_calendar(force_reconnect=True))


# ---------- conversions ----------

def _local_dt(value: str) -> datetime:
    """'YYYY-MM-DDTHH:MM' local time -> aware datetime in the configured time zone."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=config.TIMEZONE)


def event_range_local(ev: dict) -> tuple[datetime, datetime]:
    """Start/end of an app event as aware local datetimes (all-day covers whole days)."""
    if ev["all_day"]:
        start = datetime.combine(date.fromisoformat(ev["start"]), time.min, tzinfo=config.TIMEZONE)
        last = date.fromisoformat(ev.get("end") or ev["start"])
        end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=config.TIMEZONE)
        return start, end
    start = _local_dt(ev["start"])
    end = _local_dt(ev["end"]) if ev.get("end") else start + timedelta(minutes=config.DEFAULT_EVENT_MINUTES)
    return start, end


def build_ical(ev: dict, uid: str, note: str = "") -> str:
    cal = Calendar()
    cal.add("prodid", "-//Magic Import//EN")
    cal.add("version", "2.0")
    vevent = Event()
    vevent.add("uid", uid)
    vevent.add("dtstamp", datetime.now(timezone.utc))
    vevent.add("summary", ev["title"])
    if ev["all_day"]:
        first = date.fromisoformat(ev["start"])
        last = date.fromisoformat(ev.get("end") or ev["start"])
        vevent.add("dtstart", first)
        vevent.add("dtend", last + timedelta(days=1))   # iCal all-day end is exclusive
    else:
        start, end = event_range_local(ev)
        vevent.add("dtstart", start.astimezone(timezone.utc))
        vevent.add("dtend", end.astimezone(timezone.utc))
    if ev.get("location"):
        vevent.add("location", ev["location"])
    description = (ev.get("description") or "").strip()
    if note:
        description = f"{description}\n\n{note}".strip()
    if description:
        vevent.add("description", description)
    cal.add_component(vevent)
    return cal.to_ical().decode("utf-8")


def _to_local(value) -> tuple[datetime, bool]:
    """Convert an iCal DTSTART/DTEND value to (aware local datetime, is_all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=config.TIMEZONE)   # floating time
        return value.astimezone(config.TIMEZONE), False
    return datetime.combine(value, time.min, tzinfo=config.TIMEZONE), True


def _existing_events(cal, start: datetime, end: datetime) -> list[dict]:
    results = cal.search(start=start, end=end, event=True, expand=True)
    found = []
    for obj in results:
        try:
            comp = obj.icalendar_component
            dtstart = comp.get("dtstart")
            if dtstart is None:
                continue
            s, all_day = _to_local(dtstart.dt)
            dtend = comp.get("dtend")
            if dtend is not None:
                e, _ = _to_local(dtend.dt)
            elif all_day:
                e = s + timedelta(days=1)
            else:
                e = s + timedelta(minutes=config.DEFAULT_EVENT_MINUTES)
            found.append({
                "title": str(comp.get("summary", "")),
                "start": s,
                "end": e,
                "all_day": all_day,
                "uid": str(comp.get("uid", "")),
            })
        except Exception:
            continue
    return found


# ---------- duplicate matching ----------

_STOP_WORDS = {"the", "and", "for", "a", "an", "of", "at", "to", "in", "on", "day", "event"}


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", title.lower()).strip()


def _words(title: str) -> set[str]:
    return {w for w in _norm(title).split() if len(w) > 1 and w not in _STOP_WORDS}


def title_similarity(a: str, b: str) -> float:
    na, nb = " ".join(_norm(a).split()), " ".join(_norm(b).split())
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return 0.9
    ratio = SequenceMatcher(None, na, nb).ratio()
    wa, wb = _words(a), _words(b)
    jaccard = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return max(ratio, jaccard)


def is_same_event(new: dict, new_start: datetime, new_end: datetime, existing: dict) -> bool:
    """new = app event; existing = {title, start, end, all_day} with aware local datetimes."""
    # Must overlap in time (all-day events cover whole days)
    if not (new_start < existing["end"] and existing["start"] < new_end):
        # timed events on the same day but not overlapping can still be the same thing
        if new_start.date() != existing["start"].date():
            return False
    sim = title_similarity(new["title"], existing["title"])
    if not new["all_day"] and not existing["all_day"]:
        close_in_time = abs((new_start - existing["start"]).total_seconds()) <= 90 * 60
        return (close_in_time and sim >= 0.6) or sim >= 0.85
    if new["all_day"] and existing["all_day"]:
        return sim >= 0.6
    return sim >= 0.8


def find_duplicate(ev: dict) -> str | None:
    """Look on the calendar for an event that matches. Returns a description of it, or None."""
    start, end = event_range_local(ev)
    day_start = datetime.combine(start.date(), time.min, tzinfo=config.TIMEZONE)
    day_end = datetime.combine(end.date() + timedelta(days=1), time.min, tzinfo=config.TIMEZONE)

    existing = _with_retry(lambda cal: _existing_events(cal, day_start, day_end))
    for other in existing:
        if is_same_event(ev, start, end, other):
            when = other["start"].strftime("%a %b %d") if other["all_day"] else other["start"].strftime("%a %b %d %I:%M %p")
            return f'"{other["title"]}" on {when}'
    return None


# ---------- create / update / delete ----------

def create_event(ev: dict, note: str = "") -> tuple[str, str]:
    """Creates the event. Returns (event_url, uid)."""
    uid = f"{uuid.uuid4()}@magic-import"
    ical = build_ical(ev, uid, note)
    # No automatic retry here, so a slow network can never create the event twice
    obj = get_calendar().add_event(ical)
    return str(obj.url), uid


def update_event(url: str, uid: str, ev: dict, note: str = "") -> None:
    ical = build_ical(ev, uid, note)

    def _save(cal):
        obj = caldav.Event(client=cal.client, url=url, data=ical, parent=cal)
        obj.save()

    _with_retry(_save)


def delete_event(url: str) -> None:
    def _delete(cal):
        obj = caldav.Event(client=cal.client, url=url, parent=cal)
        try:
            obj.delete()
        except caldav.error.NotFoundError:
            pass   # already gone (deleted by hand in the Calendar app)

    _with_retry(_delete)
