from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_discovery_cache() -> None:
    from aiofhir.smart.discovery import clear_discovery_cache

    clear_discovery_cache()
