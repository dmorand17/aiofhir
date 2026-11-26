"""Async FHIR R4 read and search client."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from aiofhir.errors import AuthExpired, FhirError
from aiofhir.log import redact_params, redact_url
from aiofhir.profiles.base import ServerProfile
from aiofhir.profiles.generic import GenericProfile

Json = dict[str, Any]
Params = Mapping[str, str | Sequence[str]]
Sleep = Callable[[float], Awaitable[None]]
Uniform = Callable[[float, float], float]
_RETRYABLE = frozenset({429, 502, 503, 504})
_MAX_RETRY_WAIT = 30.0
_OUTSIDE_BASE = "ignored next link outside the FHIR base (cross-origin or another path)"
_RESOURCE_TYPE = re.compile(r"^[A-Z][A-Za-z]{1,63}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
# Longer GET URLs are sent as POST [type]/_search; many servers and proxies cap URLs.
_MAX_GET_URL = 2000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequestEvent:
    """One attempt at a FHIR request, as a hook sees it.

    `url` and `params` are redacted the same way as the debug logs: resource ids
    in paths and person-identifying search values are replaced. No token is included.
    """

    method: str
    url: str
    params: dict[str, list[str]]
    status: int | None
    attempt: int
    elapsed_ms: float
    error: str | None = None


RequestHook = Callable[[RequestEvent], Awaitable[None] | None]


class TokenProvider(Protocol):
    async def get(self) -> str: ...

    async def refresh(self, stale: str | None = None) -> str: ...


@dataclass(frozen=True)
class SearchResult:
    resources: tuple[Json, ...]
    warnings: tuple[str, ...]
    truncated: bool


class FhirClient:
    """Reads and searches one FHIR base, with retries and a concurrency cap."""

    def __init__(
        self,
        fhir_base_url: str,
        provider: TokenProvider,
        profile: ServerProfile | None = None,
        max_concurrency: int = 4,
        timeout: float = 10.0,
        sleep: Sleep = asyncio.sleep,
        uniform: Uniform = random.uniform,
        on_request: RequestHook | None = None,
    ) -> None:
        self._base = fhir_base_url.rstrip("/")
        self._provider = provider
        self.profile = profile or GenericProfile()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout
        self._sleep = sleep
        self._uniform = uniform
        self._http: httpx.AsyncClient | None = None
        self._on_request = on_request
        self._capabilities: Json | None = None

    async def __aenter__(self) -> FhirClient:
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def read(self, resource_type: str, resource_id: str) -> Json:
        check_resource_type(resource_type)
        if not _RESOURCE_ID.match(resource_id) or resource_id in {".", ".."}:
            raise ValueError("not a valid FHIR resource id")
        response = await self._send("GET", f"{self._base}/{resource_type}/{resource_id}")
        body = response.json()
        if not isinstance(body, dict):
            raise FhirError(response.status_code, "response was not a JSON object")
        return body

    async def vread(self, resource_type: str, resource_id: str, version_id: str) -> Json:
        check_resource_type(resource_type)
        for value in (resource_id, version_id):
            if not _RESOURCE_ID.match(value) or value in {".", ".."}:
                raise ValueError("not a valid FHIR resource or version id")
        url = f"{self._base}/{resource_type}/{resource_id}/_history/{version_id}"
        return _json_object(await self._send("GET", url))

    async def capabilities(self) -> Json:
        """The server's CapabilityStatement (`/metadata`), fetched once per client."""
        if self._capabilities is None:
            self._capabilities = _json_object(await self._send("GET", f"{self._base}/metadata"))
        return self._capabilities

    async def search(
        self,
        resource_type: str,
        params: Params | None = None,
        max_pages: int = 10,
    ) -> SearchResult:
        resources: list[Json] = []
        warnings: list[str] = []
        truncated = False
        async for page in self._pages(resource_type, params, max_pages):
            resources.extend(page.resources)
            warnings.extend(page.warnings)
            truncated = page.truncated
        return SearchResult(tuple(resources), tuple(warnings), truncated)

    async def iter_search(
        self,
        resource_type: str,
        params: Params | None = None,
        max_pages: int = 10,
    ) -> AsyncIterator[Json]:
        async for page in self._pages(resource_type, params, max_pages):
            for resource in page.resources:
                yield resource

    async def _pages(
        self,
        resource_type: str,
        params: Params | None,
        max_pages: int,
    ) -> AsyncIterator[SearchResult]:
        check_resource_type(resource_type)
        adapted = self.profile.adapt_search(resource_type, _normalize(params))
        first = f"{self._base}/{resource_type}"
        url: str | None = first
        query: dict[str, list[str]] | None = adapted or None
        pages = 0
        by_link = False
        use_post = query is not None and len(str(httpx.URL(first, params=query))) > _MAX_GET_URL
        while url is not None and pages < max_pages:
            if use_post and not by_link:
                search_url = f"{self._base}/{resource_type}/_search"
                response = await self._send("POST", search_url, form=query or {})
            else:
                response = await self._send("GET", url, params=query)
            body = response.json()
            resources, warnings = _bundle_entries(body)
            pages += 1
            nxt = _next_link(body, url)
            truncated = False
            if nxt is None:
                # Servers without `next` links (Haste) page by parameters instead.
                following = None if by_link else self.profile.next_page(query or {}, len(resources))
                if following is None:
                    url = None
                elif pages >= max_pages:
                    logger.debug("stopping after %s pages", max_pages)
                    truncated = True
                    url = None
                else:
                    query = following
            elif not _within_base(self._base, nxt):
                logger.debug("ignoring next link outside the FHIR base")
                warnings = [*warnings, _OUTSIDE_BASE]
                url = None
            elif pages >= max_pages:
                logger.debug("stopping after %s pages", max_pages)
                truncated = True
                url = None
            else:
                url = nxt
                query = None
                by_link = True
            yield SearchResult(tuple(resources), tuple(warnings), truncated)

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, list[str]] | None = None,
        form: dict[str, list[str]] | None = None,
    ) -> httpx.Response:
        if self._http is None:
            raise FhirError(0, "client is not open")
        retries = 0
        refreshed = False
        attempt = 0
        safe_url = redact_url(url)
        safe_params = redact_params(params if form is None else form)
        while True:
            attempt += 1
            token = await self._provider.get()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"}
            logger.debug("%s %s params=%s", method, safe_url, safe_params)
            started = time.monotonic()
            try:
                async with self._semaphore:
                    response = await asyncio.wait_for(
                        self._http.request(method, url, params=params, data=form, headers=headers),
                        self._timeout,
                    )
            except TimeoutError as exc:
                logger.debug("%s %s timed out", method, safe_url)
                await self._emit(method, safe_url, safe_params, None, attempt, started, "timeout")
                raise FhirError(0, "request timed out") from exc
            except httpx.HTTPError as exc:
                logger.debug("%s %s failed", method, safe_url)
                await self._emit(
                    method, safe_url, safe_params, None, attempt, started, "request failed"
                )
                raise FhirError(0, "request failed") from exc
            logger.debug("%s %s status=%s", method, safe_url, response.status_code)
            await self._emit(method, safe_url, safe_params, response.status_code, attempt, started)
            if response.status_code == 401:
                if not refreshed:
                    logger.info("received 401; refreshing the access token")
                    refreshed = True
                    await self._provider.refresh(token)
                    continue
                raise AuthExpired()
            if response.status_code in _RETRYABLE and retries < 2:
                logger.info(
                    "retrying %s %s after status %s", method, safe_url, response.status_code
                )
                await self._sleep(self._delay(response, retries))
                retries += 1
                continue
            if response.status_code >= 400:
                text, outcome = _error_details(response)
                raise FhirError(response.status_code, text, outcome=outcome)
            return response

    async def _emit(
        self,
        method: str,
        url: str,
        params: dict[str, list[str]],
        status: int | None,
        attempt: int,
        started: float,
        error: str | None = None,
    ) -> None:
        if self._on_request is None:
            return
        elapsed = (time.monotonic() - started) * 1000.0
        result = self._on_request(
            RequestEvent(method, url, params, status, attempt, elapsed, error)
        )
        if inspect.isawaitable(result):
            await result

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header is not None and header.strip().isdigit():
            return min(float(header), _MAX_RETRY_WAIT)
        jitter = float(self._uniform(0.5, 1.0))
        backoff = 0.5 * (2.0**attempt) * jitter
        return min(_MAX_RETRY_WAIT, backoff)


