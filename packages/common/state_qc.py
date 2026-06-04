"""SHUD initial-condition (``*.cfg.ic``) state-variable QC.

Parses a SHUD ``.cfg.ic`` restart/state file and validates structural and physical
integrity before a snapshot is allowed to become a usable warm-start source:

- row counts match expected mesh / river / lake element counts
- per-variable range and non-negativity checks for the element state variables
  (canopy, snow, surface, unsat, groundwater, river-stage, lake-stage if present)
- (optional) restart first-step water-balance delta within threshold for soil
  moisture / groundwater / channel storage

The SHUD ``.cfg.ic`` text layout (SHUD ``Model_Data::read_ic`` convention) is::

    <header line>           # whitespace tokens; tokens[0..] = counts, last numeric = minute-time
    <mesh block>            # one row per mesh cell, columns = mesh state variables
    <river block>          # one row per river segment, columns = river state variables
    [<lake block>]          # optional, one row per lake, columns = lake state variables

Different SHUD builds emit a slightly different number of leading count tokens and
a header minute-time. Rather than hard-code a brittle column map, the parser is
tolerant: it splits the file into numeric blocks and validates the *first*
``mesh_count`` data rows as mesh state, the next ``river_count`` rows as river
state, and (if present) ``lake_count`` rows as lake state. Column semantics are
applied by position with documented indices, and unknown extra columns are
range-checked generically (finite + non-negative for storage columns).

Parsing failure is itself a QC failure (never a crash): a malformed or truncated
IC file returns ``passed=False`` with a reason rather than raising.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Upper bound (bytes) on a SHUD ``.cfg.ic`` state file the QC parser will read into
# memory. Real per-basin restart states are far smaller; a file above this bound is
# treated as a QC failure (corrupt / wrong artifact) rather than read unboundedly into
# memory (OOM protection). 64 MiB matches the limited-read ceiling used elsewhere.
MAX_STATE_IC_BYTES = 64 * 1024 * 1024

# Mesh state-variable column layout (SHUD element IC columns, by position).
# Index 0 is the element id; storage/stage state columns follow. These are the
# columns that must be finite and non-negative (depths/storages cannot be < 0).
# Canopy(interception), Snow, Surface(overland), Unsat(soil moisture), Groundwater.
_MESH_STATE_COLUMNS = ("canopy", "snow", "surface", "unsat", "groundwater")

# River state columns (by position after the id): river stage / channel storage.
_RIVER_STATE_COLUMNS = ("river_stage",)

# Lake state columns (by position after the id): lake stage.
_LAKE_STATE_COLUMNS = ("lake_stage",)

# Physically plausible upper bound (metres) for any single storage/stage column.
# Values above this are treated as corrupt (range failure). Generous on purpose;
# real SHUD depths are << 1000 m.
_MAX_STATE_VALUE_M = 1.0e6


@dataclass(frozen=True)
class StateQCResult:
    passed: bool
    checks: dict[str, Any]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": dict(self.checks), "reason": self.reason}


def run_state_variable_qc(
    ic_path: Path | str,
    *,
    expected_mesh_count: int | None = None,
    expected_river_count: int | None = None,
    expected_lake_count: int | None = None,
    water_balance: Mapping[str, Any] | None = None,
) -> StateQCResult:
    """Parse and QC a SHUD ``.cfg.ic`` file.

    ``expected_*`` counts come from the model manifest / accompanying metadata. When
    ``None`` the corresponding row-count check is skipped (structure is still parsed).

    ``water_balance`` (optional) carries the restart first-step balance deltas and a
    threshold; if absent the water-balance check is reported as ``skipped`` (TODO:
    wired in Lane 2 once first-step storage diagnostics are available).
    """

    checks: dict[str, Any] = {
        "ic_path": str(ic_path),
        "parsed": False,
        "row_counts": None,
        "range": None,
        "water_balance": "skipped",
    }
    try:
        blocks = _parse_ic_file(Path(ic_path))
    except (OSError, ValueError) as error:
        checks["parse_error"] = str(error)
        return StateQCResult(passed=False, checks=checks, reason=f"IC parse failed: {error}")

    checks["parsed"] = True
    mesh_rows, river_rows, lake_rows = blocks

    # Row-count check against expected element counts.
    row_counts = {
        "mesh": len(mesh_rows),
        "river": len(river_rows),
        "lake": len(lake_rows),
        "expected_mesh": expected_mesh_count,
        "expected_river": expected_river_count,
        "expected_lake": expected_lake_count,
    }
    checks["row_counts"] = row_counts
    count_reason = _check_row_counts(row_counts)
    if count_reason is not None:
        return StateQCResult(passed=False, checks=checks, reason=count_reason)

    # Range / non-negative checks per block.
    range_report: dict[str, Any] = {}
    range_reason = _check_block_range("mesh", mesh_rows, _MESH_STATE_COLUMNS, range_report)
    if range_reason is None:
        range_reason = _check_block_range("river", river_rows, _RIVER_STATE_COLUMNS, range_report)
    if range_reason is None and lake_rows:
        range_reason = _check_block_range("lake", lake_rows, _LAKE_STATE_COLUMNS, range_report)
    checks["range"] = range_report
    if range_reason is not None:
        return StateQCResult(passed=False, checks=checks, reason=range_reason)

    # Restart first-step water-balance delta (optional this Lane; TODO Lane 2).
    wb_reason = _check_water_balance(water_balance, checks)
    if wb_reason is not None:
        return StateQCResult(passed=False, checks=checks, reason=wb_reason)

    return StateQCResult(passed=True, checks=checks, reason=None)


def _parse_ic_file(path: Path) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    """Split a ``.cfg.ic`` file into mesh / river / lake numeric data rows.

    Returns three lists of float rows. Raises ValueError on a structurally
    unusable file (empty, no numeric rows, non-numeric tokens in data rows).
    """

    # Bounded read (OOM protection): read at most one byte past the limit so an
    # oversized file is detected without being slurped whole into memory. The path here
    # is a trusted local IC file (the snapshot layer stages it before calling), so a
    # plain bounded read is used rather than the no-follow safe-fs reader (which would
    # reject legitimate symlinked temp dirs such as macOS /tmp).
    data = _read_bytes_limited(path, max_bytes=MAX_STATE_IC_BYTES)
    if len(data) > MAX_STATE_IC_BYTES:
        raise ValueError(f"IC file exceeds size limit of {MAX_STATE_IC_BYTES} bytes")
    try:
        raw = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"IC file is not valid UTF-8: {error}") from error
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty IC file")

    header = lines[0].split()
    counts = _header_counts(header)
    if counts is None:
        raise ValueError(f"unreadable IC header: {lines[0]!r}")
    mesh_count, river_count, lake_count = counts

    data_rows: list[list[float]] = []
    for line in lines[1:]:
        row = _numeric_row(line)
        if row is None:
            raise ValueError(f"non-numeric IC data row: {line!r}")
        data_rows.append(row)

    total_expected = mesh_count + river_count + lake_count
    if mesh_count <= 0:
        raise ValueError(f"non-positive mesh count in header: {mesh_count}")
    if len(data_rows) < mesh_count + river_count:
        raise ValueError(
            f"truncated IC body: have {len(data_rows)} rows, header implies >= {mesh_count + river_count}"
        )

    mesh_rows = data_rows[:mesh_count]
    river_rows = data_rows[mesh_count : mesh_count + river_count]
    lake_rows: list[list[float]] = []
    if lake_count > 0:
        # The header declares lakes; the body MUST contain them. A short body is a
        # structural inconsistency (truncated / wrong artifact), not an empty-lake
        # state -- silently dropping the lakes would mask a corrupt restart file.
        if len(data_rows) < total_expected:
            raise ValueError(
                f"header declares lake_count={lake_count} but body has only "
                f"{len(data_rows)} rows (< {total_expected} = mesh+river+lake)"
            )
        lake_rows = data_rows[mesh_count + river_count : total_expected]
    return mesh_rows, river_rows, lake_rows


def _read_bytes_limited(path: Path, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` bytes from a trusted local IC file.

    Reading one byte past the limit lets the caller detect (and reject) an oversized
    file without ever materialising more than ``max_bytes + 1`` bytes in memory.
    """

    with open(path, "rb") as handle:
        return handle.read(max_bytes + 1)


