"""Human display for a summary. Formatting never changes the summary."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiofhir.summary import Summary, mark_data


def format_summary(summary: Summary, tz: str) -> str:
    zone = ZoneInfo(tz)
    lines = [summary.kind]
    for item in summary.items:
        when = _when(item.at, zone)
        marked = mark_data(item.text) if item.text else ""
        pieces = [piece for piece in (marked, when, f"({item.citation})") if piece]
        lines.append("- " + " ".join(pieces))
    return "\n".join(lines)


def _when(value: datetime | date | None, zone: ZoneInfo) -> str:
    if isinstance(value, datetime):
        local = value.astimezone(zone)
        return local.strftime("%a %b %-d, %Y, %-I:%M %p %Z")
    if isinstance(value, date):
        return value.strftime("%b %-d, %Y")
    return ""
