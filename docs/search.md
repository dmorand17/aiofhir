# Search and the FHIR client

## Patient-scoped search

```python
from aiofhir import FhirClient, PatientRecord

async with FhirClient(base, holder, profile=smart.profile) as fhir:
    record = PatientRecord(fhir, patient_id=tokens.patient)
    types = await record.searchable_types()
    labs = await record.search_raw("Observation", {"category": "laboratory"})
```

`PatientRecord.search_raw` searches one resource type and always scopes it to
the token's patient: it adds `patient=` itself (`_id=` for `Patient`).

- Allowed types come from the server's CapabilityStatement (`/metadata`): each
  type with the `search-type` interaction and a `patient` search parameter, plus
  `Patient`. `searchable_types()` returns them. A type the server does not list
  that way raises `ValueError`, because a server may ignore an unknown
  `patient` parameter and return everyone's data.
- Parameters that could widen the result are refused with `ValueError`:
  `patient`, `subject`, `_id`, `_include`, `_revinclude`, `_has`, `_contained`,
  `_containedType`, `_type`, `_list`, `_query`, `_filter`, and chained or
  modified forms of them.
- Resources of other types in the response are dropped, with a warning.
- The result is a `SearchResult`: `resources` (FHIR JSON dicts), `warnings` and
  `truncated` (paging stopped at `max_pages`).

## Recipes

All of these run inside `async with FhirClient(...) as fhir:` with
`record = PatientRecord(fhir, patient_id=tokens.patient)`.

### Check what the server supports

```python
types = await record.searchable_types()
if "Immunization" not in types:
    ...  # this server cannot scope Immunization to a patient; skip it

statement = await fhir.capabilities()  # the full CapabilityStatement, fetched once
print(statement.get("software", {}).get("name"))
```

### Search parameters

Values are strings. Pass a list to repeat a parameter, for example a date range:

```python
labs = await record.search_raw(
    "Observation",
    {
        "category": "laboratory",
        "date": ["ge2026-01-01", "lt2026-07-01"],  # sent as date=ge...&date=lt...
        "_sort": "-date",
        "_count": "50",
    },
)
for observation in labs.resources:
    code = observation.get("code", {}).get("text")
    value = observation.get("valueQuantity", {})
    print(observation.get("effectiveDateTime"), code, value.get("value"), value.get("unit"))
```

Comma-separated values mean OR, as in FHIR: `{"status": "booked,pending"}`.
Modifiers and prefixes go in the key or value as usual (`"code:text": "glucose"`).
Parameters that could widen the result, such as `subject` or `_include`, raise
`ValueError`; see the list above.

### Follow a reference

`search_raw` returns only the requested type. Read a referenced resource with
`fhir.read`:

```python
requests = await record.search_raw("MedicationRequest", {"status": "active"})
for request in requests.resources:
    if "medicationCodeableConcept" in request:
        name = request["medicationCodeableConcept"].get("text")
    else:
        reference = request.get("medicationReference", {}).get("reference", "")
        if reference.startswith("Medication/"):
            medication = await fhir.read("Medication", reference.split("/", 1)[1])
            name = medication.get("code", {}).get("text")
        else:
            name = request.get("medicationReference", {}).get("display")
    print(name)
```

`read` checks the type and id and raises `ValueError` for anything else, so an
absolute URL or a path in a reference is never fetched. Calls made at the same
time share the client's concurrency cap (`max_concurrency`, default 4):

```python
import asyncio

ids = ["med-1", "med-2", "med-3"]
medications = await asyncio.gather(*(fhir.read("Medication", i) for i in ids))
```

### Paging, truncation and warnings

`search_raw` follows up to 10 pages. Check what came back:

```python
result = await record.search_raw("Encounter")
if result.truncated:
    print("stopped after 10 pages; narrow the search, for example with a date range")
for warning in result.warnings:
    print("warning:", warning)  # OperationOutcome messages, dropped resources
```

For a different page limit, or to handle resources one at a time, use the
client directly. It does not add the patient for you, so pass it yourself, from
the token:

```python
async for resource in fhir.iter_search(
    "Observation", {"patient": tokens.patient, "category": "vital-signs"}, max_pages=50
):
    print(resource["id"])
```

### Handle errors

```python
from aiofhir import AuthExpired
from aiofhir.errors import FhirError

try:
    patient = await fhir.read("Patient", tokens.patient)
except FhirError as exc:
    if exc.status == 404:
        ...  # not found, or hidden by the token's scope
    elif exc.status == 403:
        ...  # the server's access policy refused it
    else:
        raise
except AuthExpired:
    ...  # sign in again
```

Retries for 429, 502, 503 and 504 responses happen before `FhirError` is raised.

### Audit every request

`on_request` sees each attempt, with redacted URLs and no token:

```python
import logging

from aiofhir.client import RequestEvent

audit = logging.getLogger("audit")


def record_request(event: RequestEvent) -> None:
    audit.info(
        "%s %s status=%s attempt=%s %.0fms",
        event.method,
        event.url,
        event.status,
        event.attempt,
        event.elapsed_ms,
    )


async with FhirClient(base, holder, profile=smart.profile, on_request=record_request) as fhir:
    ...
```

The hook can also be `async def`.

## The FHIR client

```python
FhirClient(fhir_base_url, provider, profile=None, max_concurrency=4, timeout=10.0, on_request=None)
```

| Method | What |
| --- | --- |
| `read(type, id)` | one resource |
| `vread(type, id, version)` | one version of a resource |
| `capabilities()` | the CapabilityStatement (`/metadata`), fetched once per client |
| `search(type, params, max_pages=10)` | all pages as a `SearchResult` (`resources`, `warnings`, `truncated`) |
| `iter_search(type, params, max_pages=10)` | the same, one resource at a time |

- Retries 429, 502, 503 and 504 twice with backoff and jitter, honoring
  `Retry-After` up to 30 seconds. Reads and searches (GET and `POST _search`)
  are also retried after a dropped connection or a timeout, within the same two
  retries. On a 401 it refreshes once and retries.
- Follows `next` links only inside the FHIR base, including after decoding
  percent-encoded dot segments. A profile can page another way (Haste uses
  `_offset`). A resource returned twice across pages is dropped, with a warning.
- A search whose GET URL would pass 2,000 characters is sent as a form-encoded
  `POST [type]/_search`, which also keeps its parameters out of the URL.
- `on_request` (sync or async) receives a `RequestEvent` for every attempt:
  method, redacted URL and parameters, status or error, attempt number and
  elapsed milliseconds. It never includes the token. Use it for audit logs and
  metrics.

`FhirClient` itself does not scope anything to a patient; the server's token
scope decides what it may read. Use `PatientRecord.search_raw` when results must
belong to the signed-in patient.

## Capability helpers

`aiofhir.capability` reads a CapabilityStatement:

- `readable_types(statement)`: types with `read` or `search-type`, in order.
  The scope uses these when `scopes_supported` lists no wildcard.
- `patient_search_types(statement)`: the types `search_raw` accepts.
- `fetch_capability_statement(base, http)`: an unauthenticated `GET /metadata`.
