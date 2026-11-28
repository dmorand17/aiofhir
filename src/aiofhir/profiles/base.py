"""Server profile hooks: per-server discovery and search quirks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


class ServerProfile:
    """Default SMART and FHIR behavior for an R4 server."""

    name = "generic"

    def discovery_paths(self, base: str) -> list[str]:
        root = base.rstrip("/")
        return [f"{root}/.well-known/smart-configuration"]

    def adapt_search(
        self,
        resource: str,
        params: Mapping[str, Sequence[str]],
    ) -> dict[str, list[str]]:
        del resource
        return {key: list(values) for key, values in params.items()}

    def next_page(
        self,
        params: Mapping[str, Sequence[str]],
        received: int,
    ) -> dict[str, list[str]] | None:
        """Search params for the page after one that had no `next` link, or None to stop."""
        del params, received
        return None


def discovery_fallback(base: str, tenant_project_path: str) -> str | None:
    """Build an absolute fallback URL on the same host, or None when it repeats the primary."""
    root = base.rstrip("/")
    parsed = urlsplit(root)
    fallback = urlunsplit((parsed.scheme, parsed.netloc, tenant_project_path, "", ""))
    primary = f"{root}/.well-known/smart-configuration"
    if fallback == primary:
        return None
    return fallback
