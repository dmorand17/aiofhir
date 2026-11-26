"""Patient-scoped reads. The patient id always comes from the token."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from aiofhir.client import FhirClient, SearchResult
from aiofhir.summary import Citation, Item, Summary

_ALLOWED = frozenset(
    {
        "Patient",
        "Appointment",
        "MedicationRequest",
        "AllergyIntolerance",
        "Condition",
        "Observation",
    }
)
_UPCOMING = frozenset({"booked", "pending"})
# Parameters a caller may not send to search_raw: they name another patient,
# pull in other resource types, or bypass the forced patient.
_REFUSED_PARAMS = frozenset(
    {
        "patient",
        "subject",
        "_id",
        "_include",
        "_revinclude",
        "_has",
        "_contained",
        "_containedType",
        "_type",
        "_list",
        "_query",
        "_filter",
    }
)
Params = Mapping[str, str | Sequence[str]]
logger = logging.getLogger(__name__)


@runtime_checkable
class Record(Protocol):
    """The patient-record helpers.

    `PatientRecord` runs them in process with a FHIR token; `RemoteRecord` asks
    the `aiofhir[server]` backend, which runs a `PatientRecord`. Agent code can
    accept either one, or a test fake.
    """

    async def upcoming_appointments(self, days: int = 90) -> Summary: ...

    async def active_medications(self) -> Summary: ...

    async def allergies(self) -> Summary: ...

    async def conditions(self) -> Summary: ...

    async def recent_labs(self, days: int = 90) -> Summary: ...


# The helper names, from the Protocol. The server's allowlist is built from these.
RECORD_HELPERS: tuple[str, ...] = tuple(
    name for name, value in vars(Record).items() if not name.startswith("_") and callable(value)
)


class PatientRecord:
    def __init__(
        self,
        fhir: FhirClient,
        patient_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fhir = fhir
        self.patient_id = patient_id
        self._clock = clock or (lambda: datetime.now(UTC))

    async def search_raw(self, resource: str, params: Params | None = None) -> SearchResult:
        """Search an allowlisted type, always scoped to the token's patient.

        Parameters that could widen the result (`subject`, `_include`,
        `_revinclude`, `_has`, chained or modified `patient`, ...) raise
        ValueError. Only resources of the requested type are returned.
        """
        if resource not in _ALLOWED:
            raise ValueError(f"{resource} is not an allowed resource")
        merged = _normalize(params)
        for key in merged:
            base = re.split(r"[:.]", key, maxsplit=1)[0]
            if base in _REFUSED_PARAMS:
                raise ValueError(f"search_raw does not accept the {base} parameter")
        if resource == "Patient":
            merged["_id"] = [self.patient_id]
        else:
            merged["patient"] = [self.patient_id]
        result = await self._fhir.search(resource, merged)
        kept = tuple(item for item in result.resources if item.get("resourceType") == resource)
        if len(kept) == len(result.resources):
            return result
        dropped = len(result.resources) - len(kept)
        return SearchResult(
            kept,
            (*result.warnings, f"dropped {dropped} resources that were not {resource}"),
            result.truncated,
        )

    async def upcoming_appointments(self, days: int = 90) -> Summary:
        now = self._clock()
        until = now + timedelta(days=days)
        result = await self.search_raw(
            "Appointment",
            {
                "date": [f"ge{_instant(now)}", f"lt{_instant(until)}"],
                "_sort": ["date"],
            },
        )
        kept = [item for item in result.resources if item.get("status") in _UPCOMING]
        kept.sort(key=_appointment_start)
        return _summary(
            "upcoming_appointments",
            [
                Item(
                    str(item.get("description") or "Appointment"),
                    _cite("Appointment", item),
                    at=parse_fhir_datetime(item.get("start")),
                )
                for item in kept
            ],
            result,
        )

    async def active_medications(self) -> Summary:
        result = await self.search_raw("MedicationRequest", {"status": "active"})
        items: list[Item] = []
        for resource in result.resources:
            if resource.get("status") != "active":
                continue
            text = await self._fhir.profile.resolve_medication(resource, self._fhir.read)
            items.append(
                Item(
                    _with_dosage(text, resource),
                    _cite("MedicationRequest", resource),
                    at=parse_fhir_datetime(resource.get("authoredOn")),
                )
            )
        return _summary("active_medications", items, result)

    async def allergies(self) -> Summary:
        result = await self.search_raw("AllergyIntolerance", {"clinical-status": "active"})
        items = [
            Item(_code_text(resource.get("code")), _cite("AllergyIntolerance", resource))
            for resource in result.resources
            if "active" in _codes(resource.get("clinicalStatus"))
        ]
        return _summary("allergies", items, result)

    async def conditions(self) -> Summary:
        result = await self.search_raw(
            "Condition",
            {"category": "problem-list-item", "clinical-status": "active"},
        )
        items = [
            Item(_code_text(resource.get("code")), _cite("Condition", resource))
            for resource in result.resources
            if "active" in _codes(resource.get("clinicalStatus"))
            and _has_category(resource, "problem-list-item")
        ]
        return _summary("conditions", items, result)

    async def recent_labs(self, days: int = 90) -> Summary:
        start = self._clock() - timedelta(days=days)
        result = await self.search_raw(
            "Observation",
            {"category": "laboratory", "date": [f"ge{_instant(start)}"]},
        )
        labs = list(result.resources)
        labs.sort(key=_effective, reverse=True)
        items = [
            Item(
                _lab_text(resource),
                _cite("Observation", resource),
                at=parse_fhir_datetime(_effective(resource) or None),
            )
            for resource in labs
        ]
        return _summary("recent_labs", items, result)


def _summary(kind: str, items: list[Item], result: SearchResult) -> Summary:
    logger.debug(
        "%s items=%s warnings=%s truncated=%s",
        kind,
        len(items),
        len(result.warnings),
        result.truncated,
    )
    return Summary(
        kind=kind,
        items=tuple(items),
        warnings=result.warnings,
        truncated=result.truncated,
    )


def _normalize(params: Params | None) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for key, value in (params or {}).items():
        merged[key] = [value] if isinstance(value, str) else [str(item) for item in value]
    return merged


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cite(resource_type: str, resource: dict[str, Any]) -> Citation:
    identifier = resource.get("id")
    return Citation(resource_type, identifier if isinstance(identifier, str) else "")


def _appointment_start(resource: dict[str, Any]) -> str:
    start = resource.get("start")
    return start if isinstance(start, str) else "9999"


def _effective(resource: dict[str, Any]) -> str:
    for key in ("effectiveDateTime", "effectiveInstant", "issued"):
        value = resource.get(key)
        if isinstance(value, str) and value:
            return value
    period = resource.get("effectivePeriod")
    if isinstance(period, dict) and isinstance(period.get("start"), str):
        return str(period["start"])
    return ""


def parse_fhir_datetime(value: object) -> datetime | date | None:
    """Parse a FHIR date or dateTime. Partial dates (YYYY, YYYY-MM) give None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _lab_text(resource: dict[str, Any]) -> str:
    name = _code_text(resource.get("code"))
    quantity = resource.get("valueQuantity")
    if isinstance(quantity, dict) and isinstance(quantity.get("value"), int | float):
        unit = quantity.get("unit") or quantity.get("code") or ""
        value = f"{quantity['value']} {unit}".strip()
        return f"{name}: {value}" if name else value
    string_value = resource.get("valueString")
    if isinstance(string_value, str) and string_value:
        return f"{name}: {string_value}" if name else string_value
    concept = _code_text(resource.get("valueCodeableConcept"))
    if concept:
        return f"{name}: {concept}" if name else concept
    return name


def _with_dosage(text: str, resource: dict[str, Any]) -> str:
    for dosage in resource.get("dosageInstruction") or []:
        if isinstance(dosage, dict) and isinstance(dosage.get("text"), str) and dosage["text"]:
            return f"{text}: {dosage['text']}" if text else str(dosage["text"])
    return text


def _code_text(concept: object) -> str:
    if not isinstance(concept, dict):
        return ""
    text = concept.get("text")
    return text if isinstance(text, str) else ""


def _codes(value: object) -> set[str]:
    if not isinstance(value, dict):
        return set()
    found: set[str] = set()
    for coding in value.get("coding") or []:
        if isinstance(coding, dict) and isinstance(coding.get("code"), str):
            found.add(coding["code"])
    return found


def _has_category(resource: dict[str, Any], code: str) -> bool:
    for category in resource.get("category") or []:
        if code in _codes(category):
            return True
    return False
