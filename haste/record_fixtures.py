"""Record de-identified Haste responses into tests/fixtures/haste.

Uses the patient TokenSet that `SAVE_TOKENS=1 just smoke` wrote. Run with
`uv run python -m haste.record_fixtures`. The patient id becomes
`example-patient`, `meta` and Bundle links are dropped, and the output is
checked for token material before it is written.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from aiofhir.scrub import scrub
from haste.env import ROOT, start_script
from haste.login import load_tokens

_ROOT = ROOT
OUT = _ROOT / "tests" / "fixtures" / "haste"
EXAMPLE_PATIENT = "example-patient"
# File name -> (path under the FHIR base, search params). "{patient}" is replaced.
SEARCHES: dict[str, tuple[str, dict[str, str]]] = {
    "appointment-search.json": ("Appointment", {"patient": "{patient}"}),
    "medicationrequest-search.json": ("MedicationRequest", {"patient": "{patient}"}),
    "medication-med-remote.json": ("Medication/med-remote", {}),
    "allergyintolerance-search.json": ("AllergyIntolerance", {"patient": "{patient}"}),
    "condition-search.json": ("Condition", {"patient": "{patient}"}),
    "observation-search.json": (
        "Observation",
        {"patient": "{patient}", "category": "laboratory"},
    ),
    "vital-signs-search.json": (
        "Observation",
        {"patient": "{patient}", "category": "vital-signs"},
    ),
    "immunization-search.json": ("Immunization", {"patient": "{patient}"}),
    "encounter-search.json": ("Encounter", {"patient": "{patient}"}),
    "procedure-search.json": ("Procedure", {"patient": "{patient}"}),
    "documentreference-search.json": ("DocumentReference", {"patient": "{patient}"}),
}


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in ("meta", "link", "fullUrl")}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def deidentify(body: Any, patient_id: str) -> str:
    text = json.dumps(_strip(body), indent=2, sort_keys=True).replace(patient_id, EXAMPLE_PATIENT)
    return scrub(text) + "\n"


async def _main() -> None:
    start_script()
    base = os.environ.get("BASE", "").strip().rstrip("/")
    path = _ROOT / "haste" / ".tokens.json"
    if not base or not path.exists():
        print("Set BASE and run SAVE_TOKENS=1 just smoke first.", file=sys.stderr)
        raise SystemExit(1)
    tokens = load_tokens(path)
    if not tokens.patient:
        raise SystemExit("the saved token has no patient")
    headers = {"Authorization": f"Bearer {tokens.access_token}", "Accept": "application/fhir+json"}
    secrets = [tokens.access_token, tokens.refresh_token or "", tokens.id_token or ""]
    outputs: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30.0) as http:
        config = await http.get(f"{base}/.well-known/smart-configuration")
        config.raise_for_status()
        outputs["smart-configuration.json"] = deidentify(config.json(), tokens.patient)
        for name, (resource, params) in SEARCHES.items():
            query = {k: v.replace("{patient}", tokens.patient) for k, v in params.items()}
            response = await http.get(f"{base}/{resource}", params=query, headers=headers)
            response.raise_for_status()
            outputs[name] = deidentify(response.json(), tokens.patient)
    for name, text in outputs.items():
        if tokens.patient in text or any(s and s in text for s in secrets):
            raise SystemExit(f"{name} still contains the patient id or a token")
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (OUT / name).write_text(text)
        print(f"wrote tests/fixtures/haste/{name}")


if __name__ == "__main__":
    asyncio.run(_main())
