from __future__ import annotations

import pytest

from aiofhir.errors import DiscoveryError
from aiofhir.smart.discovery import SmartConfiguration
from aiofhir.smart.scopes import scopes_from_capabilities


def _config(
    capabilities: list[str],
    scopes: list[str] | None,
) -> SmartConfiguration:
    return SmartConfiguration(
        authorization_endpoint="http://auth.example/authorize",
        token_endpoint="http://auth.example/token",
        capabilities=tuple(capabilities),
        scopes_supported=None if scopes is None else tuple(scopes),
        code_challenge_methods_supported=("S256",),
        token_endpoint_auth_methods_supported=("client_secret_basic",),
    )


def test_haste_capabilities_select_patient_rs_wildcard() -> None:
    cfg = _config(
        [
            "permission-patient",
            "permission-v2",
            "launch-standalone",
            "sso-openid-connect",
            "permission-offline",
        ],
        ["patient/*.rs", "openid", "fhirUser", "launch/patient", "offline_access"],
    )
    assert scopes_from_capabilities(cfg) == (
        "patient/*.rs launch/patient openid fhirUser offline_access"
    )


def test_epic_v1_capabilities_select_patient_read_wildcard() -> None:
    cfg = _config(
        [
            "permission-patient",
            "permission-v1",
            "launch-standalone",
            "sso-openid-connect",
            "permission-offline",
        ],
        ["patient/*.read", "openid", "fhirUser", "launch/patient", "offline_access"],
    )
    assert scopes_from_capabilities(cfg) == (
        "patient/*.read launch/patient openid fhirUser offline_access"
    )


def test_cerner_per_resource_scopes_are_intersected() -> None:
    cfg = _config(
        ["permission-patient", "permission-v1", "launch-standalone", "sso-openid-connect"],
        [
            "patient/Patient.read",
            "patient/Observation.read",
            "patient/Condition.read",
            "patient/AllergyIntolerance.read",
            "patient/MedicationRequest.read",
            "patient/Appointment.read",
            "openid",
            "launch/patient",
            "online_access",
        ],
    )
    assert scopes_from_capabilities(cfg) == (
        "patient/Patient.read patient/Appointment.read patient/MedicationRequest.read "
        "patient/AllergyIntolerance.read patient/Condition.read patient/Observation.read "
        "launch/patient openid online_access"
    )


def test_user_only_capabilities_select_user_scopes() -> None:
    cfg = _config(["permission-user", "permission-v2"], ["user/*.rs"])
    assert scopes_from_capabilities(cfg) == "user/*.rs"


def test_both_scope_syntaxes_prefer_v2() -> None:
    cfg = _config(
        ["permission-patient", "permission-v1", "permission-v2"],
        ["patient/*.read", "patient/*.rs"],
    )
    assert scopes_from_capabilities(cfg) == "patient/*.rs"


def test_missing_patient_and_user_permission_raises() -> None:
    cfg = _config(["launch-standalone"], ["openid"])
    with pytest.raises(DiscoveryError):
        scopes_from_capabilities(cfg)
