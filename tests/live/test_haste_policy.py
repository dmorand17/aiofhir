"""Opt-in live checks for H1: the seed patient reaches only their own record.

Skipped unless AIOFHIR_LIVE=1. Each test needs a little more set up:

* ``test_seed_client_requires_its_secret``: BASE only. A token request for
  aiofhir-seed without a secret must fail with ``invalid_client``.
* ``test_policy_matrix_through_evaluate_policy``: HASTE_TOKEN (an admin or
  owner token, for example from ``SAVE_HASTE_TOKEN=1 just seed``), PATIENT_ID
  and SEED_USER_ID. Runs every case through ``$evaluate-policy``; nothing is
  written.
* ``test_patient_scoped_token_reaches_only_the_patient``: ``haste/.tokens.json``
  from ``SAVE_TOKENS=1 just smoke``, as for tests/live/test_haste.py.
* ``test_user_scoped_patient_token_reaches_only_the_patient``:
  ``haste/.user-tokens.json`` from ``SAVE_TOKENS=1 just check-policy-as-patient``,
  a ``user/*.rs`` token the patient user got through aiofhir-seed.

Only GET requests are sent with patient tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

from aiofhir.profiles import HasteProfile
from aiofhir.smart.discovery import discover
from haste.login import load_tokens
from haste.policy_check import (
    client_is_confidential,
    evaluate_cases,
    fetch_policy_allows_search,
    find_appointment,
    find_other_observation,
    format_results,
    policy_cases,
    probe_cases,
)
from haste.seeding import SEED_CLIENT_ID

_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("AIOFHIR_LIVE") != "1",
    reason="Set AIOFHIR_LIVE=1 to run the live Haste checks.",
)


def _env(name: str, *, skip_hint: str | None = None) -> str:
    load_dotenv(_ROOT / ".env")
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if skip_hint:
        pytest.skip(f"{name} is not set; {skip_hint}")
    pytest.fail(f"AIOFHIR_LIVE=1 requires {name}")


def _token_file(env_name: str, default: str, hint: str) -> Path:
    raw = os.environ.get(env_name, "").strip()
    path = Path(raw) if raw else _ROOT / "haste" / default
    if not path.exists():
        pytest.skip(f"{path} is missing; {hint}")
    return path


async def test_seed_client_requires_its_secret() -> None:
    base = _env("BASE")
    async with httpx.AsyncClient(timeout=30.0) as http:
        configuration = await discover(base, HasteProfile(), http)
        confidential = await client_is_confidential(
            http, configuration.token_endpoint, SEED_CLIENT_ID
        )
    assert confidential, f"{SEED_CLIENT_ID} is public; run `just seed` to lock it"


async def test_policy_matrix_through_evaluate_policy() -> None:
    base = _env("BASE")
    token = _env("HASTE_TOKEN", skip_hint="use SAVE_HASTE_TOKEN=1 just seed")
    patient_id = _env("PATIENT_ID")
    user_id = _env("SEED_USER_ID", skip_hint="set it to the patient User id")
    async with httpx.AsyncClient(timeout=30.0) as http:
        allow_search = await fetch_policy_allows_search(http, base, token)
        assert allow_search is not None, "the seed policy is missing; run just seed"
        other = await find_other_observation(http, base, token, patient_id)
        appointment = await find_appointment(http, base, token, patient_id)
        cases = policy_cases(
            patient_id,
            other_observation=other,
            appointment_id=appointment,
            allow_search=allow_search,
        )
        results = await evaluate_cases(http, base, token, user_id=user_id, cases=cases)
    failed = [result for result in results if not result.passed]
    assert not failed, format_results(failed)


async def _probe(path: Path) -> None:
    base = _env("BASE")
    patient_id = _env("PATIENT_ID")
    # Searches allowed or not changes what a denied search looks like: keep
    # SEED_ALLOW_SEARCH set the way the seed ran.
    allow_search = os.environ.get("SEED_ALLOW_SEARCH", "").strip() == "1"
    tokens = load_tokens(path)
    async with httpx.AsyncClient(timeout=30.0) as http:
        results = await probe_cases(
            http,
            base,
            tokens.access_token,
            patient_id=patient_id,
            cases=policy_cases(patient_id, allow_search=allow_search),
        )
    failed = [result for result in results if not result.passed]
    assert not failed, format_results(failed)


async def test_patient_scoped_token_reaches_only_the_patient() -> None:
    await _probe(_token_file("AIOFHIR_LIVE_TOKENS", ".tokens.json", "run SAVE_TOKENS=1 just smoke"))


async def test_user_scoped_patient_token_reaches_only_the_patient() -> None:
    await _probe(
        _token_file(
            "AIOFHIR_LIVE_USER_TOKENS",
            ".user-tokens.json",
            "run SAVE_TOKENS=1 just check-policy-as-patient",
        )
    )
