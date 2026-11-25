"""Async FHIR R4 and SMART on FHIR client."""

from aiofhir.client import FhirClient
from aiofhir.errors import AuthExpired
from aiofhir.record import PatientRecord

__version__ = "0.0.0"

__all__ = ["AuthExpired", "FhirClient", "PatientRecord", "__version__"]
