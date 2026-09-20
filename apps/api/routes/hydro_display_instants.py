"""RFC3339 instant validation, canonical time formatting and the national tile identity.

Split out of `apps/api/routes/hydro_display.py` (#2026).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BeforeValidator

from apps.api.errors import ApiError
from apps.api.routes.hydro_display_constants import (
    HYDRO_NATIONAL_SOURCE_ID,
    HYDRO_NATIONAL_SOURCE_VERSION,
)
from services.tiles.mvt import TileInput, canonical_mvt_time, public_hydro_layer_id

# RFC3339 at seconds precision, with an optional fractional part and a
# mandatory offset: `2026-09-02T12:00:00Z`, `...T12:00:00.000Z`,
# `...T12:00:00+00:00`, `...T20:00:00+08:00`. A fractional part is matched here
# and rejected later by `_require_seconds_precision_instant`, so `.000` still
# canonicalizes while `.500` gets the precision message rather than a shape one.
_RFC3339_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _reject_non_rfc3339_instant_text(value: Any) -> Any:
    """Gate the raw path segment before pydantic's lax datetime coercion runs.

    `cycle: datetime` on its own accepts spellings the tile contract does not
    define an answer for: a bare Unix epoch (`1756814400` -> 2025-09-02T12:00Z),
    an offset-less `2026-09-02T12:00:00`, and a space-separated
    `2026-09-02 12:00:00` all coerce and reach the tile SQL. The contract's
    "Invalid tile" scenario requires a 422 without expensive SQL instead, so the
    string shape is checked first and the value handed on unchanged for pydantic
    to parse.

    Raises `ValueError`, never `ApiError`: only `ValueError`/`AssertionError`
    become a `RequestValidationError`, which the app's handler renders as the
    same 422 `VALIDATION_ERROR` an out-of-enum `source` produces. Any other
    exception escapes dependency solving as a 500.
    """
    if isinstance(value, str) and not _RFC3339_INSTANT_RE.match(value):
        raise ValueError("Input should be an RFC3339 instant, e.g. 2026-09-02T12:00:00Z")
    return value


# Only the canonical `{source}/{cycle}` national route uses this. The legacy
# routes' `valid_time: datetime` laxness is pre-existing and deliberately left
# alone here.
Rfc3339Instant = Annotated[datetime, BeforeValidator(_reject_non_rfc3339_instant_text)]


def _validated_national_valid_time_selector(
    *,
    layer_id: str,
    run_id: str | None,
    source: str | None,
    cycle: datetime | None,
) -> datetime | None:
    """Fail closed on every half-formed national valid-time selector, before any SQL.

    A source alone has no defined window, a cycle alone has no source to resolve
    it against, `run_id` names a different identity than `(source, cycle)` does,
    and only `discharge` has a national source/cycle contract at all. Each of
    those is 422 rather than a silently-ignored argument, which would otherwise
    serve gfs times under an ifs request.
    """
    if source is None and cycle is None:
        return None
    details: dict[str, Any] = {"layer_id": layer_id, "source": source, "run_id": run_id}
    if layer_id != "discharge":
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="source/cycle valid-time discovery is defined for the discharge layer only.",
            details=details,
        )
    if source is None or cycle is None:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="source and cycle must be given together.",
            details=details,
        )
    if run_id is not None:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="run_id cannot be combined with source/cycle: they name different identities.",
            details=details,
        )
    return _require_seconds_precision_instant(cycle, "cycle")


def _format_time(value: Any) -> str:
    return canonical_mvt_time(value) or str(value)


def _require_seconds_precision_instant(value: datetime, field_name: str) -> datetime:
    """Reject a sub-second or out-of-range instant, and normalize the rest to UTC.

    `canonical_mvt_time` does not truncate: a non-zero microsecond round-trips
    as `...:00.500000Z`. Truncating one here would serve the `12:00:00` tile
    under a `12:00:00.500Z` request, so this is a 422 instead. The
    zero-microsecond spellings the contract is written for -- `...T12:00:00Z`,
    `...T12:00:00.000Z`, `...T12:00:00+00:00`, and a non-UTC
    `...T20:00:00+08:00` -- are all accepted and collapse onto one instant, one
    SQL bind and one cache key.

    The range check and the UTC normalization both live in
    `_require_representable_instant`, which the two legacy tile routes call on
    their own (they must NOT inherit the sub-second rejection above -- they have
    always accepted an in-range `...T12:00:00.500Z`). Only the sub-second gate is
    this function's own; everything else, including the exact success-path return
    value, is that helper's contract.
    """
    if value.microsecond:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Tile time instants must be RFC3339 with seconds precision.",
            details={field_name: value.isoformat(), "expected_format": "YYYY-MM-DDTHH:MM:SSZ"},
        )
    return _require_representable_instant(value, field_name)


def _require_representable_instant(value: datetime, field_name: str) -> datetime:
    """Normalize an instant to UTC, or 422 if that shift leaves `datetime`'s range.

    Extracted from `_require_seconds_precision_instant` so ONE implementation
    serves all three instant-taking tile routes plus `list_layer_valid_times` and
    the two precip routes (#2033). A sibling copy per route is exactly how the
    error body drifts.

    `9999-12-31T23:59:59-08:00` and `0001-01-01T00:00:00+08:00` are well-formed
    RFC3339, so every string-shape gate passes them, and `astimezone(UTC)` then
    raises `OverflowError` -- an HTTP 500 on a public URL, and on the legacy
    routes a 500 charged AFTER their SQL. They are bad requests, so they get the
    same 422 every other rejected instant gets, before any statement runs.

    THE TERNARY IS THE CONTRACT, not an implementation detail:

    - the naive branch is load-bearing because `valid_time: datetime` is lax on
      both legacy tile aliases, so a NAIVE instant is a real input class there; a
      flat `value.astimezone(UTC)` would reinterpret it in SERVER-LOCAL time
      (macOS local vs node-27 UTC+8) and newly 422 a naive extreme, and
    - the return must stay UTC-NORMALIZED rather than the caller's original
      object, because `precip.py::_require_whole_hour_instant` reads
      `.minute`/`.second` off it and `_RFC3339_INSTANT_RE` accepts half-hour
      offsets: `2026-09-02T20:00:00+05:30` (= 14:30 UTC) would otherwise pass the
      whole-hour gate and be floored to hour 14 by `cycle_token`.

    Raising `ApiError` here, rather than in `services/tiles/mvt.py`, is the
    layering: the tile helper raises the domain-level `MvtTimeOutOfRangeError` and
    knows nothing about HTTP. This guard runs BEFORE the helper is ever reached
    with an out-of-range user instant, so no import of that error is needed.
    """
    try:
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    except (OverflowError, ValueError) as exc:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Tile time instants must be representable in UTC.",
            details={field_name: value.isoformat(), "expected_format": "YYYY-MM-DDTHH:MM:SSZ"},
        ) from exc


def _national_source_cycle_tile_input(
    *,
    source: str,
    cycle_text: str,
    variable: str,
    valid_time: Any,
    z: int,
    x: int,
    y: int,
    source_digest: str,
) -> TileInput:
    """Cache identity for the canonical national tile.

    `source` and the canonicalized `cycle` ride in `source_version`, so
    `cache_key` -- and the file cache path derived from it -- separate two
    identities that share a variable/valid_time/z/x/y. The ETag deliberately
    does not: `stable_etag` hashes tile bytes only and is shared by all five
    tile layers.

    Factored out of the route so the identity can be asserted without a
    database.
    """
    return TileInput(
        layer_id=public_hydro_layer_id(variable),
        source_id=HYDRO_NATIONAL_SOURCE_ID,
        source_version=f"{HYDRO_NATIONAL_SOURCE_VERSION}:{source}:{cycle_text}:{source_digest}",
        valid_time=_format_time(valid_time),
        z=z,
        x=x,
        y=y,
        variant_id=f"variable:{variable}",
    )
