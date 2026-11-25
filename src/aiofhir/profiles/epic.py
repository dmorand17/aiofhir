"""Epic. Sort on the client; Epic rejects `_sort`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aiofhir.profiles.base import ServerProfile


class EpicProfile(ServerProfile):
    name = "epic"

    def adapt_search(
        self,
        resource: str,
        params: Mapping[str, Sequence[str]],
    ) -> dict[str, list[str]]:
        adapted = super().adapt_search(resource, params)
        adapted.pop("_sort", None)
        return adapted
