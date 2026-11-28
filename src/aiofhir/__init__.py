"""Async FHIR R4 and SMART on FHIR client."""

import logging
from importlib.metadata import version

from aiofhir.client import FhirClient
from aiofhir.errors import AuthExpired
from aiofhir.log import apply_env_level
from aiofhir.record import PatientRecord

# Applications decide where logs go. AIOFHIR_LOG_LEVEL opts in to aiofhir's own
# level (and a fallback handler while the app has none); unset, nothing changes.
logging.getLogger("aiofhir").addHandler(logging.NullHandler())
apply_env_level()

__version__ = version("aiofhir")

__all__ = [
    "AuthExpired",
    "FhirClient",
    "PatientRecord",
    "__version__",
]
