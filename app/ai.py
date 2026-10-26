"""Ask the AI (local Ollama model, or Claude) to read an email / PDF / image and pull out calendar events."""
import base64
import io
import json
import logging
import threading
from datetime import date, datetime, timedelta

import anthropic
import httpx
import pypdfium2 as pdfium
from PIL import Image

from . import config, db
from .mailparse import Attachment

log = logging.getLogger("magic-import.ai")

MAX_TEXT_CHARS = 30000          # keeps long newsletters inside the local model's memory
MAX_IMAGE_SIDE = 1600           # pictures are shrunk to this many pixels on the long side
MIN_PAGE_TEXT = 80              # a PDF page with less text than this is treated as a scan

# Models that accept the server-side refusal fallback ("fallbacks": "default")
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One or two sentences describing what this email/document is about.",
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM 24-hour, or empty string if no time"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, or empty string"},
                    "end_time": {"type": "string", "description": "HH:MM 24-hour, or empty string"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "start_date", "start_time", "end_date", "end_time",
                             "all_day", "location", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "events"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You turn emails, newsletters, flyers and PDFs (usually from a school, team or activity) into calendar events for a family calendar.

Rules:
- Only include things that happen on a specific date: events, performances, games, practices, field trips, picture day, conferences, early dismissals, no-school days, holidays/breaks, deadlines that a parent must act on (put "Due: " at the start of the title).
- Do not invent events. If something has no date, leave it out.
- If a year is not written, choose the year that makes the date the next upcoming occurrence on or after today (school-year logic). Dates a few days in the past that are clearly being described are fine to keep.
- Use 24-hour HH:MM times. If there is a start time but no end time, leave end_time empty.
- If there is no time, set all_day to true and leave start_time and end_time empty.
- A multi-day event (for example a break from Mon to Fri) is ONE all-day event with end_date set to the last day.
- A repeating event with listed dates (for example "every Tuesday in March" or "Oct 3, 10, 17") should be one event per date.
- Titles should be short and clear, like a person would type them into a calendar (for example "No School - Teacher In-Service" or "3rd Grade Field Trip to Zoo"). Include the child's name, grade, class or team in the title when the email is about a specific one.
- description: one to three short lines with the useful details (what to bring, cost, who it is for). Do not paste the whole email.
- location: the place if given, otherwise empty string.
- If the email has no events, return an empty events list."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=300, max_retries=3)


def _header(subject: str, from_addr: str, has_files: bool) -> str:
    today = datetime.now(config.TIMEZONE)
    header = (
        f"Today is {today:%A, %B %d, %Y}. Time zone: {config.TIMEZONE_NAME}.\n"
        f"Email subject: {subject or '(none)'}\n"
        f"Forwarded by: {from_addr or '(unknown)'}\n"
    )
    if config.AI_EXTRA_INSTRUCTIONS:
        header += f"\nFamily preferences (follow these):\n{config.AI_EXTRA_INSTRUCTIONS}\n"
    if has_files:
        header += "\nThe attached files are part of this email.\n"
    return header


def build_content(subject: str, from_addr: str, body_text: str, attachments: list[Attachment]) -> list[dict]:
    """Message content for Claude, which reads PDFs and pictures directly."""
    content: list[dict] = []
    for att in attachments:
        if att.content_type == "application/pdf":
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf",
                           "data": base64.b64encode(att.data).decode("ascii")},
                "title": att.filename,
            })
        elif att.content_type.startswith("image/"):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": att.content_type,
                           "data": base64.b64encode(att.data).decode("ascii")},
            })
        else:
            text = att.data.decode("utf-8", errors="replace")
            content.append({"type": "text", "text": f"Attached file {att.filename}:\n{text}"})

    text = _header(subject, from_addr, bool(attachments)) + f"\nEmail text:\n{body_text or '(no text)'}"
    content.append({"type": "text", "text": text})
    return content


# ---------- local model (Ollama) ----------

def _image_b64(img: Image.Image) -> str:
    img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_local_inputs(attachments: list[Attachment]) -> tuple[str, list[str]]:
    """Local models can't open PDFs, so: PDF text is pulled out here, and scanned pages
    (and attached pictures) are turned into JPEG images. Returns (files_text, images)."""
    texts: list[str] = []
    images: list[str] = []
    for att in attachments:
        if att.content_type == "application/pdf":
            try:
                pdf = pdfium.PdfDocument(att.data)
            except Exception as exc:
                texts.append(f"--- {att.filename}: could not open this PDF ({exc}) ---")
                continue
            for index in range(len(pdf)):
                page = pdf[index]
                page_text = page.get_textpage().get_text_range().strip()
                if len(page_text) >= MIN_PAGE_TEXT:
                    texts.append(f"--- {att.filename}, page {index + 1} ---\n{page_text}")
                elif len(images) < config.PDF_MAX_IMAGE_PAGES:
                    scale = MAX_IMAGE_SIDE / max(page.get_width(), page.get_height())
                    images.append(_image_b64(page.render(scale=scale).to_pil()))
                    texts.append(f"--- {att.filename}, page {index + 1}: see picture {len(images)} ---")
            pdf.close()
        elif att.content_type.startswith("image/"):
            try:
                images.append(_image_b64(Image.open(io.BytesIO(att.data))))
                texts.append(f"--- {att.filename}: see picture {len(images)} ---")
            except Exception as exc:
                texts.append(f"--- {att.filename}: could not open this picture ({exc}) ---")
        else:
            texts.append(f"--- {att.filename} ---\n{att.data.decode('utf-8', errors='replace')}")
    return "\n\n".join(texts), images


