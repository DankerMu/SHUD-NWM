"""Typed failures of the precipitation raster service.

Every payload carries RELATIVE object keys, never absolute mirror paths: the
route renders them into `details` and a 404 body must not leak the serving
node's filesystem layout.
"""

from __future__ import annotations

from datetime import datetime


class PrecipError(RuntimeError):
    """Base class for the fail-closed precipitation errors."""


class PrecipCycleNotMirrored(PrecipError):
    """The requested cycle has no `canonical/<S>/<K>/prcp_rate_or_amount/` tree.

    The mirrored-cycle check is route-level (pinned decision 9) and never part
    of `resolve_window`: the lead-0 window is legitimately served entirely by
    earlier cycles, so the resolver must not require the requested cycle
    itself. The display routes answer that gate with `ApiError` directly, so
    nothing in this repository raises this class today; it is reserved for a
    future library caller (`cycle_is_mirrored` is the predicate to use).
    """

    def __init__(self, *, reason: str = "cycle_not_mirrored") -> None:
        super().__init__(reason)
        self.reason = reason


class PrecipWindowIncomplete(PrecipError):
    """The 24h window cannot be assembled from cycles at or before the request.

    `missing` names the absent slice's relative object key; it is `None` when the
    failure is that no mirrored cycle precedes `window_end` at all (the oldest
    retained cycle's first 24h window, which is reachable in production).
    """

    def __init__(
        self,
        *,
        missing: str | None = None,
        reason: str = "missing_slice",
        window_end: datetime | None = None,
    ) -> None:
        super().__init__(missing or reason)
        self.missing = missing
        self.reason = reason
        self.window_end = window_end


class PrecipSliceInvalid(PrecipError):
    """A mirrored slice or grid definition does not match the canonical contract."""

    def __init__(self, object_key: str, reason: str) -> None:
        super().__init__(f"{object_key}: {reason}")
        self.object_key = object_key
        self.reason = reason
