"""Build a SMART scope string from a server's advertised capabilities."""

from __future__ import annotations

import logging

from aiofhir.errors import DiscoveryError
from aiofhir.smart.discovery import SmartConfiguration

READ_RESOURCES: tuple[str, ...] = (
    "Patient",
    "Appointment",
    "MedicationRequest",
    "Medication",
    "AllergyIntolerance",
    "Condition",
    "Observation",
    "Immunization",
    "Encounter",
    "Procedure",
    "DocumentReference",
)
logger = logging.getLogger(__name__)


def scopes_from_capabilities(configuration: SmartConfiguration) -> str:
    capabilities = set(configuration.capabilities)
    if "permission-patient" in capabilities:
        context = "patient"
    elif "permission-user" in capabilities:
        context = "user"
    else:
        raise DiscoveryError("SMART configuration advertises neither patient nor user scopes")

    supported = configuration.scopes_supported
    permission = _permission(capabilities, supported)
    parts = _resource_scopes(context, permission, supported)
    parts.extend(_launch_scopes(context, capabilities, supported))
    parts.extend(_identity_scopes(capabilities, supported))
    refresh = _refresh_scope(capabilities, supported)
    if refresh is not None:
        parts.append(refresh)
    scope = " ".join(parts)
    logger.debug("derived scope %s", scope)
    return scope


def _permission(capabilities: set[str], supported: tuple[str, ...] | None) -> str:
    v2 = "permission-v2" in capabilities
    if v2 and (supported is None or any(scope.endswith(".rs") for scope in supported)):
        return ".rs"
    return ".read"


def _resource_scopes(
    context: str,
    permission: str,
    supported: tuple[str, ...] | None,
) -> list[str]:
    wildcard = f"{context}/*{permission}"
    if supported is None or wildcard in supported:
        return [wildcard]
    listed = set(supported)
    return [
        f"{context}/{resource}{permission}"
        for resource in READ_RESOURCES
        if f"{context}/{resource}{permission}" in listed
    ]


def _launch_scopes(
    context: str,
    capabilities: set[str],
    supported: tuple[str, ...] | None,
) -> list[str]:
    if supported is not None and "launch/patient" in supported:
        return ["launch/patient"]
    if "launch-standalone" in capabilities and context == "patient":
        return ["launch/patient"]
    return []


def _identity_scopes(capabilities: set[str], supported: tuple[str, ...] | None) -> list[str]:
    if "sso-openid-connect" not in capabilities:
        return []
    names = ("openid", "fhirUser")
    if supported is None:
        return list(names)
    return [name for name in names if name in supported]


def _refresh_scope(capabilities: set[str], supported: tuple[str, ...] | None) -> str | None:
    offline = supported is not None and "offline_access" in supported
    if "permission-offline" in capabilities or offline:
        return "offline_access"
    if supported is not None and "online_access" in supported:
        return "online_access"
    return None
