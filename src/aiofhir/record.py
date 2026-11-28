"""Patient-scoped reads. The patient id always comes from the token."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from aiofhir.client import FhirClient, SearchResult
from aiofhir.errors import FhirError
from aiofhir.summary import Citation, Item, Summary

_ALLOWED = frozenset(
    {
        "Patient",
        "Appointment",
        "MedicationRequest",
        "AllergyIntolerance",
        "Condition",
        "Observation",
        "Immunization",
        "Encounter",
        "Procedure",
        "DocumentReference",
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

    async def immunizations(self) -> Summary: ...

    async def vital_signs(self, days: int = 30) -> Summary: ...

    async def past_visits(self, days: int = 365) -> Summary: ...

    async def procedures(self, days: int = 365) -> Summary: ...

    async def documents(self, days: int = 365) -> Summary: ...


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
        # Servers may ignore the date filter, so check the window here too.
        kept = [
            item
            for item in result.resources
            if item.get("status") in _UPCOMING and _starts_within(item, now, until)
        ]
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
        active = [r for r in result.resources if r.get("status") == "active"]
        # Referenced Medications are read concurrently (the client caps concurrency).
        # One failed read leaves that item without a name instead of failing the summary.
        names = await asyncio.gather(
            *(self._fhir.profile.resolve_medication(r, self._fhir.read) for r in active),
            return_exceptions=True,
        )
        items: list[Item] = []
        unread: list[str] = []
        for resource, name in zip(active, names, strict=True):
            if isinstance(name, FhirError):
                unread.append(str(_cite("MedicationRequest", resource)))
                name = ""
            elif isinstance(name, BaseException):
                raise name
            items.append(
                Item(
                    _with_dosage(name, resource),
                    _cite("MedicationRequest", resource),
                    at=parse_fhir_datetime(resource.get("authoredOn")),
                )
            )
        summary = _summary("active_medications", items, result)
        if not unread:
            return summary
        warning = f"could not read the medication for {', '.join(unread)}"
        return replace(summary, warnings=(*summary.warnings, warning))

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

    async def immunizations(self) -> Summary:
        return await self._dated(
            "immunizations",
            "Immunization",
            {"status": "completed"},
            keep=lambda r: r.get("status") == "completed",
            text=lambda r: _code_text(r.get("vaccineCode")),
            when=lambda r: _first_str(r, "occurrenceDateTime"),
        )

    async def vital_signs(self, days: int = 30) -> Summary:
        return await self._dated(
            "vital_signs",
            "Observation",
            {"category": "vital-signs", "date": [f"ge{self._since(days)}"]},
            keep=lambda r: True,
            text=_vital_text,
            when=_effective,
        )

    async def past_visits(self, days: int = 365) -> Summary:
        return await self._dated(
            "past_visits",
            "Encounter",
            {"status": "finished", "date": [f"ge{self._since(days)}"]},
            keep=lambda r: r.get("status") == "finished",
            text=_visit_text,
            when=lambda r: _period_start(r, "period"),
        )

    async def procedures(self, days: int = 365) -> Summary:
        return await self._dated(
            "procedures",
            "Procedure",
            {"status": "completed", "date": [f"ge{self._since(days)}"]},
            keep=lambda r: r.get("status") == "completed",
            text=lambda r: _code_text(r.get("code")),
            when=lambda r: (
                _first_str(r, "performedDateTime") or _period_start(r, "performedPeriod")
            ),
        )

    async def documents(self, days: int = 365) -> Summary:
        # Titles and dates only: content, attachments and narrative are never read.
        return await self._dated(
            "documents",
            "DocumentReference",
            {"status": "current", "date": [f"ge{self._since(days)}"]},
            keep=lambda r: r.get("status") == "current",
            text=lambda r: _first_str(r, "description") or _code_text(r.get("type")),
            when=lambda r: _first_str(r, "date"),
        )

    def _since(self, days: int) -> str:
        return _instant(self._clock() - timedelta(days=days))

    async def _dated(
        self,
        kind: str,
        resource_type: str,
        params: Params,
        *,
        keep: Callable[[dict[str, Any]], bool],
        text: Callable[[dict[str, Any]], str],
        when: Callable[[dict[str, Any]], str],
    ) -> Summary:
        """Search, keep matching records (servers may ignore filters), newest first."""
        result = await self.search_raw(resource_type, params)
        kept = [resource for resource in result.resources if keep(resource)]
        kept.sort(key=when, reverse=True)
        items = [
            Item(text(r), _cite(resource_type, r), at=parse_fhir_datetime(when(r) or None))
            for r in kept
        ]
        return _summary(kind, items, result)


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


def _starts_within(resource: dict[str, Any], start: datetime, end: datetime) -> bool:
    """True when `start` is in [start, end), or missing (the server's filter decides)."""
    when = parse_fhir_datetime(resource.get("start"))
    if when is None:
        return True
    if not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day, tzinfo=UTC)
    return start <= when < end


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
        value = f"{_number(quantity['value'])} {unit}".strip()
        return f"{name}: {value}" if name else value
    string_value = resource.get("valueString")
    if isinstance(string_value, str) and string_value:
        return f"{name}: {string_value}" if name else string_value
    concept = _code_text(resource.get("valueCodeableConcept"))
    if concept:
        return f"{name}: {concept}" if name else concept
    return name


def _number(value: float | int) -> str:
    """FHIR decimals arrive as JSON floats; print 118.0 as 118 (it is read aloud)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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


_SYSTOLIC = "8480-6"
_DIASTOLIC = "8462-4"


def _vital_text(resource: dict[str, Any]) -> str:
    """Like a lab, but a blood pressure reads its components as systolic/diastolic."""
    values: dict[str, tuple[float | int, str]] = {}
    for component in resource.get("component") or []:
        if not isinstance(component, dict):
            continue
        quantity = component.get("valueQuantity")
        if not isinstance(quantity, dict) or not isinstance(quantity.get("value"), int | float):
            continue
        unit = str(quantity.get("unit") or quantity.get("code") or "")
        for code in _codes(component.get("code")):
            values[code] = (quantity["value"], unit)
    if _SYSTOLIC in values and _DIASTOLIC in values:
        (systolic, unit), (diastolic, _) = values[_SYSTOLIC], values[_DIASTOLIC]
        name = _code_text(resource.get("code")) or "Blood pressure"
        return f"{name}: {_number(systolic)}/{_number(diastolic)} {unit}".strip()
    return _lab_text(resource)


def _visit_text(resource: dict[str, Any]) -> str:
    for kind in resource.get("type") or []:
        text = _code_text(kind)
        if text:
            return text
    visit_class = resource.get("class")
    if isinstance(visit_class, dict):
        return str(visit_class.get("display") or visit_class.get("code") or "")
    return ""


def _first_str(resource: dict[str, Any], key: str) -> str:
    value = resource.get(key)
    return value if isinstance(value, str) else ""


def _period_start(resource: dict[str, Any], key: str) -> str:
    period = resource.get(key)
    if isinstance(period, dict) and isinstance(period.get("start"), str):
        return str(period["start"])
    return ""