def _header_counts(header: Sequence[str]) -> tuple[int, int, int] | None:
    """Extract (mesh, river, lake) counts from the header tokens.

    SHUD IC headers lead with integer element counts and end with a minute-time token.
    The minute-time may itself be integer-valued (e.g. ``27000000.000000``), so it
    cannot be distinguished from a count by integer-ness alone. We therefore take the
    LAST numeric token as the minute-time and the integer-valued tokens BEFORE it as
    the (mesh, river, lake) counts. lake defaults to 0 when absent.
    """

    numeric = [value for value in (_as_float(token) for token in header) if value is not None]
    if len(numeric) < 2:
        # Need at least one count token plus the trailing minute-time.
        return None
    # Drop the trailing minute-time; the remaining tokens are the integer counts.
    count_values = numeric[:-1]
    ints: list[int] = []
    for value in count_values:
        if not float(value).is_integer():
            # A fractional token among the counts marks an earlier minute-time / malformed
            # header; counts must precede it.
            break
        ints.append(int(value))
        if len(ints) == 3:
            break
    if not ints:
        return None
    mesh = ints[0]
    river = ints[1] if len(ints) > 1 else 0
    lake = ints[2] if len(ints) > 2 else 0
    return mesh, river, lake


def cfg_ic_header_minute_index(header_tokens: Sequence[str]) -> int | None:
    """Return the position of the SHUD IC header minute-time token, or None.

    Shares the "LAST numeric token is the minute-time" rule with ``_header_counts``
    so every consumer (state QC, runtime header read, runtime time shift) interprets
    3-token ``<mesh> <river> <minute-time>`` and 4-token
    ``<mesh> <river> <lake> <minute-time>`` headers identically. Returns the index
    into ``header_tokens`` of that trailing numeric token. None when there are fewer
    than two numeric tokens (no count + minute-time pair) or none at all.
    """

    numeric_indices = [
        index for index, token in enumerate(header_tokens) if _as_float(token) is not None
    ]
    if len(numeric_indices) < 2:
        # Need at least one count token plus the trailing minute-time.
        return None
    return numeric_indices[-1]


