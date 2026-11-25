"""Generic R4 server."""

from __future__ import annotations

from aiofhir.profiles.base import ServerProfile


class GenericProfile(ServerProfile):
    name = "generic"
