"""PKCE S256 helpers."""

from __future__ import annotations

import base64
import hashlib
import secrets


def make_verifier() -> str:
    """Return a 256-bit verifier of unreserved characters."""
    return secrets.token_urlsafe(32)


def make_state() -> str:
    """Return a 256-bit OAuth state value."""
    return secrets.token_urlsafe(32)


def challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
