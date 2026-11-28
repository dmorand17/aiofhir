# Record helpers

`PatientRecord(fhir, patient_id)` reads one patient's record. Every helper adds
`patient=` itself, filters on the client as well as the server (servers may
ignore a filter), and returns a `Summary`.

## The helpers

| Helper | Resource | Keeps | Text | Date |
| --- | --- | --- | --- | --- |
| `upcoming_appointments(days=90)` | Appointment | `booked`, `pending`; from now to now + days; soonest first | `description` | `start` |
| `active_medications()` | MedicationRequest | `active` | medication name (inline, contained or referenced) and dosage text | `authoredOn` |
| `allergies()` | AllergyIntolerance | clinical status `active` | `code.text` | |
| `conditions()` | Condition | `active` problem-list items | `code.text` | |
| `recent_labs(days=90)` | Observation (`laboratory`) | last `days`; newest first | name and value | effective date |
| `immunizations()` | Immunization | `completed`; newest first | `vaccineCode.text` | `occurrenceDateTime` |
| `vital_signs(days=30)` | Observation (`vital-signs`) | last `days`; newest first | name and value; blood pressure as systolic/diastolic | effective date |
| `past_visits(days=365)` | Encounter | `finished`; newest first | `type[0].text`, else the class | `period.start` |
| `procedures(days=365)` | Procedure | `completed`; newest first | `code.text` | performed date or period start |
| `documents(days=365)` | DocumentReference | `current`; newest first | `description`, else `type.text` | `date` |

`documents()` returns titles and dates only. It never reads document content,
attachments or narrative.

`active_medications()` reads referenced Medications concurrently. If one read
fails, that item keeps its citation without a medication name, and the summary
gets a warning. `upcoming_appointments()` also checks the window itself, for
servers that ignore the `date` filter.

## Summary

```python
summary.kind  # "active_medications"
summary.items  # tuple[Item, ...]: text, citation (Type/id), at (datetime, date or None)
summary.warnings  # server OperationOutcome messages, dropped resources
summary.truncated  # True when paging stopped at max_pages
summary.to_text(max_chars=4000)
summary.to_dict() / Summary.from_dict(data)
```

`to_text()` is the form to give a model. Each line is
`- «text» 2026-11-28T03:07:00Z (Appointment/a1)`: free text from the record is
wrapped in `«»` so instructions can tell the model it is data, dates are ISO
8601 in UTC, and every line cites its resource. Over `max_chars`, it cuts on an
item boundary and adds `(+N more)`.

`aiofhir.display.format_summary(summary, tz=...)` formats the same summary for a
person, in their time zone. It never changes the summary.

## One interface, three implementations

`aiofhir.Record` is a `typing.Protocol` with the ten helpers. `PatientRecord`
(in process, holds a token) and `RemoteRecord` (asks the
[token-holding backend](server.md)) both satisfy it, as can a test fake. Type
agent code against `Record`. `aiofhir.RECORD_HELPERS` lists the helper names.

## Raw search

```python
result = await record.search_raw("Observation", {"code": "http://loinc.org|4548-4"})
```

- Allowed types: Patient, Appointment, MedicationRequest, AllergyIntolerance,
  Condition, Observation, Immunization, Encounter, Procedure, DocumentReference.
- Parameters that could widen the result are refused with `ValueError`:
  `patient`, `subject`, `_id`, `_include`, `_revinclude`, `_has`, `_contained`,
  `_containedType`, `_type`, `_list`, `_query`, `_filter`, and chained or
  modified forms of them.
- Resources of other types in the response are dropped, with a warning.

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
