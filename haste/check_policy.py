"""Check the seed patient's access policy and the admin seed client on a Haste project.

    uv run python -m haste.check_policy               # just check-policy
    uv run python -m haste.check_policy --as-patient  # just check-policy-as-patient

Without flags, nothing signs in as the patient and nothing is written:

1. the token endpoint must refuse ``aiofhir-seed`` without its secret
   (``invalid_client``);
2. every case in ``haste.policy_check.policy_cases`` goes through
   ``$evaluate-policy`` as ``SEED_USER_ID`` and must be granted or denied as
   listed. This needs an admin token: ``HASTE_TOKEN``, or a browser sign-in
   with ``CLIENT_ID``/``CLIENT_SECRET`` as for the seed.

``--as-patient`` also signs in **as the patient user** through ``aiofhir-seed``
with ``CLIENT_SECRET``, asking for ``user/*.rs`` (the widest read token the
patient could get), and sends every GET case for real. Writes are never sent.
``SAVE_TOKENS=1`` writes that token to ``haste/.user-tokens.json`` (mode 600,
gitignored) for ``tests/live/test_haste_policy.py``. It has no refresh token.

Reads BASE, PATIENT_ID and SEED_USER_ID from the environment or ``.env``.
Exits 1 when any check fails. Tokens are never printed.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from aiofhir.profiles import HasteProfile
from aiofhir.smart.discovery import discover
from haste.env import ROOT, start_script
from haste.login import login_tokens, save_tokens
from haste.policy_check import (
    client_is_confidential,
    describe_claims,
    evaluate_cases,
    fetch_policy_allows_search,
    find_appointment,
    find_other_observation,
    format_results,
    gap_count,
    patient_token_problem,
    policy_cases,
    probe_cases,
    token_claims,
)
from haste.seed import admin_token
from haste.seeding import READ_POLICY_ID, SEED_CLIENT_ID, AdminTokenError

_ROOT = ROOT
USER_TOKENS_PATH = _ROOT / "haste" / ".user-tokens.json"
PATIENT_USER_SCOPE = "user/*.rs openid fhirUser"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Set {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


async def _main(as_patient: bool) -> int:
    start_script()
    base = _env("BASE").rstrip("/")
    patient_id = _env("PATIENT_ID")
    user_id = _env("SEED_USER_ID")
    failed = False
    async with httpx.AsyncClient(timeout=30.0) as http:
        configuration = await discover(base, HasteProfile(), http)
        confidential = await client_is_confidential(
            http, configuration.token_endpoint, SEED_CLIENT_ID
        )
        print(
            f"{'ok  ' if confidential else 'FAIL'} {SEED_CLIENT_ID} "
            f"{'requires its secret' if confidential else 'is PUBLIC: run just seed to lock it'}"
        )
        failed |= not confidential

        token = await admin_token()
        print(f"admin token: {describe_claims(token_claims(token))}")
        allow_search = await fetch_policy_allows_search(http, base, token)
        if allow_search is None:
            print(f"FAIL AccessPolicyV2/{READ_POLICY_ID} is missing: run just seed")
            return 1
        print(
            "searches: allowed by type (SEED_ALLOW_SEARCH=1); GAP marks searches only a "
            "patient/ scope confines"
            if allow_search
            else "searches: denied (reads by id only)"
        )
        other = await find_other_observation(http, base, token, patient_id)
        appointment = await find_appointment(http, base, token, patient_id)
        cases = policy_cases(
            patient_id,
            other_observation=other,
            appointment_id=appointment,
            allow_search=allow_search,
        )
        if other is None:
            print("note: no other patient's Observation found; that read case is skipped")
        evaluated = await evaluate_cases(http, base, token, user_id=user_id, cases=cases)
        print(f"\n$evaluate-policy as User/{user_id}:")
        print(format_results(evaluated))
        failed |= not all(result.passed for result in evaluated)
        if gaps := gap_count(evaluated):
            print(
                f"note: {gaps} searches are allowed although they are not the patient's own. "
                "The policy cannot see search parameters; only a patient/ scope confines them."
            )

        if as_patient:
            print(
                f"\nSign in AS THE PATIENT USER (private window) through {SEED_CLIENT_ID} "
                f"with scope {PATIENT_USER_SCOPE!r}."
            )
            tokens = await login_tokens(
                base,
                SEED_CLIENT_ID,
                _env("CLIENT_SECRET"),
                scope=PATIENT_USER_SCOPE,
            )
            claims = token_claims(tokens.access_token)
            print(f"patient token: {describe_claims(claims)}")
            problem = patient_token_problem(claims, user_id=user_id, patient_id=patient_id)
            if problem:
                print(f"FAIL not the patient user's member token: {problem}.")
                print(
                    "No requests were sent. Chrome incognito windows share one session, so use "
                    "a fresh browser profile for the patient (see haste/README.md)."
                )
                return 1
            if os.environ.get("SAVE_TOKENS", "").strip() == "1":
                save_tokens(USER_TOKENS_PATH, tokens)
                print(f"Saved the patient user token to {USER_TOKENS_PATH.relative_to(_ROOT)}.")
            probed = await probe_cases(
                http, base, tokens.access_token, patient_id=patient_id, cases=cases
            )
            print(f"\nReal requests with the patient user's {PATIENT_USER_SCOPE!r} token:")
            print(format_results(probed))
            failed |= not all(result.passed for result in probed)
    print("\nFAILED" if failed else "\nall checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    flags = sys.argv[1:]
    if flags not in ([], ["--as-patient"]):
        raise SystemExit("usage: python -m haste.check_policy [--as-patient]")
    try:
        raise SystemExit(asyncio.run(_main(as_patient=flags == ["--as-patient"])))
    except AdminTokenError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
