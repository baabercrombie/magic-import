"""Checks the dedicated mailbox every few minutes for new forwarded emails."""
import email
import imaplib
import logging
import threading
from datetime import datetime, timedelta

from . import config, db, processor

log = logging.getLogger("magic-import.mailbox")

wake_up = threading.Event()        # set by the "Check mail now" button
_stop = threading.Event()
LOOKBACK_DAYS = 3


def is_configured() -> bool:
    return bool(config.IMAP_HOST and config.IMAP_USERNAME and config.IMAP_PASSWORD)


def check_once() -> int:
    """Fetch and process every new email. Returns how many were handled."""
    handled = 0
    imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=60)
    try:
        imap.login(config.IMAP_USERNAME, config.IMAP_PASSWORD)
        status, _ = imap.select(f'"{config.IMAP_FOLDER}"')
        if status != "OK":
            raise RuntimeError(f'Could not open mail folder "{config.IMAP_FOLDER}"')
        # Look at the last few days of mail (read or unread) and skip anything already handled,
        # so it still works if someone opens the email in Gmail first.
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = imap.uid("search", None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("Mail search failed")
        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            status, head = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status == "OK" and head and isinstance(head[0], tuple):
                header = email.message_from_bytes(head[0][1])
                message_id = "".join((header.get("Message-ID") or "").split())
                if message_id and db.message_already_seen(message_id):
                    continue
            status, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            raw = msg_data[0][1]
            try:
                if processor.process_email(raw) is not None:
                    handled += 1
            except Exception as exc:   # never let one bad email stop the others
                log.exception("Failed to process email")
                db.log("error", f"Failed to process an email: {exc}")
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return handled


def _loop() -> None:
    while not _stop.is_set():
        if is_configured():
            try:
                count = check_once()
                db.set_setting("last_mail_check", db.now_iso())
                db.set_setting("last_mail_error", "")
                if count:
                    log.info("Processed %s email(s)", count)
            except Exception as exc:
                log.warning("Mail check failed: %s", exc)
                db.set_setting("last_mail_check", db.now_iso())
                db.set_setting("last_mail_error", str(exc))
        wake_up.wait(config.POLL_SECONDS)
        wake_up.clear()


def start() -> None:
    threading.Thread(target=_loop, name="mail-poller", daemon=True).start()


def stop() -> None:
    _stop.set()
    wake_up.set()
