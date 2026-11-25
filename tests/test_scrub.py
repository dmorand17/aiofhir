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


def test_recorded_fixtures_have_no_token_material() -> None:
    files = sorted(FIXTURE.parent.glob("*.json"))
    assert len(files) > 1
    for path in files:
        text = path.read_text()
        assert scrub(text) == text, path.name
        assert "access_token" not in text, path.name
        assert "Bearer " not in text, path.name
