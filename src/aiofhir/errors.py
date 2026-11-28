"""Errors raised by the FHIR and SMART clients."""

from __future__ import annotations

from typing import Any

from aiofhir.log import redact_text


class AiofhirError(Exception):
    """Base error for aiofhir."""


class DiscoveryError(AiofhirError):
    """SMART discovery failed or the server cannot be used."""


class StateMismatch(AiofhirError):
    """The OAuth state parameter did not match the saved value."""


class TokenError(AiofhirError):
    """The token endpoint returned a response the client cannot use."""


class FhirError(AiofhirError):
    """A FHIR request failed.

    The message is redacted (tokens, person references and identifying search
    values), because servers often echo ids or names in their diagnostics and the
    message ends up in logs, tracebacks and user-facing output. The server's raw
    OperationOutcome, when there was one, is kept on `outcome`. It can hold PHI:
    do not log it.
    """

    def __init__(self, status: int, message: str, *, outcome: dict[str, Any] | None = None) -> None:
        self.status = status
        self.outcome = outcome
        super().__init__(f"FHIR request failed ({status}): {redact_text(message)}")


class AuthExpired(AiofhirError):
    """The health-record connection must be signed in again."""

    def __init__(self) -> None:
        super().__init__("Your health record connection expired. Please reconnect to continue.")
