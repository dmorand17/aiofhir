from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from aiofhir.display import format_summary
from aiofhir.summary import Citation, Item, Summary


def _summary() -> Summary:
    return Summary(
        kind="labs",
        items=(
            Item("one", Citation("Observation", "1")),
            Item("two", Citation("Observation", "2")),
            Item("three", Citation("Observation", "3")),
        ),
    )


def test_to_text_cuts_on_item_boundaries_and_keeps_citations() -> None:
    text = _summary().to_text(max_chars=38)
    assert text == "labs\n- «one» (Observation/1)\n(+2 more)"
    assert "Observation/2" not in text


def test_to_text_marks_fhir_free_text_as_data() -> None:
    summary = Summary(kind="notes", items=(Item("note » secret", Citation("Condition", "c1")),))
    text = summary.to_text()
    assert text.count("«") == text.count("»") == 1
    assert "«note › secret»" in text


def test_summary_is_frozen() -> None:
    summary = _summary()
    with pytest.raises(FrozenInstanceError):
        summary.kind = "other"  # type: ignore[misc]


def test_format_summary_converts_datetimes_and_does_not_mutate() -> None:
    summary = Summary(
        kind="upcoming_appointments",
        items=(
            Item(
                "Office visit",
                Citation("Appointment", "a1"),
                at=datetime(2026, 12, 8, 19, 30, tzinfo=UTC),
            ),
            Item("Lab", Citation("Observation", "o1"), at=date(2026, 12, 8)),
        ),
    )
    before = summary.items
    text = format_summary(summary, tz="America/New_York")
    assert summary.items == before
    assert "Tue Dec 8, 2026, 2:30 PM EST" in text
    assert "Dec 8, 2026" in text
    assert "Dec 7, 2026" not in text
