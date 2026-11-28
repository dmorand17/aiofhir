"""SMART configuration discovery."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from aiofhir.errors import DiscoveryError
from aiofhir.profiles.base import ServerProfile

# (base, profile) -> (fetched at, configuration). Entries expire so a server's
# changed endpoints or capabilities are picked up without a restart.
_cache: dict[tuple[str, str], tuple[float, SmartConfiguration]] = {}
_CACHE_TTL = 3600.0
_monotonic = time.monotonic
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmartConfiguration:
    authorization_endpoint: str
    token_endpoint: str
    capabilities: tuple[str, ...]
    scopes_supported: tuple[str, ...] | None
    code_challenge_methods_supported: tuple[str, ...]
    token_endpoint_auth_methods_supported: tuple[str, ...]


def clear_discovery_cache() -> None:
    _cache.clear()


async def discover(
    base: str,
    profile: ServerProfile,
    http: httpx.AsyncClient,
) -> SmartConfiguration:
    root = base.rstrip("/")
    key = (root, type(profile).__qualname__)
    cached = _cache.get(key)
    if cached is not None and _monotonic() - cached[0] < _CACHE_TTL:
        logger.debug("using cached SMART configuration for %s", root)
        return cached[1]
    last_error: Exception | None = None
    for url in profile.discovery_paths(base):
        logger.debug("GET %s", url)
        try:
            response = await http.get(url)
        except httpx.HTTPError as exc:
            logger.debug("SMART configuration request failed for %s", url)
            last_error = exc
            continue
        logger.debug("SMART configuration %s status=%s", url, response.status_code)
        if response.status_code != 200:
            last_error = DiscoveryError(f"{url} returned {response.status_code}")
            continue
        try:
            body = response.json()
        except ValueError:
            logger.debug("SMART configuration %s was not JSON", url)
            last_error = DiscoveryError(f"{url} did not return JSON")
            continue
        configuration = _parse(body)
        if "S256" not in configuration.code_challenge_methods_supported:
            raise DiscoveryError("SMART configuration does not support S256 PKCE")
        _cache[key] = (_monotonic(), configuration)
        logger.info("discovered SMART configuration for %s", root)
        return configuration
    raise DiscoveryError("SMART discovery failed") from last_error


def _parse(body: object) -> SmartConfiguration:
    if not isinstance(body, dict):
        raise DiscoveryError("SMART configuration was not a JSON object")
    authorization = _required_str(body, "authorization_endpoint")
    token = _required_str(body, "token_endpoint")
    scopes = body.get("scopes_supported")
    scopes_supported = tuple(_strings(scopes)) if isinstance(scopes, list) else None
    return SmartConfiguration(
        authorization_endpoint=authorization,
        token_endpoint=token,
        capabilities=tuple(_strings(body.get("capabilities"))),
        scopes_supported=scopes_supported,
        code_challenge_methods_supported=tuple(
            _strings(body.get("code_challenge_methods_supported"))
        ),
        token_endpoint_auth_methods_supported=tuple(
            _strings(body.get("token_endpoint_auth_methods_supported"))
        ),
    )


def _required_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise DiscoveryError(f"SMART configuration is missing {key}")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
