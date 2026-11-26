"""Signed client assertions for `private_key_jwt`. Needs the `aiofhir[jwt]` extra."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ALGORITHMS = ("RS384", "ES384")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# SMART Backend Services: the assertion lives at most five minutes.
_LIFETIME = timedelta(minutes=5)


@dataclass(frozen=True)
class ClientAssertionKey:
    """The app's private key, as registered (public half) with the authorization server."""

    private_key_pem: bytes | str = field(repr=False)
    key_id: str
    algorithm: str = "RS384"

    def __post_init__(self) -> None:
        if self.algorithm not in ALGORITHMS:
            raise ValueError("algorithm must be RS384 or ES384")

    def sign(self, client_id: str, token_endpoint: str, now: datetime) -> str:
        """A one-time assertion: iss and sub are the client, aud the token endpoint."""
        import jwt  # the [jwt] extra; imported only when an assertion is needed

        claims = {
            "iss": client_id,
            "sub": client_id,
            "aud": token_endpoint,
            "exp": int((now + _LIFETIME).timestamp()),
            "jti": secrets.token_urlsafe(32),
        }
        headers = {"kid": self.key_id, "typ": "JWT"}
        return jwt.encode(claims, self.private_key_pem, algorithm=self.algorithm, headers=headers)
