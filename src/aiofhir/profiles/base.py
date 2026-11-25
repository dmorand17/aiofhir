"""Server profile hooks. Search quirks stay here, not in the helpers."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

Json = dict[str, Any]
ReadResource = Callable[[str, str], Awaitable[Json]]


class ServerProfile:
    """Default SMART and FHIR behavior for an R4 server."""

    name = "generic"

    def discovery_paths(self, base: str) -> list[str]:
        root = base.rstrip("/")
        return [f"{root}/.well-known/smart-configuration"]

    def adapt_search(
        self,
        resource: str,
        params: Mapping[str, Sequence[str]],
    ) -> dict[str, list[str]]:
        del resource
        return {key: list(values) for key, values in params.items()}

    async def resolve_medication(self, request: Mapping[str, Any], read: ReadResource) -> str:
        concept = request.get("medicationCodeableConcept")
        if isinstance(concept, dict):
            return code_text(concept)
        value = request.get("medicationReference")
        reference = _reference(value)
        display = _display(value)
        if reference.startswith("#"):
            contained = _contained(request, reference[1:])
            return code_text(contained.get("code")) if contained else display
        medication_id = medication_reference_id(reference)
        if medication_id is not None:
            medication = await read("Medication", medication_id)
            return code_text(medication.get("code")) or display
        return display


def discovery_fallback(base: str, tenant_project_path: str) -> str | None:
    """Build an absolute fallback URL on the same host, or None when it repeats the primary."""
    root = base.rstrip("/")
    parsed = urlsplit(root)
    fallback = urlunsplit((parsed.scheme, parsed.netloc, tenant_project_path, "", ""))
    primary = f"{root}/.well-known/smart-configuration"
    if fallback == primary:
        return None
    return fallback


def code_text(concept: object) -> str:
    if not isinstance(concept, dict):
        return ""
    text = concept.get("text")
    if isinstance(text, str) and text:
        return text
    for coding in concept.get("coding") or []:
        if not isinstance(coding, dict):
            continue
        display = coding.get("display") or coding.get("code")
        if isinstance(display, str) and display:
            return display
    return ""


_RELATIVE_MEDICATION = re.compile(
    r"^Medication/([A-Za-z0-9\-.]{1,64})(?:/_history/[A-Za-z0-9\-.]{1,64})?$"
)


def medication_reference_id(reference: str) -> str | None:
    """The id in a relative `Medication/{id}[/_history/{v}]` reference, else None.

    Absolute URLs (another server) and anything else are not followed.
    """
    match = _RELATIVE_MEDICATION.match(reference.strip())
    if match is None or match.group(1) in {".", ".."}:
        return None
    return match.group(1)


def _display(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("display"), str):
        return str(value["display"])
    return ""


def _reference(value: object) -> str:
    if isinstance(value, dict):
        reference = value.get("reference")
        if isinstance(reference, str):
            return reference
    return ""


def _contained(request: Mapping[str, Any], contained_id: str) -> Json:
    for item in request.get("contained") or []:
        if isinstance(item, dict) and item.get("id") == contained_id:
            return item
    return {}
