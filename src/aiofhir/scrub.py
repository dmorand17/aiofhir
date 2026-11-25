"""Remove token material from text before it is stored or printed."""

from __future__ import annotations

import re

_TOKEN_FIELD = re.compile(
    r'"(access_token|refresh_token|id_token|secret)"\s*:\s*"[^"]*"',
)
_BEARER = re.compile(r"Bearer\s+\S+")


def scrub(text: str) -> str:
    """Replace token fields and bearer values. Other text is unchanged."""
    text = _TOKEN_FIELD.sub(r'"\1": "[redacted]"', text)
    return _BEARER.sub("Bearer [redacted]", text)
