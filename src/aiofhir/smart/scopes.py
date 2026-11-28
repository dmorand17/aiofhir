"""Build a SMART scope string from a server's advertised capabilities."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiofhir.errors import DiscoveryError
from aiofhir.smart.discovery import SmartConfiguration

logger = logging.getLogger(__name__)


def scopes_from_capabilities(
    configuration: SmartConfiguration,
    *,
    ehr_launch: bool = False,
    resources: Sequence[str] = (),
) -> str:
    """The authorize scope. An EHR launch asks for `launch` instead of `launch/patient`.

    When `scopes_supported` lists no wildcard, a scope is requested for each of
    `resources` (the CapabilityStatement's readable types) that it lists.
    """
    capabilities = set(configuration.capabilities)
    context = _context(capabilities)
    supported = configuration.scopes_supported
    permission = _permission(capabilities, supported)
    parts = _resource_scopes(context, permission, supported, resources)
    if ehr_launch:
        parts.append("launch")
    else:
        parts.extend(_launch_scopes(context, capabilities, supported))
    parts.extend(_identity_scopes(capabilities, supported))
    refresh = _refresh_scope(capabilities, supported)
    if refresh is not None:
        parts.append(refresh)
    scope = " ".join(parts)
    logger.debug("derived scope %s", scope)
    return scope


def system_scopes_from_capabilities(
    configuration: SmartConfiguration,
    resources: Sequence[str] = (),
) -> str:
    """Read scopes for the client_credentials grant (SMART Backend Services).

    The same permission and resource rules as `scopes_from_capabilities`, in the
    `system` context, with no launch, identity or refresh scopes.
    """
    supported = configuration.scopes_supported
    permission = _permission(set(configuration.capabilities), supported)
    scope = " ".join(_resource_scopes("system", permission, supported, resources))
    if not scope:
        raise DiscoveryError("SMART configuration lists no system read scopes")
    logger.debug("derived system scope %s", scope)
    return scope


def lists_wildcard(configuration: SmartConfiguration, *, system: bool = False) -> bool:
    """True when the scope needs no resource types: the wildcard scope is allowed."""
    supported = configuration.scopes_supported
    if supported is None:
        return True
    capabilities = set(configuration.capabilities)
    context = "system" if system else _context(capabilities)
    return f"{context}/*{_permission(capabilities, supported)}" in supported


def _context(capabilities: set[str]) -> str:
    if "permission-patient" in capabilities:
        return "patient"
    if "permission-user" in capabilities:
        return "user"
    raise DiscoveryError("SMART configuration advertises neither patient nor user scopes")


def _permission(capabilities: set[str], supported: tuple[str, ...] | None) -> str:
    v2 = "permission-v2" in capabilities
    if v2 and (supported is None or any(scope.endswith(".rs") for scope in supported)):
        return ".rs"
    return ".read"


def _resource_scopes(
    context: str,
    permission: str,
    supported: tuple[str, ...] | None,
    resources: Sequence[str],
) -> list[str]:
    wildcard = f"{context}/*{permission}"
    if supported is None or wildcard in supported:
        return [wildcard]
    listed = set(supported)
    return [
        f"{context}/{resource}{permission}"
        for resource in resources
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
