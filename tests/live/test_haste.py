"""Opt-in checks against the seeded Haste project.

Run `SAVE_TOKENS=1 just smoke` first, signed in as the seeded patient user.
That writes haste/.tokens.json. Then run `AIOFHIR_LIVE=1 just test tests/live`.
BASE, SMOKE_CLIENT_ID and SMOKE_CLIENT_SECRET come from the environment or the
repo-root .env. AIOFHIR_LIVE_TOKENS overrides the token file path. Rotated
refresh tokens are written back to that file.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from aiofhir import RECORD_HELPERS, FhirClient, PatientRecord
from aiofhir.display import format_summary
from aiofhir.profiles import HasteProfile
from aiofhir.smart import RefreshingToken, SmartClient, TokenSet
from haste.login import REDIRECT_URI, load_tokens, save_tokens
from haste.seeding import CLIENT_ID, SEED_SYSTEM

_ROOT = Path(__file__).resolve().parents[2]
_HELPERS = RECORD_HELPERS

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOFHIR_LIVE") != "1",
    reason="Set AIOFHIR_LIVE=1 to run the live Haste checks.",
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"AIOFHIR_LIVE=1 requires {name}")
    return value


def _tokens_path() -> Path:
    raw = os.environ.get("AIOFHIR_LIVE_TOKENS", "").strip()
    path = Path(raw) if raw else _ROOT / "haste" / ".tokens.json"
    if not path.exists():
        pytest.fail(f"{path} is missing. Run SAVE_TOKENS=1 just smoke first.")
    return path


@asynccontextmanager
async def _session() -> AsyncIterator[tuple[FhirClient, PatientRecord, TokenSet]]:
    load_dotenv(_ROOT / ".env")
    base = _required("BASE")
    path = _tokens_path()
    tokens = load_tokens(path)

    async def persist(rotated: TokenSet) -> None:
        save_tokens(path, rotated)

    smart = SmartClient(
        base,
        client_id=os.environ.get("SMOKE_CLIENT_ID", "").strip() or CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        client_secret=os.environ.get("SMOKE_CLIENT_SECRET", "").strip() or None,
        profile=HasteProfile(),
    )
    holder = RefreshingToken(smart, tokens, on_refresh=persist)
    async with FhirClient(base, holder, profile=HasteProfile()) as fhir:
        patient = tokens.patient or ""
        yield fhir, PatientRecord(fhir, patient_id=patient), tokens


def _references(resource: dict[str, Any]) -> list[str]:
    """Every patient-pointing reference on the resource types the helpers return."""
    found: list[str] = []
    for key in ("subject", "patient"):
        value = resource.get(key)
        if isinstance(value, dict) and isinstance(value.get("reference"), str):
            found.append(value["reference"])
    for participant in resource.get("participant") or []:
        actor = participant.get("actor") if isinstance(participant, dict) else None
        if isinstance(actor, dict) and isinstance(actor.get("reference"), str):
            found.append(actor["reference"])
    return found


def _belongs_to(resource: dict[str, Any], patient_id: str) -> bool:
    target = f"Patient/{patient_id}"
    return any(ref == target or ref.endswith(f"/{target}") for ref in _references(resource))


async def test_token_response_names_a_patient() -> None:
    async with _session() as (_fhir, _record, tokens):
        assert tokens.patient, "the token response has no patient; sign in as the patient user"


async def test_every_helper_item_is_cited_and_belongs_to_the_patient() -> None:
    async with _session() as (fhir, record, tokens):
        assert tokens.patient
        for name in _HELPERS:
            summary = await getattr(record, name)()
            assert summary.items, f"{name} returned nothing; is the seed loaded?"
            for item in summary.items:
                cited = await fhir.read(item.citation.resource_type, item.citation.id)
                assert _belongs_to(cited, tokens.patient), f"{name} cited {item.citation}"
            for text in (summary.to_text(), format_summary(summary, tz="UTC")):
                assert tokens.access_token not in text
                if tokens.refresh_token:
                    assert tokens.refresh_token not in text


async def test_cancelled_appointment_is_filtered_out() -> None:
    async with _session() as (_fhir, record, _tokens):
        found = await record.search_raw(
            "Appointment", {"identifier": f"{SEED_SYSTEM}|appt-cancelled"}
        )
        assert len(found.resources) == 1, "the seeded cancelled appointment is missing"
        cancelled = str(found.resources[0]["id"])
        upcoming = await record.upcoming_appointments()
    assert cancelled not in {item.citation.id for item in upcoming.items}


async def test_paging_follows_next_links_and_terminates() -> None:
    async with _session() as (fhir, _record, tokens):
        assert tokens.patient
        result = await fhir.search(
            "Appointment", {"patient": tokens.patient, "_count": "1"}, max_pages=50
        )
    ids = [str(resource.get("id")) for resource in result.resources]
    assert not result.truncated, "paging hit the 50-page cap"
    assert len(ids) >= 2, "expected several one-item pages from the seeded appointments"
    assert len(ids) == len(set(ids)), "a page repeated a resource"
    for resource in result.resources:
        assert _belongs_to(resource, tokens.patient)
