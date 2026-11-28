"""Resource types a server supports, read from its CapabilityStatement (`/metadata`)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from aiofhir.errors import DiscoveryError

_READ_INTERACTIONS = frozenset({"read", "search-type"})


async def fetch_capability_statement(base: str, http: httpx.AsyncClient) -> dict[str, Any]:
    """GET `{base}/metadata` without a token. Raises DiscoveryError when it is not readable."""
    url = f"{base.rstrip('/')}/metadata"
    try:
        response = await http.get(url, headers={"Accept": "application/fhir+json"})
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise DiscoveryError("could not read the CapabilityStatement") from exc
    if not isinstance(body, dict) or body.get("resourceType") != "CapabilityStatement":
        raise DiscoveryError("could not read the CapabilityStatement")
    return body


def readable_types(statement: Mapping[str, Any]) -> tuple[str, ...]:
    """Types the server can read or search, in the statement's order."""
    found: list[str] = []
    for resource in _server_resources(statement):
        name = resource.get("type")
        if (
            isinstance(name, str)
            and name not in found
            and _interactions(resource) & _READ_INTERACTIONS
        ):
            found.append(name)
    return tuple(found)


def patient_search_types(statement: Mapping[str, Any]) -> frozenset[str]:
    """Types a search can scope to one patient: those with a `patient` search parameter.

    `Patient` itself counts when it can be searched (it is scoped by `_id`).
    """
    found: set[str] = set()
    for resource in _server_resources(statement):
        name = resource.get("type")
        if not isinstance(name, str) or "search-type" not in _interactions(resource):
            continue
        params = {p.get("name") for p in resource.get("searchParam") or [] if isinstance(p, dict)}
        if name == "Patient" or "patient" in params:
            found.add(name)
    return frozenset(found)


def _server_resources(statement: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for rest in statement.get("rest") or []:
        if not isinstance(rest, dict) or rest.get("mode") != "server":
            continue
        for resource in rest.get("resource") or []:
            if isinstance(resource, dict):
                yield resource


def _interactions(resource: Mapping[str, Any]) -> set[str]:
    return {
        item["code"]
        for item in resource.get("interaction") or []
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    }