_pull_lock = threading.Lock()


def ollama_installed_models() -> list[str]:
    resp = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=15)
    resp.raise_for_status()
    return [m.get("name", "") for m in resp.json().get("models", [])]


def _model_present(names: list[str]) -> bool:
    wanted = config.AI_MODEL if ":" in config.AI_MODEL else f"{config.AI_MODEL}:latest"
    return wanted in names


def ensure_local_model() -> None:
    """Download the model into Ollama the first time (several GB, takes a while)."""
    with _pull_lock:
        if _model_present(ollama_installed_models()):
            db.set_setting("ai_model_status", "ready")
            return
        db.set_setting("ai_model_status", f"downloading {config.AI_MODEL} (this can take 10+ minutes)")
        log.info("Downloading model %s into Ollama", config.AI_MODEL)
        resp = httpx.post(f"{config.OLLAMA_URL}/api/pull",
                          json={"model": config.AI_MODEL, "stream": False}, timeout=7200)
        if resp.status_code != 200:
            db.set_setting("ai_model_status", f"download failed: {resp.text[:300]}")
            raise RuntimeError(f"Could not download model {config.AI_MODEL}: {resp.text[:300]}")
        db.set_setting("ai_model_status", "ready")


def start_model_download() -> None:
    """Called at startup so the model is ready before the first email arrives."""
    if config.AI_PROVIDER != "ollama":
        return

    def _run():
        try:
            ensure_local_model()
        except Exception as exc:
            log.warning("Model check/download failed: %s", exc)
            if not (db.get_setting("ai_model_status") or "").startswith("download failed"):
                db.set_setting("ai_model_status", f"can't reach Ollama at {config.OLLAMA_URL}: {exc}")

    threading.Thread(target=_run, name="model-download", daemon=True).start()


def _extract_ollama(subject: str, from_addr: str, body_text: str, attachments: list[Attachment]) -> dict:
    ensure_local_model()
    files_text, images = prepare_local_inputs(attachments)

    text = _header(subject, from_addr, bool(attachments)) + f"\nEmail text:\n{body_text or '(no text)'}"
    if files_text:
        text += f"\n\nAttached files:\n{files_text}"
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n[The rest was cut off because it was too long.]"
    text += "\n\nReply with JSON only, matching the schema."

    user_message: dict = {"role": "user", "content": text}
    if images:
        user_message["images"] = images

    resp = httpx.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": config.AI_MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, user_message],
            "format": EVENT_SCHEMA,
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX},
        },
        timeout=900,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Local AI error ({resp.status_code}): {resp.text[:300]}")
    return _parse_json(resp.json().get("message", {}).get("content", ""))


# ---------- Claude (cloud) ----------

def _extract_claude(subject: str, from_addr: str, body_text: str, attachments: list[Attachment]) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in docker-compose.yml")

    kwargs = dict(
        model=config.AI_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_content(subject, from_addr, body_text, attachments)}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EVENT_SCHEMA}},
    )
    client = _client()
    if config.AI_MODEL in FALLBACK_MODELS:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    else:
        response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("The AI declined to read this email.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The AI response was cut off (email too long).")

    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_json(text)


def extract_events(subject: str, from_addr: str, body_text: str, attachments: list[Attachment]) -> dict:
    """Returns {"summary": str, "events": [normalized event dicts]}. Raises on failure."""
    if config.AI_PROVIDER == "claude":
        data = _extract_claude(subject, from_addr, body_text, attachments)
    else:
        data = _extract_ollama(subject, from_addr, body_text, attachments)
    events = [ev for ev in (normalize(raw) for raw in data.get("events", []) if isinstance(raw, dict)) if ev]
    return {"summary": str(data.get("summary", "")).strip(), "events": events}


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise RuntimeError(f"AI did not return valid JSON: {text[:200]}")


def _valid_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def _valid_time(value: str) -> str | None:
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def normalize(raw: dict) -> dict | None:
    """Convert the AI's fields into the app's event format (local times)."""
    title = str(raw.get("title", "")).strip()
    start_date = _valid_date(raw.get("start_date", ""))
    if not title or not start_date:
        return None
    end_date = _valid_date(raw.get("end_date", "")) or start_date
    if end_date < start_date:
        end_date = start_date
    start_time = _valid_time(raw.get("start_time", ""))
    end_time = _valid_time(raw.get("end_time", ""))
    all_day = bool(raw.get("all_day")) or start_time is None

    ev = {
        "title": title[:200],
        "location": str(raw.get("location", "")).strip()[:300],
        "description": str(raw.get("description", "")).strip()[:2000],
        "all_day": all_day,
    }
    if all_day:
        ev["start"] = start_date.isoformat()
        ev["end"] = end_date.isoformat()
    else:
        start_dt = datetime.combine(start_date, datetime.strptime(start_time, "%H:%M").time())
        if end_time:
            end_dt = datetime.combine(end_date, datetime.strptime(end_time, "%H:%M").time())
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(minutes=config.DEFAULT_EVENT_MINUTES)
        else:
            end_dt = start_dt + timedelta(minutes=config.DEFAULT_EVENT_MINUTES)
        ev["start"] = start_dt.strftime("%Y-%m-%dT%H:%M")
        ev["end"] = end_dt.strftime("%Y-%m-%dT%H:%M")
    return ev
