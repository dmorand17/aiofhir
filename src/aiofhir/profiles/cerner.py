"""Oracle Health (Cerner). Appointment search requires both date bounds.

Other Cerner quirks stay for the sandbox pass.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from aiofhir.profiles.base import ServerProfile

logger = logging.getLogger(__name__)

# Far upper bound used only when a caller sent `ge` and no `lt`.
_OPEN_UPPER = "lt9999-12-31T00:00:00Z"


class CernerProfile(ServerProfile):
    name = "cerner"

    def adapt_search(
        self,
        resource: str,
        params: Mapping[str, Sequence[str]],
    ) -> dict[str, list[str]]:
        adapted = super().adapt_search(resource, params)
        if resource != "Appointment":
            return adapted
        dates = adapted.get("date", [])
        has_lower = any(value.startswith("ge") for value in dates)
        has_upper = any(value.startswith("lt") for value in dates)
        if has_lower and not has_upper:
            logger.debug("adding an open Appointment date bound")
            adapted["date"] = [*dates, _OPEN_UPPER]
        return adapted
