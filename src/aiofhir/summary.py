"""Compact cited summaries for a patient record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True)
class Citation:
    resource_type: str
    id: str

    def __str__(self) -> str:
        return f"{self.resource_type}/{self.id}"


@dataclass(frozen=True)
class Item:
    text: str
    citation: Citation
    at: datetime | date | None = None


@dataclass(frozen=True)
class Summary:
    kind: str
    items: tuple[Item, ...]
    warnings: tuple[str, ...] = ()
    truncated: bool = False

    def to_text(self, max_chars: int = 4000) -> str:
        lines = [_line(item) for item in self.items]
        chosen: list[str] = []
        for index, line in enumerate(lines):
            remaining = len(lines) - index - 1
            extra = f"\n(+{remaining} more)" if remaining else ""
            draft = "\n".join([self.kind, *chosen, line]) + extra
            if len(draft) > max_chars:
                break
            chosen.append(line)
        hidden = len(lines) - len(chosen)
        text = "\n".join([self.kind, *chosen])
        if hidden:
            text += f"\n(+{hidden} more)"
        return text[:max_chars]


def mark_data(text: str) -> str:
    """Wrap FHIR free text so it cannot close the marker."""
    safe = text.replace("«", "‹").replace("»", "›")
    return f"«{safe}»"


def _line(item: Item) -> str:
    pieces = ["-"]
    if item.text:
        pieces.append(mark_data(item.text))
    when = iso_when(item.at)
    if when:
        pieces.append(when)
    pieces.append(f"({item.citation})")
    return " ".join(pieces)


def iso_when(value: datetime | date | None) -> str:
    """ISO 8601 for model-facing text: UTC instants and plain dates. Never local time."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return ""
