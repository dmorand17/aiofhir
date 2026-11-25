"""Haste Health discovery paths and offset paging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from aiofhir.profiles.base import ServerProfile

# Haste pages with `_offset` and returns no Bundle `next` link. Its default page is 50.
_DEFAULT_PAGE = 50


class HasteProfile(ServerProfile):
    name = "haste"

    def next_page(
        self,
        params: Mapping[str, Sequence[str]],
        received: int,
    ) -> dict[str, list[str]] | None:
        size = _first_int(params.get("_count"), _DEFAULT_PAGE)
        if size <= 0 or received < size:
            return None
        offset = _first_int(params.get("_offset"), 0) + received
        return {**{key: list(values) for key, values in params.items()}, "_offset": [str(offset)]}

    def discovery_paths(self, base: str) -> list[str]:
        primary = super().discovery_paths(base)[0]
        parsed = urlsplit(base.rstrip("/"))
        parts = [part for part in parsed.path.split("/") if part]
        try:
            index = parts.index("w")
            tenant = parts[index + 1]
            project = parts[index + 2]
        except (ValueError, IndexError):
            return [primary]
        path = f"/w/{tenant}/{project}/api/v1/.well-known/smart-configuration"
        fallback = f"{parsed.scheme}://{parsed.netloc}{path}"
        if fallback == primary:
            return [primary]
        return [primary, fallback]


def _first_int(values: Sequence[str] | None, default: int) -> int:
    try:
        return int(values[0]) if values else default
    except ValueError:
        return default
