"""Server profiles."""

from aiofhir.profiles.cerner import CernerProfile
from aiofhir.profiles.epic import EpicProfile
from aiofhir.profiles.generic import GenericProfile
from aiofhir.profiles.haste import HasteProfile

__all__ = ["CernerProfile", "EpicProfile", "GenericProfile", "HasteProfile"]
