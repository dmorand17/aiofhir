"""A 401 on an admin-token request says the saved HASTE_TOKEN expired, without the token."""

from __future__ import annotations

import httpx
import pytest
import respx

from haste import seed
from haste.policy_check import evaluate_case, find_appointment, policy_cases
from haste.seeding import (
    SEED_CLIENT_ID,
    AdminTokenError,
    _post_bundle,
    delete_seed_client,
    raise_if_token_rejected,
    secure_seed_client,
)

BASE = "http://h.example/fhir"
TOKEN = "expired-admin-token-value"


def _assert_helpful(exc: pytest.ExceptionInfo[AdminTokenError]) -> None:
    message = str(exc.value)
    assert "401" in message
    assert "HASTE_TOKEN" in message
    assert "expired" in message
    assert "remove" in message
    assert TOKEN not in message


async def test_bundle_post_401_explains_the_expired_token() -> None:
    with respx.mock as mock:
        mock.post(BASE).respond(401, json={"resourceType": "OperationOutcome"})
        async with httpx.AsyncClient() as http:
            with pytest.raises(AdminTokenError, match="post a bundle") as exc:
                await _post_bundle(http, BASE, TOKEN, {"resourceType": "Bundle", "entry": []})
    _assert_helpful(exc)


async def test_bundle_post_other_errors_keep_the_status() -> None:
    with respx.mock as mock:
        mock.post(BASE).respond(403)
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match=r"bundle post failed \(403\)") as exc:
                await _post_bundle(http, BASE, TOKEN, {"resourceType": "Bundle", "entry": []})
    assert not isinstance(exc.value, AdminTokenError)


async def test_seed_client_helpers_explain_a_401() -> None:
    url = f"{BASE}/ClientApplication/{SEED_CLIENT_ID}"
    with respx.mock as mock:
        mock.get(url).respond(401)
        mock.delete(url).respond(401)
        async with httpx.AsyncClient() as http:
            with pytest.raises(AdminTokenError) as exc:
                await secure_seed_client(
                    http, BASE, TOKEN, new_secret="s", on_secret=lambda _: None
                )
            _assert_helpful(exc)
            with pytest.raises(AdminTokenError):
                await delete_seed_client(http, BASE, TOKEN)


async def test_policy_check_helpers_explain_a_401() -> None:
    case = policy_cases("p1", other_observation=None, appointment_id=None)[0]
    with respx.mock as mock:
        mock.post(url__regex=r".*\$evaluate-policy$").respond(401)
        mock.get(f"{BASE}/Appointment").respond(401)
        async with httpx.AsyncClient() as http:
            with pytest.raises(AdminTokenError) as exc:
                await evaluate_case(http, BASE, TOKEN, user_id="u1", case=case)
            _assert_helpful(exc)
            with pytest.raises(AdminTokenError):
                await find_appointment(http, BASE, TOKEN, "p1")


def test_raise_if_token_rejected_ignores_other_statuses() -> None:
    for status in (200, 403, 404, 500):
        raise_if_token_rejected(httpx.Response(status), "do a thing")


def test_seed_main_prints_the_message_instead_of_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def rejected() -> None:
        raise AdminTokenError("Haste rejected the admin token (401) while trying to x.")

    monkeypatch.setattr(seed, "_main", rejected)
    with pytest.raises(SystemExit) as exit_info:
        seed.main([])
    assert exit_info.value.code == 1
    assert "rejected the admin token" in capsys.readouterr().err


def test_seed_main_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit, match="usage"):
        seed.main(["--nope"])
