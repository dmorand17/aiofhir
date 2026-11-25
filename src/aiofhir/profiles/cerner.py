"""Oracle Health (Cerner). Search quirks are filled in during the sandbox pass."""

from __future__ import annotations

from aiofhir.profiles.base import ServerProfile


class CernerProfile(ServerProfile):
    name = "cerner"
