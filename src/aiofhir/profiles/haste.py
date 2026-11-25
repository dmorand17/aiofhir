"""Haste Health discovery paths."""

from __future__ import annotations

from urllib.parse import urlsplit

from aiofhir.profiles.base import ServerProfile


class HasteProfile(ServerProfile):
    name = "haste"

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
