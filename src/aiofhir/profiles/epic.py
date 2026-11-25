"""Epic. Search quirks are filled in during the sandbox pass."""

from __future__ import annotations

from aiofhir.profiles.base import ServerProfile


class EpicProfile(ServerProfile):
    name = "epic"
