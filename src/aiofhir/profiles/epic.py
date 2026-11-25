"""Epic. Sort on the client; Epic rejects `_sort`."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from aiofhir.profiles.base import ServerProfile

logger = logging.getLogger(__name__)


class EpicProfile(ServerProfile):
    name = "epic"

    def adapt_search(
        self,
        resource: str,
        params: Mapping[str, Sequence[str]],
    ) -> dict[str, list[str]]:
        adapted = super().adapt_search(resource, params)
        if "_sort" in adapted:
            logger.debug("dropping _sort for %s", resource)
            adapted.pop("_sort")
        return adapted
