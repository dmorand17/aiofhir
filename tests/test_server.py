"""The aiofhir[server] reference backend, end to end over ASGI."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from aiofhir.server import EncryptedStore, create_app
from aiofhir.smart import TokenSet
from aiofhir.smart.discovery import clear_discovery_cache

FHIR = "https://fhir.example/r4"
TOKEN_URL = "https://auth.example/token"
BACKEND = "http://backend"
SERVICE = "service-secret"
SUBJECT = "user:42"


def _config() -> dict[str, object]:
    return {
        "authorization_endpoint": "https://auth.example/authorize",
        "token_endpoint": TOKEN_URL,
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["permission-patient", "permission-v2", "launch-standalone"],
        "scopes_supported": ["patient/*.rs", "launch/patient", "offline_access"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _token_body(access: str, refresh: str, expires_in: int = 3600) -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "patient": "pt-1",
        "scope": "patient/*.rs launch/patient offline_access",
    }


def _tokens(access: str = "access-1", expires_in: int = 3600) -> TokenSet:
    return TokenSet(
        access_token=access,
        refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scope="patient/*.rs launch/patient offline_access",
        patient="pt-1",
        encounter=None,
        need_patient_banner=None,
        id_token=None,
        fhir_user=None,
    )


@pytest.fixture(autouse=True)
def _fresh_discovery() -> None:
    clear_discovery_cache()


@pytest.fixture
def store() -> EncryptedStore:
    return EncryptedStore(Fernet.generate_key())


@pytest.fixture
async def backend(store: EncryptedStore) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        fhir_base_url=FHIR,
        client_id="aiofhir-app",
        redirect_uri=f"{BACKEND}/auth/callback",
        service_token=SERVICE,
        store=store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BACKEND) as client:
        yield client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE}"}


def _mock_fhir(router: respx.MockRouter) -> None:
    router.get(f"{FHIR}/.well-known/smart-configuration").respond(200, json=_config())
    router.get(f"{FHIR}/MedicationRequest").respond(
        200,
        json={
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "mr-1",
                        "status": "active",
                        "subject": {"reference": "Patient/pt-1"},
                        "medicationCodeableConcept": {"text": "Lisinopril"},
                    }
                }
            ],
        },
    )
    router.get(f"{FHIR}/Appointment").respond(200, json={"resourceType": "Bundle", "entry": []})


async def _sign_in(backend: httpx.AsyncClient) -> str:
    started = await backend.post("/auth/start", json={"subject": SUBJECT}, headers=_auth())
    assert started.status_code == 200
    url = str(started.json()["authorize_url"])
    return parse_qs(urlsplit(url).query)["state"][0]


# EncryptedStore


def test_store_encrypts_tokens_and_hides_the_subject(store: EncryptedStore) -> None:
    backing: dict[str, bytes] = {}
    store = EncryptedStore(Fernet.generate_key(), backing)
    store.put(SUBJECT, _tokens())
    assert store.get(SUBJECT) is not None
    assert store.get(SUBJECT).access_token == "access-1"  # type: ignore[union-attr]
    raw = b"".join(backing.values())
    assert b"access-1" not in raw and b"refresh-1" not in raw
    assert all(SUBJECT not in key for key in backing)
    store.delete(SUBJECT)
    assert store.get(SUBJECT) is None and backing == {}


def test_store_with_another_key_reads_nothing() -> None:
    backing: dict[str, bytes] = {}
    EncryptedStore(Fernet.generate_key(), backing).put(SUBJECT, _tokens())
    assert EncryptedStore(Fernet.generate_key(), backing).get(SUBJECT) is None


# Sign-in


async def test_start_requires_the_service_bearer(backend: httpx.AsyncClient) -> None:
    missing = await backend.post("/auth/start", json={"subject": SUBJECT})
    wrong = await backend.post(
        "/auth/start", json={"subject": SUBJECT}, headers={"Authorization": "Bearer nope"}
    )
    assert missing.status_code == 401 and wrong.status_code == 401


async def test_sign_in_stores_the_tokens_under_the_subject(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        router.post(TOKEN_URL).respond(200, json=_token_body("access-1", "refresh-1"))
        state = await _sign_in(backend)
        callback = await backend.get("/auth/callback", params={"code": "c1", "state": state})
    assert callback.status_code == 200
    saved = store.get(SUBJECT)
    assert saved is not None and saved.patient == "pt-1"


async def test_state_is_single_use(backend: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        token = router.post(TOKEN_URL).respond(200, json=_token_body("access-1", "refresh-1"))
        state = await _sign_in(backend)
        first = await backend.get("/auth/callback", params={"code": "c1", "state": state})
        again = await backend.get("/auth/callback", params={"code": "c1", "state": state})
    assert first.status_code == 200
    assert again.status_code == 400
    assert token.call_count == 1


async def test_unknown_state_is_refused(backend: httpx.AsyncClient) -> None:
    response = await backend.get("/auth/callback", params={"code": "c1", "state": "made-up"})
    assert response.status_code == 400


async def test_expired_state_is_refused(store: EncryptedStore) -> None:
    now = [1000.0]
    app = create_app(
        fhir_base_url=FHIR,
        client_id="aiofhir-app",
        redirect_uri=f"{BACKEND}/auth/callback",
        service_token=SERVICE,
        store=store,
        monotonic=lambda: now[0],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BACKEND
    ) as client:
        with respx.mock(assert_all_called=False) as router:
            _mock_fhir(router)
            state = await _sign_in(client)
            now[0] += 601
            response = await client.get("/auth/callback", params={"code": "c1", "state": state})
    assert response.status_code == 400


# Record


async def test_record_returns_a_summary_for_the_subject(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    store.put(SUBJECT, _tokens())
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        response = await backend.post(
            "/v1/record/active_medications", json={"subject": SUBJECT}, headers=_auth()
        )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "active_medications"
    assert body["items"][0]["text"] == "Lisinopril"
    assert body["items"][0]["id"] == "mr-1"


async def test_record_requires_the_service_bearer(backend: httpx.AsyncClient) -> None:
    response = await backend.post("/v1/record/allergies", json={"subject": SUBJECT})
    assert response.status_code == 401


async def test_unknown_subject_is_auth_expired(backend: httpx.AsyncClient) -> None:
    response = await backend.post(
        "/v1/record/allergies", json={"subject": "user:nobody"}, headers=_auth()
    )
    assert response.status_code == 401
    assert response.json() == {"error": "auth_expired"}


async def test_only_allowlisted_methods_and_arguments(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    store.put(SUBJECT, _tokens())
    search_raw = await backend.post(
        "/v1/record/search_raw", json={"subject": SUBJECT}, headers=_auth()
    )
    patient_arg = await backend.post(
        "/v1/record/allergies",
        json={"subject": SUBJECT, "args": {"patient": "pt-2"}},
        headers=_auth(),
    )
    bad_days = await backend.post(
        "/v1/record/upcoming_appointments",
        json={"subject": SUBJECT, "args": {"days": 100000}},
        headers=_auth(),
    )
    assert search_raw.status_code == 404
    assert patient_arg.status_code == 422
    assert bad_days.status_code == 422


async def test_refresh_is_saved_back_to_the_store(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    store.put(SUBJECT, _tokens(expires_in=10))
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        refresh = router.post(TOKEN_URL).respond(200, json=_token_body("access-2", "refresh-2"))
        response = await backend.post(
            "/v1/record/active_medications", json={"subject": SUBJECT}, headers=_auth()
        )
    assert response.status_code == 200
    assert refresh.call_count == 1
    saved = store.get(SUBJECT)
    assert saved is not None and saved.refresh_token == "refresh-2"


async def test_a_dead_refresh_token_disconnects_the_subject(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    store.put(SUBJECT, _tokens(expires_in=10))
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        router.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        response = await backend.post(
            "/v1/record/allergies", json={"subject": SUBJECT}, headers=_auth()
        )
    assert response.status_code == 401
    assert response.json() == {"error": "auth_expired"}
    assert store.get(SUBJECT) is None


async def test_disconnect_deletes_the_subject(
    backend: httpx.AsyncClient, store: EncryptedStore
) -> None:
    store.put(SUBJECT, _tokens())
    response = await backend.post("/auth/disconnect", json={"subject": SUBJECT}, headers=_auth())
    assert response.status_code == 204
    assert store.get(SUBJECT) is None


async def test_no_token_reaches_the_logs(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        router.post(TOKEN_URL).respond(200, json=_token_body("access-zzz", "refresh-zzz"))
        state = await _sign_in(backend)
        await backend.get("/auth/callback", params={"code": "c1", "state": state})
        await backend.post(
            "/v1/record/active_medications", json={"subject": SUBJECT}, headers=_auth()
        )
    # httpx's own INFO lines print full URLs (the callback's state, the patient id
    # in FHIR searches). `python -m aiofhir.server` keeps httpx at WARNING; see
    # test_serve_logging_silences_httpx_and_access_logs.
    ours = [r for r in caplog.records if r.name.startswith("aiofhir")]
    text = "\n".join(r.getMessage() for r in ours)
    assert ours
    for secret in ("access-zzz", "refresh-zzz", SERVICE, state, SUBJECT, "pt-1"):
        assert secret not in text


def test_serve_logging_silences_httpx_and_access_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiofhir.server import __main__ as serve

    calls: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(kw))
    env = {
        "AIOFHIR_FHIR_BASE": FHIR,
        "AIOFHIR_CLIENT_ID": "aiofhir-app",
        "AIOFHIR_REDIRECT_URI": f"{BACKEND}/auth/callback",
        "AIOFHIR_SERVICE_TOKEN": SERVICE,
        "AIOFHIR_FERNET_KEY": Fernet.generate_key().decode(),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    serve.main()
    assert calls["access_log"] is False
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# Why a sign-in failed, in the logs


def _callback_log(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("aiofhir"))


async def test_a_refused_code_exchange_logs_the_servers_error(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        router.post(TOKEN_URL).respond(
            401, json={"error": "invalid_client", "error_description": "Invalid credentials"}
        )
        state = await _sign_in(backend)
        response = await backend.get("/auth/callback", params={"code": "c1", "state": state})
    assert response.status_code == 400
    text = _callback_log(caplog)
    assert "code exchange failed: TokenError" in text
    assert "invalid_client" in text and "Invalid credentials" in text
    assert state not in text and SUBJECT not in text


async def test_a_token_without_a_patient_logs_why(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    body = _token_body("access-1", "refresh-1")
    del body["patient"]
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        router.post(TOKEN_URL).respond(200, json=body)
        state = await _sign_in(backend)
        await backend.get("/auth/callback", params={"code": "c1", "state": state})
    assert "did not include a patient" in _callback_log(caplog)


async def test_an_unknown_state_is_logged(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await backend.get("/auth/callback", params={"code": "c1", "state": "made-up"})
    text = _callback_log(caplog)
    assert "unknown, used or expired state" in text
    assert "made-up" not in text


async def test_an_authorization_error_is_logged(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        state = await _sign_in(backend)
        await backend.get(
            "/auth/callback",
            params={
                "state": state,
                "error": "access_denied",
                "error_description": "The user denied access",
            },
        )
    text = _callback_log(caplog)
    assert "access_denied" in text and "The user denied access" in text


async def test_a_callback_without_a_code_is_logged(
    backend: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with respx.mock(assert_all_called=False) as router:
        _mock_fhir(router)
        state = await _sign_in(backend)
        await backend.get("/auth/callback", params={"state": state})
    assert "no authorization code" in _callback_log(caplog)
