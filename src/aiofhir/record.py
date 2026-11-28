"""Patient-scoped search. The patient id always comes from the token."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from aiofhir.capability import patient_search_types
from aiofhir.client import FhirClient, SearchResult

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


class PatientRecord:
    def __init__(self, fhir: FhirClient, patient_id: str) -> None:
        self._fhir = fhir
        self.patient_id = patient_id

    async def searchable_types(self) -> frozenset[str]:
        """Types `search_raw` accepts: from the CapabilityStatement, those with a
        `patient` search parameter, plus `Patient`."""
        return patient_search_types(await self._fhir.capabilities())

    async def search_raw(self, resource: str, params: Params | None = None) -> SearchResult:
        """Search one type, always scoped to the token's patient.

        The type must be in `searchable_types()`. Parameters that could widen the
        result (`subject`, `_include`, `_revinclude`, `_has`, chained or modified
        `patient`, ...) raise ValueError. Only resources of the requested type
        are returned.
        """
        merged = _normalize(params)
        for key in merged:
            base = re.split(r"[:.]", key, maxsplit=1)[0]
            if base in _REFUSED_PARAMS:
                raise ValueError(f"search_raw does not accept the {base} parameter")
        if resource not in await self.searchable_types():
            raise ValueError(f"{resource} cannot be searched by patient on this server")
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


def _normalize(params: Params | None) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for key, value in (params or {}).items():
        merged[key] = [value] if isinstance(value, str) else [str(item) for item in value]
    return merged
