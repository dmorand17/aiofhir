"""Errors raised by the FHIR and SMART clients."""

from __future__ import annotations


class AiofhirError(Exception):
    """Base error for aiofhir."""


class DiscoveryError(AiofhirError):
    """SMART discovery failed or the server cannot be used."""


class StateMismatch(AiofhirError):
    """The OAuth state parameter did not match the saved value."""


class TokenError(AiofhirError):
    """The token endpoint returned a response the client cannot use."""


class FhirError(AiofhirError):
    """A FHIR request failed."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"FHIR request failed ({status}): {message}")


class AuthExpired(AiofhirError):
    """The health-record connection must be signed in again."""

    def __init__(self) -> None:
        super().__init__("Your health record connection expired. Please reconnect to continue.")
