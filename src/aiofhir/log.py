"""Redaction for request traces, and opt-in logging setup for scripts.

The library itself only attaches a NullHandler to the ``aiofhir`` logger (see
``aiofhir/__init__.py``). Applications own handlers and formatting. Scripts and
examples call :func:`configure_cli_logging`, which installs one stream handler
with an ISO 8601 timestamp and a redaction filter.

Environment:
- ``AIOFHIR_LOG_LEVEL``: read on ``import aiofhir``; sets the level of the
  ``aiofhir`` logger only (see :func:`apply_env_level`).
- ``LOG_LEVEL``: root level for :func:`configure_logging` (default ``INFO``).
- ``LOG_UTC=1``: print timestamps in UTC instead of local time.

httpx and httpcore stay at WARNING so a DEBUG run does not print Authorization
headers.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiofhir.scrub import SECRET_KEYS, scrub

LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
REDACTED = "[redacted]"

# Search parameters whose values can identify a person. Matched on the base
# name, so `subject:Patient`, `patient.identifier` and `patient:missing` count.
_HIDDEN_QUERY = frozenset(
    {
        "patient",
        "subject",
        "_id",
        "identifier",
        "name",
        "family",
        "given",
        "birthdate",
        "phone",
        "email",
        "telecom",
        "address",
        "address-postalcode",
        "user",
        "link",
        # References that usually point at a person (Appointment.actor, Observation.performer, ...).
        "actor",
        "performer",
        "requester",
        "participant",
        "author",
        "beneficiary",
        "general-practitioner",
        "asserter",
        "recorder",
        # Free-text search can carry names.
        "_text",
        "_content",
        # Credentials, should one ever reach a URL.
        *SECRET_KEYS,
    }
)
# Resource types whose ids are redacted in URL paths and log messages.
_PERSON_TYPES = ("Patient", "User", "Person", "RelatedPerson", "Membership")
_PATH_TYPE = re.compile(r"^[A-Z][A-Za-z]+$")
# `Patient/123`, also URL-encoded (`Patient%2F123`) and inside absolute URLs.
_PERSON_REF = re.compile(
    r"\b(" + "|".join(_PERSON_TYPES) + r")(?:/|%2[Ff])(?!\[id\])[A-Za-z0-9\-.]{1,64}"
)
# `patient=123`, `subject:Patient=123`, `actor=...` inside free text such as a log line.
_HIDDEN_ASSIGNMENT = re.compile(
    r"(?<![\w-])("
    + "|".join(re.escape(key) for key in sorted(_HIDDEN_QUERY, key=len, reverse=True))
    + r")((?:[:.][\w\-]+)*)=([^&\s\"',;)]+)"
)
_HANDLER_NAME = "aiofhir-cli"


def _hidden(key: str) -> bool:
    base = re.split(r"[:.]", key, maxsplit=1)[0]
    return base in _HIDDEN_QUERY


def redact_path(path: str) -> str:
    """Replace the id after a resource type: `/Patient/123` -> `/Patient/[id]`."""
    parts = path.split("/")
    for index in range(1, len(parts)):
        previous, current = parts[index - 1], parts[index]
        if current and _PATH_TYPE.match(previous) and not current.startswith(("$", "_")):
            parts[index] = "[id]"
    return "/".join(parts)


def redact_url(url: str) -> str:
    """Hide resource ids in the path and person-identifying query values."""
    parts = urlsplit(url)
    path = redact_path(parts.path)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    hidden = [(key, REDACTED if _hidden(key) else _redact_refs(value)) for key, value in pairs]
    query = urlencode(hidden) if pairs else parts.query
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def redact_params(
    params: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]]:
    """Hide person-identifying search values."""
    if not params:
        return {}
    return {
        key: [REDACTED] if _hidden(key) else [_redact_refs(str(value)) for value in values]
        for key, values in params.items()
    }


def _redact_refs(text: str) -> str:
    """Replace the id in every person reference, whatever key or field it sits in."""
    return _PERSON_REF.sub(lambda match: f"{match.group(1)}/[id]", text)


def redact_text(text: str) -> str:
    """Scrub tokens, person references and identifying search values from free text.

    Best effort: a bare id with nothing around it (say, a subject on its own) cannot
    be recognized, so keep treating logs as sensitive.
    """
    text = _redact_refs(scrub(text))
    return _HIDDEN_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}={REDACTED}", text
    )


class RedactingFilter(logging.Filter):
    """Rewrites each record's message, traceback and stack with :func:`redact_text`.

    The traceback is formatted once and stored, redacted, in ``record.exc_text``;
    ``logging.Formatter`` uses a set ``exc_text`` instead of formatting
    ``exc_info`` again. ``exc_info`` itself is left for handlers that need the
    exception object, so attach this filter to every handler that writes text.
    """

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact_text(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


class IsoFormatter(logging.Formatter):
    """ISO 8601 timestamps with milliseconds and a UTC offset."""

    def __init__(self, fmt: str = LOG_FORMAT, *, utc: bool = False) -> None:
        super().__init__(fmt)
        self._utc = utc

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        moment = datetime.fromtimestamp(record.created, UTC)
        if not self._utc:
            moment = moment.astimezone()
        if datefmt:
            return moment.strftime(datefmt)
        return moment.isoformat(timespec="milliseconds")


ENV_LEVEL = "AIOFHIR_LOG_LEVEL"
_FALLBACK_NAME = "aiofhir-fallback"


class _FallbackHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Prints aiofhir records only while the application has no logging of its own.

    It decides on every record, not once: apps usually configure logging after
    their imports, and from then on
    their handlers get the records instead, in their format, exactly once.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if not logging.getLogger().handlers:
            super().emit(record)


def apply_env_level() -> None:
    """Apply ``AIOFHIR_LOG_LEVEL`` to the ``aiofhir`` logger. Called on import.

    Only the ``aiofhir`` logger changes: never the root logger, other libraries,
    or the app's handlers. Records reach the app's handlers as usual. While the
    app has configured no logging, one stderr handler with ISO 8601 timestamps
    and the redaction filter prints them. Unset, nothing changes.
    """
    raw = os.environ.get(ENV_LEVEL, "").strip()
    if not raw:
        return
    level = getattr(logging, raw.upper(), None)
    if not isinstance(level, int):
        warnings.warn(f"{ENV_LEVEL}={raw!r} is not a logging level; ignored", stacklevel=2)
        return
    logger = logging.getLogger("aiofhir")
    logger.setLevel(level)
    if any(handler.get_name() == _FALLBACK_NAME for handler in logger.handlers):
        return
    handler = _FallbackHandler()
    handler.set_name(_FALLBACK_NAME)
    handler.setFormatter(IsoFormatter(utc=os.environ.get("LOG_UTC", "") == "1"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)


def configure_logging(level_name: str | None = None, *, utc: bool | None = None) -> None:
    """Install one redacting stream handler on the root logger.

    Call this from scripts only. Calling it twice replaces the handler rather
    than adding a second one. Other handlers on the root logger are left alone.
    """
    raw = level_name if level_name is not None else os.environ.get("LOG_LEVEL", "INFO")
    level = getattr(logging, raw.strip().upper(), None)
    if not isinstance(level, int):
        raise ValueError(raw)
    use_utc = utc if utc is not None else os.environ.get("LOG_UTC", "") == "1"
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(IsoFormatter(utc=use_utc))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure_cli_logging() -> None:
    """Configure logging for a script. An unknown LOG_LEVEL exits."""
    try:
        configure_logging()
    except ValueError as exc:
        print(f"Set LOG_LEVEL to a logging level, not {exc}.", file=sys.stderr)
        raise SystemExit(1) from exc
