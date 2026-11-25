from __future__ import annotations

from pathlib import Path

from aiofhir.scrub import scrub

FIXTURE = Path(__file__).parent / "fixtures" / "haste" / "patient.json"


def test_scrub_removes_token_fields_and_bearer_values() -> None:
    raw = '{"access_token": "access-secret", "note": "Bearer access-secret"}'
    cleaned = scrub(raw)
    assert "access-secret" not in cleaned
    assert '"access_token": "[redacted]"' in cleaned
    assert "Bearer [redacted]" in cleaned


def test_recorded_fixture_has_no_token_material() -> None:
    text = FIXTURE.read_text()
    assert scrub(text) == text
    assert "access_token" not in text
    assert "Bearer " not in text
