# aiofhir

Async FHIR R4 and SMART on FHIR client.

## Haste Health

The seed and smoke scripts for Haste Health live in [`haste/`](haste/README.md). They are not part of the package.

## Logging

The library only attaches a `NullHandler` to the `aiofhir` logger. Scripts call
`aiofhir.log.configure_cli_logging()`, which prints lines like:

```
2026-11-25T14:58:03.412-05:00 INFO  aiofhir.smart.discovery: discovered SMART configuration for https://...
```

`LOG_LEVEL` sets the level (default `INFO`) and `LOG_UTC=1` prints UTC timestamps.
Every line passes a redaction filter: bearer tokens, token JSON fields, and
`Patient/`, `User/`, `Membership/` ids are replaced, and request URLs hide
person-identifying search values.