def _json_object(response: httpx.Response) -> Json:
    body = response.json()
    if not isinstance(body, dict):
        raise FhirError(response.status_code, "response was not a JSON object")
    return body


def _normalize(params: Params | None) -> dict[str, list[str]]:
    if not params:
        return {}
    normalized: dict[str, list[str]] = {}
    for key, value in params.items():
        if isinstance(value, str):
            normalized[key] = [value]
        else:
            normalized[key] = [str(item) for item in value]
    return normalized


def _bundle_entries(body: object) -> tuple[list[Json], list[str]]:
    if not isinstance(body, dict):
        return [], []
    if body.get("resourceType") == "OperationOutcome":
        return [], _outcome_messages(body)
    resources: list[Json] = []
    warnings: list[str] = []
    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        if resource.get("resourceType") == "OperationOutcome":
            warnings.extend(_outcome_messages(resource))
        else:
            resources.append(resource)
    return resources, warnings


def _outcome_messages(resource: Json) -> list[str]:
    messages: list[str] = []
    for issue in resource.get("issue") or []:
        if not isinstance(issue, dict):
            continue
        diagnostics = issue.get("diagnostics")
        if isinstance(diagnostics, str) and diagnostics:
            messages.append(diagnostics)
            continue
        details = issue.get("details")
        text = details.get("text") if isinstance(details, dict) else None
        if isinstance(text, str) and text:
            messages.append(text)
    return messages or ["OperationOutcome"]


def _next_link(body: object, current: str) -> str | None:
    if not isinstance(body, dict):
        return None
    for link in body.get("link") or []:
        if not isinstance(link, dict) or link.get("relation") != "next":
            continue
        target = link.get("url")
        if isinstance(target, str) and target:
            return urljoin(current, target)
    return None


def check_resource_type(resource_type: str) -> None:
    if not _RESOURCE_TYPE.match(resource_type):
        raise ValueError("not a valid FHIR resource type")


def _within_base(base: str, url: str) -> bool:
    """Same scheme and host, and a path under the FHIR base.

    Multi-tenant servers such as Haste share one host across tenants
    (`/w/{tenant}/{project}/...`), so the origin alone is not enough.
    Dot segments are rejected so `base/../other` cannot climb out.
    """
    left, right = urlsplit(base), urlsplit(url)
    if (left.scheme, left.netloc.lower()) != (right.scheme, right.netloc.lower()):
        return False
    segments = right.path.split("/")
    if ".." in segments or "." in segments:
        return False
    prefix = left.path.rstrip("/")
    return right.path == prefix or right.path.startswith(prefix + "/")


def _error_details(response: httpx.Response) -> tuple[str, Json | None]:
    """The error text (redacted later by FhirError) and the raw OperationOutcome, if any."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase, None
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        return "; ".join(_outcome_messages(body)), body
    return response.reason_phrase, None
