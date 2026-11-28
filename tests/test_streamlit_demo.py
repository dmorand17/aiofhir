"""The Streamlit demo, run headlessly. Skipped unless the `demo` group is installed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import respx

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from aiofhir.smart import TokenSet  # noqa: E402
from aiofhir.smart.discovery import clear_discovery_cache  # noqa: E402

APP = str(Path(__file__).parents[1] / "examples" / "streamlit_app.py")
FIXTURES = Path(__file__).parent / "fixtures" / "haste"
_STATEMENT = json.loads((FIXTURES.parent / "capability-statement.json").read_text())
BASE = "https://fhir.example/r4"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_discovery_cache()
    monkeypatch.setenv("BASE", BASE)
    monkeypatch.setenv("AIOFHIR_PROFILE", "generic")
    monkeypatch.setenv("SMOKE_CLIENT_ID", "demo-app")
    monkeypatch.setenv("SMOKE_CLIENT_SECRET", "")
    monkeypatch.setenv("DEMO_REDIRECT_URI", "http://localhost:8765/callback")


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_starting_sign_in_keeps_state_on_the_server() -> None:
    with respx.mock:
        respx.get(f"{BASE}/.well-known/smart-configuration").respond(
            200, json=_fixture("smart-configuration.json")
        )
        at = AppTest.from_file(APP, default_timeout=30).run()
        assert not at.exception
        [start] = [b for b in at.button if b.label == "Start sign-in"]
        start.click().run()
    assert not at.exception
    url = at.session_state["authorize_url"]
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["demo-app"]
    assert query["redirect_uri"] == ["http://localhost:8765/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert "launch/patient" in query["scope"][0].split()


def test_a_signed_in_session_searches_every_patient_type() -> None:
    tokens = TokenSet(
        access_token="access",
        refresh_token="refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="patient/*.rs launch/patient",
        patient="example-patient",
        encounter=None,
        need_patient_banner=None,
        id_token=None,
        fhir_user=None,
    )
    with respx.mock:
        respx.get(f"{BASE}/metadata").respond(200, json=_STATEMENT)
        for resource in (
            "Appointment",
            "MedicationRequest",
            "AllergyIntolerance",
            "Condition",
            "Observation",
            "Immunization",
            "Encounter",
            "Procedure",
            "DocumentReference",
        ):
            respx.get(f"{BASE}/{resource}").respond(
                200, json=_fixture(f"{resource.lower()}-search.json")
            )
        patient = respx.get(f"{BASE}/Patient").respond(
            200, json={"resourceType": "Bundle", "entry": [{"resource": _fixture("patient.json")}]}
        )
        at = AppTest.from_file(APP, default_timeout=30)
        at.session_state["tokens"] = tokens
        at.run()
        [load] = [b for b in at.button if b.label == "Load the record"]
        load.click().run()
    assert not at.exception
    assert patient.calls.last.request.url.params["_id"] == "example-patient"
    assert any("Pat Example" in str(header.value) for header in at.subheader)
    [picker] = [box for box in at.selectbox if box.label == "Resource type"]
    assert list(picker.options) == [
        "AllergyIntolerance (1)",
        "Appointment (6)",
        "Condition (1)",
        "DocumentReference (1)",
        "Encounter (1)",
        "Immunization (1)",
        "MedicationRequest (4)",
        "Observation (1)",
        "Procedure (1)",
    ]


def test_opening_another_patient_is_blocked_by_aiofhir_and_the_server() -> None:
    tokens = TokenSet(
        access_token="access",
        refresh_token="refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="patient/*.rs launch/patient",
        patient="example-patient",
        encounter=None,
        need_patient_banner=None,
        id_token=None,
        fhir_user=None,
    )
    with respx.mock:
        read = respx.get(f"{BASE}/Patient/other-1").respond(404)
        search = respx.get(f"{BASE}/Observation").respond(
            200, json={"resourceType": "Bundle", "entry": []}
        )
        unscoped = respx.get(f"{BASE}/Patient").respond(500)  # search_raw must not get here
        at = AppTest.from_file(APP, default_timeout=30)
        at.session_state["tokens"] = tokens
        at.run()
        [other] = [t for t in at.text_input if t.label == "Another patient's id"]
        other.input("other-1").run()  # the button enables once an id is entered
        [probe] = [b for b in at.button if b.label == "Try to open this patient"]
        probe.click().run()
    assert not at.exception
    assert read.called and search.called and not unscoped.called
    assert search.calls.last.request.url.params["patient"] == "other-1"
    rows = at.session_state["probe"]
    assert [row["blocked"] for row in rows] == [True, True, True]
    assert [row["layer"] for row in rows] == ["aiofhir", "FHIR server", "FHIR server"]
