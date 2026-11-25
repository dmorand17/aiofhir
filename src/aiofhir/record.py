"""Patient-scoped reads. The patient id always comes from the token."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aiofhir.client import FhirClient, SearchResult
from aiofhir.summary import Citation, Item, Summary

_ALLOWED = frozenset(
    {
        "Patient",
        "Appointment",
        "MedicationRequest",
        "Medication",
        "AllergyIntolerance",
        "Condition",
        "Observation",
    }
)
_UPCOMING = frozenset({"booked", "pending"})
Params = Mapping[str, str | Sequence[str]]


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
        if resource not in _ALLOWED:
            raise ValueError(f"{resource} is not an allowed resource")
        merged = _normalize(params)
        merged["patient"] = [self.patient_id]
        return await self._fhir.search(resource, merged)

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
                Item(str(item.get("description") or "Appointment"), _cite("Appointment", item))
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
            items.append(Item(text, _cite("MedicationRequest", resource)))
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
            Item(_code_text(resource.get("code")), _cite("Observation", resource))
            for resource in labs
        ]
        return _summary("recent_labs", items, result)


def _summary(kind: str, items: list[Item], result: SearchResult) -> Summary:
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
    value = resource.get("effectiveDateTime")
    return value if isinstance(value, str) else ""


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
