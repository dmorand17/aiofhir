"""Async FHIR R4 and SMART on FHIR client."""

import logging

from aiofhir.client import FhirClient
from aiofhir.errors import AuthExpired
from aiofhir.record import PatientRecord

# Libraries never configure handlers. Applications decide where logs go.
logging.getLogger("aiofhir").addHandler(logging.NullHandler())

__version__ = "0.0.0"

__all__ = ["AuthExpired", "FhirClient", "PatientRecord", "__version__"]