def cfg_ic_header_minute_time(header_tokens: Sequence[str]) -> float | None:
    """Return the SHUD IC header minute-time value, or None.

    Uses :func:`cfg_ic_header_minute_index` so the minute-time is read from the
    LAST numeric token regardless of whether a lake count is present.
    """

    index = cfg_ic_header_minute_index(header_tokens)
    if index is None:
        return None
    return _as_float(header_tokens[index])


def _numeric_row(line: str) -> list[float] | None:
    tokens = line.split()
    row: list[float] = []
    for token in tokens:
        value = _as_float(token)
        if value is None:
            return None
        row.append(value)
    return row or None


def _check_row_counts(row_counts: Mapping[str, Any]) -> str | None:
    for kind in ("mesh", "river", "lake"):
        expected = row_counts.get(f"expected_{kind}")
        if expected is None:
            continue
        actual = row_counts.get(kind, 0)
        if int(actual) != int(expected):
            return f"{kind} row count {actual} != expected {expected}"
    return None


def _check_block_range(
    kind: str,
    rows: list[list[float]],
    columns: Sequence[str],
    report: dict[str, Any],
) -> str | None:
    """Validate finiteness, non-negativity, and bounds of state columns.

    Column 0 is treated as the element id (ignored for non-negativity bounds beyond
    finiteness). Named state columns plus any extra trailing storage columns must be
    finite, non-negative, and within ``_MAX_STATE_VALUE_M``.
    """

    block_report: dict[str, Any] = {"rows": len(rows), "violations": 0}
    report[kind] = block_report
    # Each row must carry the element id (column 0) plus every expected state column.
    # A short row means missing state variables -- a structural QC failure, not a row
    # to be silently range-checked on whatever columns happen to be present.
    min_columns = 1 + len(columns)
    for index, row in enumerate(rows):
        if len(row) < min_columns:
            block_report["violations"] += 1
            return f"{kind} row {index} missing state columns (have {len(row)}, need >= {min_columns})"
        # Validate all columns are finite; element id is column 0.
        for col_index, value in enumerate(row):
            if not math.isfinite(value):
                block_report["violations"] += 1
                return f"{kind} row {index} column {col_index} is not finite ({value})"
            # State columns (everything after the id) must be non-negative & bounded.
            if col_index >= 1:
                if value < 0.0:
                    block_report["violations"] += 1
                    name = columns[col_index - 1] if col_index - 1 < len(columns) else f"col{col_index}"
                    return f"{kind} row {index} {name} is negative ({value})"
                if value > _MAX_STATE_VALUE_M:
                    block_report["violations"] += 1
                    name = columns[col_index - 1] if col_index - 1 < len(columns) else f"col{col_index}"
                    return f"{kind} row {index} {name} exceeds bound ({value} > {_MAX_STATE_VALUE_M})"
    return None


def _check_water_balance(water_balance: Mapping[str, Any] | None, checks: dict[str, Any]) -> str | None:
    """Restart first-step water-balance delta check (optional this Lane).

    Expects ``water_balance`` like::

        {"threshold": 0.05,
         "deltas": {"soil_moisture": 0.01, "groundwater": 0.0, "channel_storage": 0.02}}

    Returns a reason string if any delta exceeds the threshold, else None. When
    ``water_balance`` is absent the check is reported as skipped (TODO: Lane 2 wires
    first-step storage diagnostics from the SHUD restart segment).
    """

    if not water_balance:
        checks["water_balance"] = "skipped"
        return None

    threshold = _as_float(water_balance.get("threshold"))
    deltas = water_balance.get("deltas") or {}
    report: dict[str, Any] = {"threshold": threshold, "deltas": dict(deltas), "passed": True}
    checks["water_balance"] = report
    if threshold is None:
        report["passed"] = True
        report["note"] = "no threshold provided; skipped"
        return None
    for name, raw in deltas.items():
        delta = _as_float(raw)
        if delta is None:
            continue
        if abs(delta) > threshold:
            report["passed"] = False
            return f"water-balance delta {name}={delta} exceeds threshold {threshold}"
    return None


def _as_float(token: Any) -> float | None:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None
