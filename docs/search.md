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
