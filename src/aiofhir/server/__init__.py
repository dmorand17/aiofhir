"""Reference backend for `RemoteRecord`. Install with `aiofhir[server]`.

`import aiofhir` never imports this package, so the core stays free of FastAPI.
"""

from aiofhir.server.app import RECORD_METHODS, create_app
from aiofhir.server.store import EncryptedStore

__all__ = ["RECORD_METHODS", "EncryptedStore", "create_app"]
