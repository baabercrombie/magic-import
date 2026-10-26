"""Turn a raw email into plain text plus a list of useful attachments."""
import email
import re
from dataclasses import dataclass, field
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

PDF_TYPES = {"application/pdf"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_TYPES = {"text/plain", "text/calendar", "text/csv"}

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MIN_IMAGE_BYTES = 15 * 1024      # smaller images are usually logos / signatures
MAX_IMAGES = 10


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedEmail:
    message_id: str
    from_addr: str
    subject: str
    date_iso: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text("\n")
    return clean_text(text)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_email(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=policy.default)

    message_id = "".join(str(msg.get("Message-ID") or "").split())
    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    subject = str(msg.get("Subject", "") or "").strip()
    try:
        date_iso = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        date_iso = ""

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []
    skipped: list[str] = []
    image_count = 0

    # walk() also goes inside forwarded emails attached as message/rfc822
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()

        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue

        is_attachment = disposition == "attachment" or (filename is not None and ctype not in ("text/plain", "text/html"))

        if not is_attachment and ctype == "text/plain":
            plain_parts.append(_decode(part, payload))
            continue
        if not is_attachment and ctype == "text/html":
            html_parts.append(_decode(part, payload))
            continue

        name = filename or f"attachment.{ctype.split('/')[-1]}"
        if len(payload) > MAX_ATTACHMENT_BYTES:
            skipped.append(f"{name} (too large)")
            continue
        if ctype == "application/octet-stream" and name.lower().endswith(".pdf"):
            ctype = "application/pdf"

        if ctype in PDF_TYPES:
            attachments.append(Attachment(name, ctype, payload))
        elif ctype in IMAGE_TYPES:
            if len(payload) < MIN_IMAGE_BYTES:
                continue
            if image_count >= MAX_IMAGES:
                skipped.append(f"{name} (too many images)")
                continue
            image_count += 1
            attachments.append(Attachment(name, ctype, payload))
        elif ctype in TEXT_TYPES:
            attachments.append(Attachment(name, "text/plain", payload))
        elif filename:
            skipped.append(f"{name} (unsupported type {ctype})")

    if plain_parts:
        body = clean_text("\n\n".join(plain_parts))
        # Some senders put real content only in HTML; use it if plain text is nearly empty
        if len(body) < 40 and html_parts:
            body = html_to_text("\n".join(html_parts))
    elif html_parts:
        body = html_to_text("\n".join(html_parts))
    else:
        body = ""

    return ParsedEmail(message_id, from_addr, subject, date_iso, body, attachments, skipped)


def _decode(part, payload: bytes) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
