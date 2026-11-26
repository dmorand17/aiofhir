"""Remove token material from text before it is stored or printed."""

from __future__ import annotations

import re

REDACTED = "[redacted]"
# Field names whose values are credentials, wherever they appear.
SECRET_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "client_assertion",
    "secret",
    "password",
)
_KEYS = "|".join(SECRET_KEYS)
# JSON fields and Python dict reprs: "access_token": "..." or 'access_token': '...'.
_QUOTED_FIELD = re.compile(
    rf"(?P<q>[\"'])(?P<key>{_KEYS})(?P=q)(?P<sep>\s*:\s*)(?P<vq>[\"'])(?:\\.|(?!(?P=vq)).)*(?P=vq)",
    re.IGNORECASE,
)
# Form bodies and query strings: access_token=...&refresh_token=...
_ASSIGNED_FIELD = re.compile(
    rf"(?<![\w-])(?P<key>{_KEYS})=(?P<value>[^&\s\"',;]+)",
    re.IGNORECASE,
)
# RFC 6750 b64token after "Bearer", any case.
_BEARER = re.compile(r"\b(?P<scheme>bearer)\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
# HTTP Basic credentials, only in an Authorization header (so "Basic panel" text is kept).
_BASIC = re.compile(
    r"(?P<head>authorization[\"']?\s*[:=]\s*[\"']?basic)\s+[A-Za-z0-9+/]+=*",
    re.IGNORECASE,
)


def scrub(text: str) -> str:
    """Replace token fields, form or query token values, and bearer or basic credentials.

    Other text is unchanged.
    """
    text = _QUOTED_FIELD.sub(
        lambda m: f"{m['q']}{m['key']}{m['q']}{m['sep']}{m['vq']}{REDACTED}{m['vq']}", text
    )
    text = _ASSIGNED_FIELD.sub(lambda m: f"{m['key']}={REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m['scheme']} {REDACTED}", text)
    return _BASIC.sub(lambda m: f"{m['head']} {REDACTED}", text)
