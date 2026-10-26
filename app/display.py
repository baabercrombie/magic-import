"""Turning stored events into friendly text."""
from datetime import date, datetime


def fmt_when(ev: dict) -> str:
    try:
        if ev["all_day"]:
            start = date.fromisoformat(ev["start"])
            end = date.fromisoformat(ev["end"] or ev["start"])
            if end != start:
                return f"{start:%a %b %-d} – {end:%a %b %-d, %Y} (all day)"
            return f"{start:%a %b %-d, %Y} (all day)"
        start = datetime.strptime(ev["start"], "%Y-%m-%dT%H:%M")
        end = datetime.strptime(ev["end"], "%Y-%m-%dT%H:%M") if ev["end"] else None
        text = f"{start:%a %b %-d, %Y · %-I:%M %p}"
        if end:
            text += f" – {end:%-I:%M %p}" if end.date() == start.date() else f" – {end:%a %b %-d %-I:%M %p}"
        return text
    except Exception:
        return f"{ev.get('start')} – {ev.get('end')}"
