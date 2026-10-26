"""All settings come from environment variables (set in docker-compose.yml)."""
import os
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


# Mailbox the app reads (the address you forward school emails to)
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com").strip()
IMAP_PORT = _int("IMAP_PORT", 993)
IMAP_USERNAME = os.environ.get("IMAP_USERNAME", "").strip()
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "").replace(" ", "").strip()
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX").strip()
POLL_SECONDS = max(30, _int("POLL_SECONDS", 120))

# Only emails from these addresses are processed. Empty = anyone (not recommended).
ALLOWED_SENDERS = _list("ALLOWED_SENDERS")

# iCloud calendar
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/").strip()
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME", "").strip()
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "").replace(" ", "").strip()
ICLOUD_CALENDAR_NAME = os.environ.get("ICLOUD_CALENDAR_NAME", "").strip()

# AI: "ollama" (local, runs on your own GPU) or "claude" (Anthropic's cloud API)
AI_PROVIDER = os.environ.get("AI_PROVIDER", "ollama").strip().lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").strip().rstrip("/")
OLLAMA_NUM_CTX = max(4096, _int("OLLAMA_NUM_CTX", 16384))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_default_model = "claude-opus-5-5" if AI_PROVIDER == "claude" else "qwen3-vl:8b-instruct"
AI_MODEL = os.environ.get("AI_MODEL", "").strip() or _default_model
AI_EXTRA_INSTRUCTIONS = os.environ.get("AI_EXTRA_INSTRUCTIONS", "").strip()
# Scanned PDF pages are turned into pictures for the AI; this caps how many
PDF_MAX_IMAGE_PAGES = max(1, _int("PDF_MAX_IMAGE_PAGES", 8))

# Behavior
TIMEZONE_NAME = os.environ.get("TIMEZONE", "America/Chicago").strip()
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
REVIEW_MODE_DEFAULT = _bool("REVIEW_MODE", False)
DEFAULT_EVENT_MINUTES = max(5, _int("DEFAULT_EVENT_MINUTES", 60))

# Web page
UI_PASSWORD = os.environ.get("UI_PASSWORD", "").strip()
# The address of this web page as other devices see it, e.g. http://192.168.1.50:8088
APP_URL = os.environ.get("APP_URL", "").strip().rstrip("/")

# Home Assistant: the app sends a message here each time it finishes reading an email
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "").strip()

# Storage
DATA_DIR = os.environ.get("DATA_DIR", "/data").strip()
DB_PATH = os.path.join(DATA_DIR, "magic-import.db")
FILES_DIR = os.path.join(DATA_DIR, "files")
