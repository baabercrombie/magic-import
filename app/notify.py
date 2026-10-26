"""Tells Home Assistant (or anything else with a webhook) when an email has been read."""
import logging
import threading

import httpx

from . import config, db
from .display import fmt_when

log = logging.getLogger("magic-import.notify")


def event_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "when": fmt_when(row),
        "location": row["location"] or "",
        "status": row["status"],
        "import_id": row["import_id"],
    }


def import_payload(import_id: int) -> dict:
    imp = db.get_import(import_id) or {}
    events = [event_payload(row) for row in db.events_for_import(import_id)]
    return {
        "import_id": import_id,
        "subject": imp.get("subject") or "",
        "from": imp.get("from_addr") or "",
        "status": imp.get("status") or "",
        "error": imp.get("error") or "",
        "review_mode": db.review_mode(),
        "url": f"{config.APP_URL}/imports/{import_id}" if config.APP_URL else "",
        "pending": sum(1 for e in events if e["status"] == "pending"),
        "created": sum(1 for e in events if e["status"] == "created"),
        "events": events,
    }


def send_import_update(import_id: int) -> None:
    """Fire-and-forget POST to HA_WEBHOOK_URL. Never slows down or breaks email processing."""
    if not config.HA_WEBHOOK_URL:
        return

    def _send():
        try:
            resp = httpx.post(config.HA_WEBHOOK_URL, json=import_payload(import_id), timeout=15)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:
            log.warning("Home Assistant webhook failed: %s", exc)
            db.log("error", f"Could not notify Home Assistant: {exc}", import_id)

    threading.Thread(target=_send, name="ha-webhook", daemon=True).start()
