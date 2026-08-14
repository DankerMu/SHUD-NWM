"""Unit tests for the node-27 timeseries retention runner (issue #855 §6.1 + §6.2).

#1370 retired the archive lane, so H1/H2/H8/H9 (the completeness and drill
gates, their freshness rules, and the salvage-backed-window derivation) and
their thirteen wire codes are gone; the archive gate is a required explicit
``disabled`` acknowledgement, nothing else. What remains under test:

- D1 archive-gate resolution: only explicit ``disabled`` runs; unset, the
  retired ``enabled``, and every other value refuse with
  RETENTION_CONFIG_INVALID, exit 2, no receipt, retirement diagnostics.
- H3 per-tick bound + deferred_remainder.
- H4 freed_bytes measured BEFORE drop (mock-ordering assertion).
- H5 per-chunk drop failure → whole-tick refused (H5 fail-closed).
- H6 wire codes byte-identical across code / runbook §8.2, and shrunk to the
  four runner-own codes.
- H7 boundary predicate ``range_end <= cutoff``.
- H10 _default_lock_path() byte-identity + zero-arg signature parity.
- H11 governance registration (covered in test_node27_resource_governance.py).
- H17 zero-eligible enforce → outcome=enforced, all arrays empty, exit 0.
- Config parse fail-closed rows.
- Concurrent-invocation flock path → RETENTION_CONCURRENT_INVOCATION.
- Uncaught error path → RETENTION_UNCAUGHT_ERROR.
- #1213 credential redaction of every persisted error surface (receipt file
  bytes + stderr/wrapper log) on the drop-phase and uncaught-fallback paths.
- Receipt schema 1.1 (unmodified by #1370) + the ``archive_gate`` block.
- CLI + wrapper contract.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import inspect
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema
import psycopg2
import pytest

# Imported eagerly (not inside a test): the runner defers this import to keep
# psycopg2 out of module import, and these tests monkeypatch ``psycopg2`` with
# a fake — importing ``packages.common.redaction`` (which imports
# ``psycopg2.extensions`` at module scope) under that fake would fail. Loading
# it here caches the real module before any fake is installed. The same reason
# applies to the ``psycopg2`` import above: the connect-failure row needs the
# REAL ``psycopg2.OperationalError`` class, which the fake does not carry.
from packages.common.redaction import REDACTION_MARKER
from packages.common.storage import DEFAULT_RETENTION_WINDOW_DAYS
from scripts import node27_timeseries_retention as retention

_ROOT = Path(__file__).resolve().parents[1]
_RECEIPT_SCHEMA_PATH = _ROOT / "schemas/timeseries_retention_receipt.schema.json"
_RUNBOOK_PATH = _ROOT / "docs/runbooks/tier-node27-timeseries-storage.md"
_DESIGN_PATH = _ROOT / "openspec/changes/tier-node27-timeseries-storage/design.md"
_WRAPPER_PATH = _ROOT / "scripts/node27_timeseries_retention_once.sh"
_SERVICE_PATH = _ROOT / "infra/systemd/nhms-node27-timeseries-retention.service"
_TIMER_PATH = _ROOT / "infra/systemd/nhms-node27-timeseries-retention.timer"
_ENV_EXAMPLE_PATH = _ROOT / "infra/env/node27-timeseries-retention.example"

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
_DROP_WINDOW_DAYS = 14


def _cutoff(now: datetime = _NOW, days: int = _DROP_WINDOW_DAYS) -> datetime:
    return now - timedelta(days=days)


def _load_schema() -> dict:
    return json.loads(_RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {
        "enforce": False,
        "dry_run": False,
        "receipt_path": None,
        "lock_path": None,
        "archive_gate": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal schema-valid receipts.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _chunk(
    schema: str,
    hyper: str,
    label: str,
    *,
    now: datetime = _NOW,
    delta_days: float,
    is_compressed: bool = False,
    duration_days: int = 7,
) -> retention.ChunkRow:
    end = now - timedelta(days=delta_days)
    start = end - timedelta(days=duration_days)
    return retention.ChunkRow(
        hypertable_schema=schema,
        hypertable_name=hyper,
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=start,
        range_end=end,
        is_compressed=is_compressed,
    )


def _build_config(tmp_path: Path, *, enforce: bool = False, **overrides: Any) -> retention.RetentionConfig:
    kwargs: dict[str, Any] = {
        "database_url": "postgresql://user:pw@127.0.0.1:55432/nhms",
        "window_days": _DROP_WINDOW_DAYS,
        "per_tick_bound": 5,
        "receipt_path": tmp_path / "receipt.json",
        "lock_path": tmp_path / "runner.lock",
        "enforce": enforce,
    }
    kwargs.update(overrides)
    return retention.RetentionConfig(**kwargs)


class _StubRunner:
    """Records fetch/measure/drop invocations in call order for H4 mock ordering."""

    def __init__(
        self,
        chunks: Sequence[retention.ChunkRow],
        *,
        measured: Mapping[str, int] | None = None,
        drop_error: Mapping[str, Exception] | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._measured = dict(measured) if measured is not None else None
        self._drop_error = dict(drop_error) if drop_error else {}
        self.calls: list[tuple[str, Any]] = []

    def fetch(self, config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        self.calls.append(("fetch", cutoff))
        return list(self._chunks)

    def measure(
        self, config: retention.RetentionConfig, chunks: Sequence[retention.ChunkRow]
    ) -> dict[str, int]:
        self.calls.append(("measure", tuple(c.qualified_name for c in chunks)))
        if self._measured is not None:
            return {c.qualified_name: self._measured.get(c.qualified_name, 0) for c in chunks}
        return {c.qualified_name: 10_000 for c in chunks}

    def drop(self, config: retention.RetentionConfig, chunk: retention.ChunkRow) -> None:
        self.calls.append(("drop", chunk.qualified_name))
        if chunk.chunk_name in self._drop_error:
            raise self._drop_error[chunk.chunk_name]


# ---------------------------------------------------------------------------
# H6 wire-code frozenset
# ---------------------------------------------------------------------------


# #1370: the thirteen archive-family codes retired with the archive lane
# (ADR 0002 Revision 2026-08-11). Only the four runner-own codes survive.
_EXPECTED_WIRE_CODES = frozenset(
    {
        "RETENTION_CONFIG_INVALID",
        "RETENTION_CONCURRENT_INVOCATION",
        "RETENTION_DROP_FAILED",
        "RETENTION_UNCAUGHT_ERROR",
    }
)

# The retired archive-family codes, spelled out so the negative assertion is
# an oracle rather than an echo of the module.
_RETIRED_ARCHIVE_FAMILY_CODES = frozenset(
    {
        "COMPLETENESS_RECEIPT_MISSING",
        "COMPLETENESS_RECEIPT_STALE",
        "COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT",
        "COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW",
        "COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW",
        "DRILL_RECEIPT_MISSING",
        "DRILL_RECEIPT_STALE",
        "DRILL_RECEIPT_FAIL",
        "DRILL_DERIVATION_WINDOW_TOO_NARROW",
        "DRILL_COVERAGE_FORCING_MISSING",
        "DRILL_COVERAGE_RUNS_MISSING",
        "DRILL_COMPLETENESS_SNAPSHOT_UNBOUND",
        "DRILL_COVERAGE_DB_EXPORT_MISSING",
    }
)


def test_wire_codes_match_fixture_exactly() -> None:
    """H6: WIRE_CODES frozenset content is byte-identical with the fixture."""
    assert retention.WIRE_CODES == _EXPECTED_WIRE_CODES
    assert len(retention.WIRE_CODES) == 4


def test_wire_codes_contain_no_archive_family_member() -> None:
    """#1370: the archive lane is retired, so no gate code can survive.

    Both the by-name check (the thirteen retired codes) and the by-prefix
    check (any future re-introduction under the same namespaces) must hold.
    """
    assert retention.WIRE_CODES.isdisjoint(_RETIRED_ARCHIVE_FAMILY_CODES)
    assert not [
        code
        for code in retention.WIRE_CODES
        if code.startswith("COMPLETENESS_") or code.startswith("DRILL_")
    ]
    assert not [
        name
        for name in dir(retention)
        if name.startswith("CODE_COMPLETENESS_") or name.startswith("CODE_DRILL_")
    ]


def test_wire_codes_byte_identical_across_code_runbook_design() -> None:
    """H6 cross-file: every WIRE_CODES member appears in runbook §8.2 + design #855."""
    runbook_text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    design_text = _DESIGN_PATH.read_text(encoding="utf-8")
    for code in retention.WIRE_CODES:
        assert code in runbook_text, f"{code!r} missing from runbook §8.2"
        assert code in design_text, f"{code!r} missing from design.md #855 block"


# Same-class:byte-identity-drift closure (C1-fix from #855 R1/R2, mirrors
# the discipline extension from #854 R2 lock path). The forward walk asserts
# every WIRE_CODES member is documented; the reverse walk asserts every
# retention-namespaced ALL_CAPS token in the runbook §8.2 / design.md #855
# block corresponds to an actual WIRE_CODES member — no orphan codes drift
# into docs without matching source.
#
# Allowlist tokens legitimately appearing in prose but NOT wire codes.
_WIRE_CODE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # #854 archive rebuild drill wire code — referenced only for
        # symmetry callouts, not a retention wire code.
        "DRILL_UNCAUGHT_ERROR",
        # #854 archive rebuild drill wire code — referenced in §7.6.
        "DRILL_CONCURRENT_INVOCATION",
        # The frozenset symbol name itself, mentioned in prose.
        "WIRE_CODES",
    }
    # #1370: the thirteen archive-family codes were retired together with the
    # `enabled` archive-gate mode (ADR 0002 Revision 2026-08-11). They are no
    # longer `WIRE_CODES` members, but the reverse walk still scans the FROZEN
    # #855 pending design fixture, which spells all thirteen verbatim and must
    # not be edited. Allowlisting them keeps the walk mechanism intact instead
    # of narrowing its corpus.
    | _RETIRED_ARCHIVE_FAMILY_CODES
)


def _extract_wire_code_candidates(text: str) -> set[str]:
    """Return ALL_CAPS tokens that look like retention/completeness/drill wire codes.

    Pattern: RETENTION_*, COMPLETENESS_*, DRILL_* — ALL_CAPS with underscore,
    length >= 2 segments (e.g., ``RETENTION_DROP_FAILED``). Uppercase words
    like ``PASS``/``FAIL`` and single tokens like ``RETENTION`` are
    deliberately excluded (they are prose, not wire codes).
    """
    pattern = re.compile(r"\b(?:RETENTION|COMPLETENESS|DRILL)(?:_[A-Z][A-Z0-9_]*)+\b")
    return set(pattern.findall(text))


def test_wire_codes_documented_tokens_all_reference_wire_codes_frozenset() -> None:
    """H6 reverse walk (same-class fix from #854 R2 byte-identity drift):
    every ALL_CAPS token matching RETENTION_* / COMPLETENESS_* / DRILL_*
    in runbook §8.2 + design.md #855 block MUST be a WIRE_CODES member
    (or explicitly allowlisted). Prevents docs from silently gaining an
    orphan code that has no source-of-truth in ``WIRE_CODES``.
    """
    runbook_text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    design_text = _DESIGN_PATH.read_text(encoding="utf-8")
    documented_tokens = (
        _extract_wire_code_candidates(runbook_text)
        | _extract_wire_code_candidates(design_text)
    )
    orphans = documented_tokens - retention.WIRE_CODES - _WIRE_CODE_ALLOWLIST
    assert not orphans, (
        f"Documented wire-code tokens missing from WIRE_CODES frozenset: {sorted(orphans)}"
    )


# ---------------------------------------------------------------------------
# H10 lock-path byte-identity + zero-arg signature parity
# ---------------------------------------------------------------------------


def test_default_lock_path_matches_runbook_string() -> None:
    """H10: _default_lock_path() returns the exact fixture string."""
    assert str(retention._default_lock_path()) == "/tmp/nhms-node27-timeseries-retention.lock"


def test_default_lock_path_matches_env_example() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "/tmp/nhms-node27-timeseries-retention.lock" in text


def test_default_lock_path_matches_runbook_body() -> None:
    text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "/tmp/nhms-node27-timeseries-retention.lock" in text


def test_default_lock_path_is_zero_arg() -> None:
    """H10 same-class recurrence from #854 R2: signature MUST be parameter-free."""
    sig = inspect.signature(retention._default_lock_path)
    assert sig.parameters == {}


# ---------------------------------------------------------------------------
# H12 statement_timeout constants pin (CC-F4 opportunistic)
# ---------------------------------------------------------------------------


def test_statement_timeout_constants_match_h12_pin() -> None:
    """H12 pin: catalog enumeration 60 000 ms, drop_chunks 300 000 ms.

    A silent constant change would drift from the design.md fixture pin
    and the runbook §8 wording without any surface test noticing. Lock
    the two integers here.
    """
    assert retention._QUERY_TIMEOUT_MS == 60_000
    assert retention._DROP_TIMEOUT_MS == 300_000


# ---------------------------------------------------------------------------
# TARGET_HYPERTABLES contains only D3 hypertables (spec §6.1 test row 4)
# ---------------------------------------------------------------------------


def test_target_hypertables_are_exactly_d3() -> None:
    assert retention.TARGET_HYPERTABLES == frozenset(
        {("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")}
    )


def test_target_hypertables_do_not_include_metadata_tables() -> None:
    """§6.1 test row 4: metadata / coverage tables MUST NOT be retention targets."""
    metadata_tables = {
        ("hydro", "hydro_run"),
        ("hydro", "run_display_coverage"),
        ("met", "forcing_version"),
        ("hydro", "state_snapshot"),
        ("met", "state_snapshot"),
        ("core", "run_display_coverage"),
    }
    assert retention.TARGET_HYPERTABLES.isdisjoint(metadata_tables)


def test_chunk_query_targets_only_d3_hypertables() -> None:
    query = retention._CHUNK_QUERY
    assert "hydro.river_timeseries" not in query  # only as tuple filter with quotes
    assert "'hydro', 'river_timeseries'" in query
    assert "'met', 'forcing_station_timeseries'" in query
    assert "hydro_run" not in query
    assert "forcing_version" not in query


# ---------------------------------------------------------------------------
# H7 boundary predicate: range_end <= cutoff (non-strict)
# ---------------------------------------------------------------------------


def test_chunk_query_uses_non_strict_boundary_predicate() -> None:
    """H7: predicate uses range_end <= cutoff (differs from #851 compression's strict <)."""
    query = retention._CHUNK_QUERY
    assert "range_end <= %s" in query
    assert "range_end < %s" not in query
    # Divergence documented in source comment.
    source = Path(retention.__file__).read_text(encoding="utf-8")
    assert "H7" in source


def test_chunk_query_does_not_filter_compressed_chunks() -> None:
    """H3 divergence from compression: retention MUST target compressed chunks too."""
    query = retention._CHUNK_QUERY
    # is_compressed appears only as a SELECT column (line 3-ish); never in
    # the WHERE clause. Split on WHERE and assert absence in the filter tail.
    _, where_tail = query.split("WHERE", 1)
    assert "is_compressed" not in where_tail
    # And compression's exact filter literal MUST NOT appear anywhere.
    assert "is_compressed = false" not in query
    assert "is_compressed = true" not in query


# ---------------------------------------------------------------------------
# Config parse — happy path + fail-closed
# ---------------------------------------------------------------------------


def _base_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """A deployable env: the archive gate is `disabled`, which since #1370 is
    a REQUIRED explicit assignment, not a default.
    """
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE": "disabled",
        "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_RETENTION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def test_config_parse_happy_path(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = retention.config_from_args(_args(), env)
    assert config.window_days == 14
    assert config.per_tick_bound == 5
    assert config.archive_gate == "disabled"
    assert config.enforce is False
    assert str(config.lock_path) == str(tmp_path / "runner.lock")


def test_config_defaults_lock_path_to_canonical(tmp_path: Path) -> None:
    env = _base_env(tmp_path, NODE27_TIMESERIES_RETENTION_LOCK_PATH=None)
    config = retention.config_from_args(_args(), env)
    assert str(config.lock_path) == "/tmp/nhms-node27-timeseries-retention.lock"


def test_window_default_is_drift_locked_to_the_shared_constant() -> None:
    """#1227 row (h): the archive-side guard resolves a missing/empty window
    assignment to the SAME default this runner uses — pinned here as VALUE
    equality with the shared constant in `packages.common.storage`.

    Honest limit (#1229 round-1 review B4): `is` cannot prove import identity
    for 14, which CPython interns as a small int, so the identity assertion
    below degenerates to value equality and a re-hardcoded local copy would
    still pass. The real protection is directional: changing the shared
    constant while this runner keeps a hardcoded literal turns this test red.
    """
    assert retention._DEFAULT_WINDOW_DAYS is DEFAULT_RETENTION_WINDOW_DAYS
    assert DEFAULT_RETENTION_WINDOW_DAYS == 14


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": None}, id="missing-assignment"),
        pytest.param({"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": ""}, id="empty-value"),
    ],
)
def test_window_resolution_for_missing_or_empty_env_is_unchanged(
    tmp_path: Path, override: dict[str, str | None]
) -> None:
    """#1227 row (j): moving the default constant's home changed no runner behavior."""
    config = retention.config_from_args(_args(), _base_env(tmp_path, **override))

    assert config.window_days == 14


def test_config_enforce_env_toggles(tmp_path: Path) -> None:
    env = _base_env(tmp_path, NODE27_TIMESERIES_RETENTION_ENFORCE="1")
    config = retention.config_from_args(_args(), env)
    assert config.enforce is True


def test_config_enforce_env_falsy_is_dry_run(tmp_path: Path) -> None:
    env = _base_env(tmp_path, NODE27_TIMESERIES_RETENTION_ENFORCE="0")
    config = retention.config_from_args(_args(), env)
    assert config.enforce is False


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": "0"}, "WINDOW_DAYS"),
        ({"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": "-1"}, "WINDOW_DAYS"),
        ({"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": "not-an-int"}, "WINDOW_DAYS"),
        ({"NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND": "0"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND": "-3"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE": None}, "ARCHIVE_GATE"),
        ({"NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": None}, "RECEIPT_PATH"),
        ({"NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": "relative/receipt.json"}, "absolute"),
        ({"NODE27_TIMESERIES_RETENTION_LOCK_PATH": "relative.lock"}, "absolute"),
    ],
)
def test_config_parse_fails_closed(
    tmp_path: Path, override: dict[str, str | None], match: str
) -> None:
    env = _base_env(tmp_path, **override)
    with pytest.raises(retention.RetentionConfigError, match=match):
        retention.config_from_args(_args(), env)


# ---------------------------------------------------------------------------
# H3 per-tick bound + deferred_remainder (spec §6.1 row 3)
# ---------------------------------------------------------------------------


def test_per_tick_bound_selects_at_most_bound_and_defers_remainder(
    tmp_path: Path,
) -> None:
    config = _build_config(tmp_path, per_tick_bound=3, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", f"chk-{i:02d}", delta_days=60 - i)
        for i in range(6)
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    assert len(receipt["dropped_chunks"]) == 3
    assert receipt["deferred_remainder"] == [
        f"_timescaledb_internal.chk-{i:02d}" for i in range(3, 6)
    ]
    # Dropped names are the first 3 in enumeration order.
    dropped_names = [c["name"] for c in receipt["dropped_chunks"]]
    assert dropped_names == [f"_timescaledb_internal.chk-{i:02d}" for i in range(3)]
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# H4 freed_bytes measured BEFORE drop — mock ordering assertion
# ---------------------------------------------------------------------------


def test_freed_bytes_measured_before_drop(tmp_path: Path) -> None:
    """H4: measure call for chunk X precedes drop call for chunk X (per-chunk)."""
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-a", delta_days=60),
        _chunk("met", "forcing_station_timeseries", "chk-b", delta_days=61),
    ]
    stub = _StubRunner(chunks, measured={"_timescaledb_internal.chk-a": 111, "_timescaledb_internal.chk-b": 222})
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    # First call is fetch; second is measure (batch); then drops in order.
    kinds = [c[0] for c in stub.calls]
    assert kinds[0] == "fetch"
    assert kinds[1] == "measure"
    assert kinds[2:] == ["drop", "drop"]
    # measure call carried both chunk names before any drop call fired.
    measure_names = stub.calls[1][1]
    assert measure_names == ("_timescaledb_internal.chk-a", "_timescaledb_internal.chk-b")
    freed = {item["name"]: item["freed_bytes"] for item in receipt["dropped_chunks"]}
    assert freed == {
        "_timescaledb_internal.chk-a": 111,
        "_timescaledb_internal.chk-b": 222,
    }


# ---------------------------------------------------------------------------
# H5 per-chunk drop failure → whole-tick refused
# ---------------------------------------------------------------------------


def test_per_chunk_drop_failure_refuses_whole_tick(tmp_path: Path) -> None:
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-a", delta_days=60),
        _chunk("hydro", "river_timeseries", "chk-b", delta_days=61),
        _chunk("hydro", "river_timeseries", "chk-c", delta_days=62),
    ]
    stub = _StubRunner(
        chunks,
        drop_error={"chk-b": RuntimeError("simulated timeout")},
    )
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"].startswith("RETENTION_DROP_FAILED:hydro.chk-b")
    # Post-failure chunks NOT attempted.
    drop_calls = [c[1] for c in stub.calls if c[0] == "drop"]
    assert "_timescaledb_internal.chk-c" not in drop_calls
    # a was attempted (before b), b was attempted (raised), c was not.
    assert drop_calls == ["_timescaledb_internal.chk-a", "_timescaledb_internal.chk-b"]
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# H7 chunk boundary predicate: range_end == cutoff → dropped
# ---------------------------------------------------------------------------


def test_chunk_at_boundary_is_included_in_eligible(tmp_path: Path) -> None:
    """H7: chunk whose range_end == cutoff has all row times < cutoff → drop-eligible."""
    config = _build_config(tmp_path, enforce=True)
    # boundary chunk: range_end == cutoff exactly
    boundary = _chunk("hydro", "river_timeseries", "chk-boundary", delta_days=_DROP_WINDOW_DAYS)
    stub = _StubRunner([boundary])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    assert receipt["dropped_chunks"][0]["name"] == "_timescaledb_internal.chk-boundary"


def test_default_fetch_filter_at_boundary_predicate() -> None:
    """H7 SQL sanity: WHERE clause is range_end <= %s (non-strict)."""
    assert "range_end <= %s" in retention._CHUNK_QUERY


# ---------------------------------------------------------------------------
# H17 zero-eligible enforce
# ---------------------------------------------------------------------------


def test_zero_eligible_enforce_produces_empty_enforced_receipt(tmp_path: Path) -> None:
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    assert receipt["mode"] == "enforce"
    assert receipt["dropped_chunks"] == []
    assert receipt["deferred_remainder"] == []
    assert receipt["salvage_backed_windows"] == []
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Dry-run receipt shape (schema oneOf conformance)
# ---------------------------------------------------------------------------


def test_dry_run_receipt_lists_candidates_and_defers(tmp_path: Path) -> None:
    config = _build_config(tmp_path, per_tick_bound=2, enforce=False)
    chunks = [
        _chunk("hydro", "river_timeseries", f"chk-{i}", delta_days=60 - i) for i in range(4)
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["mode"] == "dry-run"
    assert receipt["outcome"] == "dry-run"
    assert receipt["candidate_chunks"] == [
        "_timescaledb_internal.chk-0",
        "_timescaledb_internal.chk-1",
    ]
    assert receipt["deferred_remainder"] == [
        "_timescaledb_internal.chk-2",
        "_timescaledb_internal.chk-3",
    ]
    # Dry-run never calls drop.
    assert not any(c[0] == "drop" for c in stub.calls)
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Concurrent invocation
# ---------------------------------------------------------------------------


def test_concurrent_invocation_publishes_refused_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    lock_path = Path(env["NODE27_TIMESERIES_RETENTION_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])
    try:
        code = retention.main(argv=[], now=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_RETENTION_CONCURRENT_INVOCATION
    jsonschema.validate(receipt, _load_schema())
    err = capsys.readouterr().err
    assert retention.CODE_RETENTION_CONCURRENT_INVOCATION in err


# ---------------------------------------------------------------------------
# Uncaught error path
# ---------------------------------------------------------------------------


def test_uncaught_error_publishes_refused_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def _bang_fetch(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        raise RuntimeError("catalog probe blew up")

    code = retention.main(
        argv=[],
        now=_NOW,
        fetch_chunks=_bang_fetch,
    )
    assert code == 1
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"].startswith("RETENTION_UNCAUGHT_ERROR:RuntimeError")
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# #1213 — DSN credentials never reach stderr OR the receipt file
#
# Both persisted operator surfaces are asserted: the stderr diagnostic (the
# wrapper redirects it into a long-lived `retention.log`) and the receipt file
# BYTES (`refusal_reason` is persisted verbatim). The injected exceptions are
# the two real driver shapes that echo credentials back:
#
# * psycopg2 `ProgrammingError('invalid dsn: ... "<full conninfo>" ...')` —
#   echoes the whole DSN including the plaintext password;
# * libpq `password authentication failed for user "<role>"` — echoes the role.
#
# The pre-#1213 lock injected a DSN-free `RuntimeError("oops")`, so its
# assertions were vacuously true against exactly the exception classes that
# can leak.
# ---------------------------------------------------------------------------


def _assert_surfaces_credential_free(*surfaces: str) -> None:
    """Every operator-facing surface is free of the probe DSN's credentials.

    ``alice`` is asserted as a bare substring (not just ``user "alice"``): the
    conninfo echo carries the username outside the libpq role-echo shape, so a
    redaction that only handled the role echo would still leak it.
    """
    for surface in surfaces:
        assert "supersekret" not in surface
        assert "alice" not in surface
        # Operator-facing text renders the marker as ``***``.
        assert REDACTION_MARKER not in surface


def test_dsn_never_appears_in_stderr_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Uncaught path, DSN-parse shape: the full conninfo echo is scrubbed.

    ``_MEASURE_PROBE_DSN`` is the shared probe secret (defined with the
    measurement-diagnostic rows further down) so both redaction call sites
    assert against the same password/role pair.
    """
    env = _base_env(tmp_path, DATABASE_URL=_MEASURE_PROBE_DSN)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def _bang(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        raise psycopg2.ProgrammingError(
            f'invalid dsn: missing "=" after "{_MEASURE_PROBE_DSN}" in connection info string'
        )

    code = retention.main(argv=[], now=_NOW, fetch_chunks=_bang)

    assert code == 1
    err = capsys.readouterr().err
    receipt_bytes = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_bytes()
    _assert_surfaces_credential_free(err, receipt_bytes.decode("utf-8"))

    receipt = json.loads(receipt_bytes.decode("utf-8"))
    assert receipt["outcome"] == "refused"
    # Operator grep contract: wire code + exception type name survive redaction.
    assert receipt["refusal_reason"].startswith("RETENTION_UNCAUGHT_ERROR:ProgrammingError:")
    assert "invalid dsn" in receipt["refusal_reason"]
    assert "***" in receipt["refusal_reason"]
    # The wrapper-log surface carries the same redacted reason, not a second
    # (unredacted) rendering of the same error.
    diagnostic = json.loads(err.strip().splitlines()[-1])
    assert diagnostic["refusal_reason"] == receipt["refusal_reason"]
    jsonschema.validate(receipt, _load_schema())


@pytest.mark.parametrize(
    ("libpq_tail", "expected_echo", "retained_phrase"),
    [
        (
            'FATAL:  password authentication failed for user "alice"',
            'user "***"',
            "password authentication failed",
        ),
        (
            'FATAL:  role "alice" does not exist',
            'role "***"',
            "does not exist",
        ),
    ],
    ids=["password-auth-failed", "missing-role"],
)
def test_uncaught_libpq_auth_failure_redacts_role_on_both_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    libpq_tail: str,
    expected_echo: str,
    retained_phrase: str,
) -> None:
    """Uncaught path, libpq role-echo shapes: both keywords are scrubbed.

    libpq echoes the DSN role back in TWO shapes — ``... for user "alice"``
    (auth failure) and ``role "alice" does not exist`` (role dropped/renamed
    mid-run). Scrubbing only the first leaves the second bleeding the DSN
    username into the receipt and the 0644 ``retention.log``.

    Diagnosability trade-off pinned here: the failure phrase and the host/port
    echo are deliberately RETAINED so the operator can still tell an auth
    failure from a timeout.
    """
    env = _base_env(tmp_path, DATABASE_URL=_MEASURE_PROBE_DSN)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def _bang(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        raise psycopg2.OperationalError(
            f'connection to server at "127.0.0.1", port 55432 failed: {libpq_tail}'
        )

    code = retention.main(argv=[], now=_NOW, fetch_chunks=_bang)

    assert code == 1
    err = capsys.readouterr().err
    receipt_bytes = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_bytes()
    _assert_surfaces_credential_free(err, receipt_bytes.decode("utf-8"))

    receipt = json.loads(receipt_bytes.decode("utf-8"))
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"].startswith("RETENTION_UNCAUGHT_ERROR:OperationalError:")
    assert expected_echo in receipt["refusal_reason"]
    assert retained_phrase in receipt["refusal_reason"]
    # Host/port echo retained by design (non-goal in the #1213 fixture).
    assert 'connection to server at "127.0.0.1", port 55432' in receipt["refusal_reason"]
    jsonschema.validate(receipt, _load_schema())


def test_drop_failure_reason_redacts_credentials_on_both_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drop path: the RETENTION_DROP_FAILED reason is credential-redacted.

    Seam is ``main()`` rather than ``run_retention()`` because only ``main()``
    publishes the receipt FILE — the leak surface under test. Enforce mode is
    reached through the real env toggle so the gate evidence is genuinely
    consumed.

    H5 whole-tick fail-closed is re-asserted on this path (drop attempted
    exactly once, second chunk untouched) so a redaction refactor cannot
    quietly turn the fail-closed return into a continue.
    """
    env = _base_env(
        tmp_path,
        DATABASE_URL=_MEASURE_PROBE_DSN,
        NODE27_TIMESERIES_RETENTION_ENFORCE="1",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    chunks = [
        _chunk("hydro", "river_timeseries", "chk-a", delta_days=60),
        _chunk("hydro", "river_timeseries", "chk-b", delta_days=61),
    ]
    drop_calls: list[str] = []

    def _fetch(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        return list(chunks)

    def _measure(
        config: retention.RetentionConfig, selected: Sequence[retention.ChunkRow]
    ) -> dict[str, int]:
        return {chunk.qualified_name: 0 for chunk in selected}

    def _drop(config: retention.RetentionConfig, chunk: retention.ChunkRow) -> None:
        drop_calls.append(chunk.qualified_name)
        raise psycopg2.OperationalError(
            'connection to server at "127.0.0.1", port 55432 failed: '
            'FATAL:  password authentication failed for user "alice" '
            f"(tried {_MEASURE_PROBE_DSN})"
        )

    code = retention.main(
        argv=[],
        now=_NOW,
        fetch_chunks=_fetch,
        measure_chunk_bytes=_measure,
        drop_chunk=_drop,
    )

    assert code == 1
    err = capsys.readouterr().err
    receipt_bytes = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_bytes()
    _assert_surfaces_credential_free(err, receipt_bytes.decode("utf-8"))

    receipt = json.loads(receipt_bytes.decode("utf-8"))
    assert receipt["outcome"] == "refused"
    # Prefix shape unchanged: <hypertable_schema>.<chunk_name> (NOT
    # ChunkRow.qualified_name, which is chunk_schema-qualified).
    assert receipt["refusal_reason"].startswith("RETENTION_DROP_FAILED:hydro.chk-a: ")
    assert 'user "***"' in receipt["refusal_reason"]
    assert "***" in receipt["refusal_reason"]
    # H5: the failing chunk is the only drop attempted.
    assert drop_calls == ["_timescaledb_internal.chk-a"]
    jsonschema.validate(receipt, _load_schema())


def test_redaction_failure_still_publishes_refused_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chokepoint is TOTAL: an unimportable redaction dep cannot eat the receipt.

    Driver-less window (venv rebuild, broken wheel): psycopg2 is unimportable,
    so the chokepoint's function-local ``packages.common.redaction`` import —
    which pulls ``psycopg2.extensions`` at module scope — raises INSIDE
    ``main()``'s except handler. Without totality that ``ModuleNotFoundError``
    escapes ``main()``: no refused receipt is published at all and a raw
    traceback (carrying whatever the driver echoed) lands in ``retention.log``.

    Fail-closed on both axes: the unredactable text is withheld ENTIRELY (no
    credential can survive), while the wire code, the original exception type
    name and the published receipt all survive.
    """
    env = _base_env(tmp_path, DATABASE_URL=_MEASURE_PROBE_DSN)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # Force a real re-import of the redaction module (the test module caches
    # it at import time) and make every psycopg2 entry unimportable — the
    # already-cached ``psycopg2.extensions`` submodule would otherwise satisfy
    # the redaction module's import straight out of ``sys.modules``.
    monkeypatch.delitem(sys.modules, "packages.common.redaction", raising=False)
    for name in [n for n in sys.modules if n == "psycopg2" or n.startswith("psycopg2.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)

    def _bang(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        raise RuntimeError(
            f'connect failed (tried {_MEASURE_PROBE_DSN}): FATAL:  role "alice" does not exist'
        )

    code = retention.main(argv=[], now=_NOW, fetch_chunks=_bang)

    assert code == 1
    err = capsys.readouterr().err
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])
    assert receipt_path.exists(), "refused receipt MUST survive a redaction failure"
    receipt_bytes = receipt_path.read_bytes()
    _assert_surfaces_credential_free(err, receipt_bytes.decode("utf-8"))

    receipt = json.loads(receipt_bytes.decode("utf-8"))
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == (
        "RETENTION_UNCAUGHT_ERROR:RuntimeError: "
        "<error text withheld: redaction unavailable (RuntimeError)>"
    )
    jsonschema.validate(receipt, _load_schema())


# A driver-less interpreter: psycopg2 (and every submodule) is hard-blocked at
# the meta-path level, then the runner is imported and its argument parser is
# exercised. Run as a subprocess so the block is total — an in-process check
# cannot un-import what pytest already loaded.
_DRIVERLESS_IMPORT_PROBE = '''
import sys


class _BlockPsycopg2:
    def find_spec(self, name, path=None, target=None):
        if name == "psycopg2" or name.startswith("psycopg2."):
            raise ImportError("psycopg2 blocked by the driver-less import probe")
        return None


sys.meta_path.insert(0, _BlockPsycopg2())

# The blocker must actually bite, else the probe is vacuous.
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("probe vacuous: psycopg2 imported despite the blocker")

from scripts import node27_timeseries_retention as runner

try:
    runner._parser().parse_args(["--help"])
except SystemExit as exc:
    if exc.code not in (0, None):
        raise SystemExit(f"--help failed with exit code {exc.code!r}")
print("DRIVERLESS_IMPORT_OK")
'''


def test_redaction_and_psycopg2_imports_stay_deferred() -> None:
    """The runner must import cleanly without psycopg2 installed.

    ``packages.common.redaction`` imports ``psycopg2.extensions`` at module
    scope, so routing two more call sites through the redaction helper is
    exactly the kind of change that tempts a module-scope import and breaks
    ``--help`` / config parsing on a driver-less host.

    Two independent guards:

    1. Static: the module's own top level carries no psycopg2 / redaction
       import. Both the ``from packages.common.redaction import ...`` and the
       ``from packages.common import redaction`` spellings are recorded —
       matching only ``ImportFrom.module`` would register ``packages.common``
       for the second spelling and wave the breaking mutant through.
    2. Behavioral: a subprocess with psycopg2 hard-blocked imports the module
       and runs ``--help``. The static check cannot see an import that creeps
       in through some other module in the import graph; this one can.
    """
    tree = ast.parse(Path(retention.__file__).read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.add(node.module)
            top_level.update(f"{node.module}.{alias.name}" for alias in node.names)

    assert not [name for name in top_level if name == "psycopg2" or name.startswith("psycopg2.")]
    assert "packages.common.redaction" not in top_level

    helper_source = inspect.getsource(retention._redact_error_text)
    assert "from packages.common.redaction import" in helper_source

    probe = subprocess.run(
        [sys.executable, "-c", _DRIVERLESS_IMPORT_PROBE],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "DRIVERLESS_IMPORT_OK" in probe.stdout


# ---------------------------------------------------------------------------
# C2-fix — RETENTION_CONFIG_INVALID stderr emit sites
# ---------------------------------------------------------------------------


def test_main_emits_config_invalid_wire_code_on_missing_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C2-fix (byte-identity discipline extension, same-class:#854 R2):
    a config parse failure (missing ``DATABASE_URL``) MUST emit stderr
    JSON carrying the byte-identical ``RETENTION_CONFIG_INVALID`` wire
    code so operators grep-match against the WIRE_CODES source of truth.
    """
    env = _base_env(tmp_path, DATABASE_URL=None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = retention.main(argv=[], now=_NOW)
    assert code == 2
    err_text = capsys.readouterr().err
    payload = json.loads(err_text.strip().splitlines()[-1])
    # Byte-identical wire code — literal string comparison against WIRE_CODES.
    assert payload["code"] == retention.CODE_RETENTION_CONFIG_INVALID
    assert payload["code"] == "RETENTION_CONFIG_INVALID"
    assert payload["code"] in retention.WIRE_CODES


def test_main_emits_config_invalid_wire_code_on_non_absolute_receipt_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C2-fix: non-absolute ``NODE27_TIMESERIES_RETENTION_RECEIPT_PATH`` env
    triggers ``RETENTION_CONFIG_INVALID`` before any DB call.
    """
    env = _base_env(
        tmp_path, NODE27_TIMESERIES_RETENTION_RECEIPT_PATH="relative/receipt.json"
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    code = retention.main(argv=[], now=_NOW)
    assert code == 2
    err_text = capsys.readouterr().err
    payload = json.loads(err_text.strip().splitlines()[-1])
    assert payload["code"] == retention.CODE_RETENTION_CONFIG_INVALID
    assert payload["code"] == "RETENTION_CONFIG_INVALID"
    assert payload["code"] in retention.WIRE_CODES


# ---------------------------------------------------------------------------
# B1 — measure isolation: one chunk's abort MUST NOT poison neighbours
# ---------------------------------------------------------------------------


class _DropProbe:
    """Recorder for the fake psycopg2 driving the real ``_default_drop_chunk``.

    ``closed`` counts ``connection.close()`` calls: ``_default_drop_chunk``
    closes in a ``finally``, so the count must be 1 on the raising rows too.
    Its kill surface is deletion of ``finally: connection.close()`` — the
    connection would leak on the one path that runs after an irreversible
    ``DROP CHUNK`` attempt. (It says nothing about *where* the guard lives:
    a guard hoisted out of the ``try`` still leaves ``closed == 1``.)

    ``conn_exit_exc`` records what the ``with connection:`` transaction
    context observed on exit, one entry per exit. ``RuntimeError`` means the
    H3 guard fired INSIDE the transaction, so psycopg2 rolls the unexpected
    drop back; ``None`` means the block completed and psycopg2 commits.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.closed = 0
        self.conn_exit_exc: list[type[BaseException] | None] = []

    @property
    def drop_statement(self) -> tuple[str, tuple | None]:
        return next((sql, params) for sql, params in self.executed if "drop_chunks" in sql)


def _install_fake_drop_psycopg2(
    monkeypatch: pytest.MonkeyPatch, dropped_rows: Sequence[tuple[str]]
) -> _DropProbe:
    """Inject a fake ``psycopg2`` driving ``_default_drop_chunk`` (no DB).

    ``dropped_rows`` is what server-side ``drop_chunks`` reports back — the
    exact input the H3 identity guard judges.
    """
    probe = _DropProbe()

    class _FakeCursor:
        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def execute(self, sql: str, params: tuple | None = None) -> None:
            probe.executed.append((sql, params))

        def fetchall(self) -> list[tuple[str]]:
            return list(dropped_rows)

    class _FakeConn:
        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            # Real psycopg2 commits on a clean exit and ROLLS BACK when the
            # block raises; record which one the transaction context saw.
            probe.conn_exit_exc.append(exc_type)
            return None

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def close(self) -> None:
            probe.closed += 1

    import types

    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        types.SimpleNamespace(connect=lambda _url: _FakeConn()),
    )
    return probe


def test_default_drop_chunk_bounds_exact_physical_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQL lower bound prevents a later selected chunk from cascading
    through an older boundary-partial chunk that was deliberately deferred.
    """
    probe = _install_fake_drop_psycopg2(monkeypatch, [("_timescaledb_internal.chk-covered",)])
    config = _build_config(tmp_path, enforce=True)
    chunk = _chunk(
        "hydro", "river_timeseries", "chk-covered", delta_days=53, duration_days=7
    )

    retention._default_drop_chunk(config, chunk)

    drop_sql, params = probe.drop_statement
    assert "newer_than" in drop_sql
    assert params == (
        chunk.range_end,
        chunk.range_start,
        "hydro.river_timeseries",
    )


# ---------------------------------------------------------------------------
# #1214 — H3 identity guard: negative oracle
#
# `_default_drop_chunk`'s `dropped_names != [chunk.qualified_name]` check is
# the SOLE runtime enforcement of H3 BLOCKING ("bind returned identity AND
# cardinality"), and runbook §8.6 item 5's trichotomy depends on it. Before
# these rows the `raise` was never executed by any test — deleting the whole
# guard left the suite green on an irreversible DROP CHUNK path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dropped_rows", "expected_returned_repr"),
    [
        ((), "[]"),
        ((("_timescaledb_internal.chk-other",),), "['_timescaledb_internal.chk-other']"),
        (
            (
                ("_timescaledb_internal.chk-covered",),
                ("_timescaledb_internal.chk-other",),
            ),
            "['_timescaledb_internal.chk-covered', '_timescaledb_internal.chk-other']",
        ),
    ],
    ids=[
        "chunk-vanished-mid-tick",
        "server-dropped-a-different-chunk",
        "server-dropped-an-extra-chunk",
    ],
)
def test_default_drop_chunk_rejects_unexpected_drop_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dropped_rows: Sequence[tuple[str]],
    expected_returned_repr: str,
) -> None:
    """H3: any ``drop_chunks`` result other than the selected chunk fails closed.

    Three directions, all reachable in production:

    * zero rows — the chunk vanished between enumeration and drop (a
      concurrent manual drop / retention policy), so this tick's evidence no
      longer describes what the server did;
    * a DIFFERENT chunk name — a surprising server-side range decision
      succeeded on something the gate never evidenced;
    * the selected chunk PLUS an extra — a widened server-side range took
      collateral chunks with it. A superset containing the selected chunk is
      NOT success: the extra drop is unevidenced and irreversible, so
      cardinality binds as tightly as identity. Membership-only
      (``qualified_name not in dropped_names``) or first-row-only
      (``dropped_names[0] != qualified_name``) weakenings of the guard pass
      the other two rows and must fail here.

    Cardinality alone cannot separate the second case from success, and
    identity alone cannot separate the third, which is why the guard binds
    both. The expected message is written out by hand rather than recomputed
    from ``dropped_rows``, so the assertion is an independent oracle rather
    than a mirror of the implementation.
    """
    probe = _install_fake_drop_psycopg2(monkeypatch, dropped_rows)
    config = _build_config(tmp_path, enforce=True)
    chunk = _chunk(
        "hydro", "river_timeseries", "chk-covered", delta_days=53, duration_days=7
    )
    expected_message = (
        f"drop_chunks returned {expected_returned_repr} for "
        f"{chunk.qualified_name}; expected exact selected chunk"
    )

    with pytest.raises(
        RuntimeError,
        match=rf"{re.escape(chunk.qualified_name)}; expected exact selected chunk",
    ) as excinfo:
        retention._default_drop_chunk(config, chunk)

    assert str(excinfo.value) == expected_message
    # The guard fired AFTER the real drop_chunks statement was issued — not
    # from some earlier failure that never reached the identity check.
    drop_sql, params = probe.drop_statement
    assert "drop_chunks" in drop_sql
    assert params == (chunk.range_end, chunk.range_start, "hydro.river_timeseries")
    # The RuntimeError propagated THROUGH `with connection:`, i.e. the guard
    # raises inside the transaction, so psycopg2 rolls the unexpected drop
    # back. A guard hoisted out of the `with` (or out of the `try`) would
    # leave the transaction to commit before failing — same exception, same
    # statement, same close count, but an irreversible drop.
    assert probe.conn_exit_exc == [RuntimeError]
    # `finally: connection.close()` still runs on the raising path.
    assert probe.closed == 1


def test_default_measure_chunk_bytes_isolates_per_chunk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1 (F1 fix): a per-chunk measurement failure must not zero the
    ``freed_bytes`` for surrounding chunks. The prior implementation shared
    one transaction across all chunks; a single failure entered
    ``InFailedSqlTransaction`` state and silently zeroed every subsequent
    chunk. Per-chunk connections (mirrors compression sibling) isolate
    each measurement — chunk index 2's abort no longer poisons 3 or 4.
    """
    _NUM = 5
    fail_index = 2
    realistic = {0: 1_111, 1: 2_222, 3: 4_444, 4: 5_555}
    # Global cross-connection counter — chunks are measured in enumeration
    # order across N fresh connections. The counter tells us which chunk
    # the current cursor.execute() is measuring.
    global_chunk_idx = [-1]

    class _FakeCursor:
        def __init__(self, poisoned: list[bool]) -> None:
            self._poisoned = poisoned
            self._last_row: tuple[int, ...] | None = None

        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def execute(self, sql: str, params: tuple | None = None) -> None:
            if self._poisoned[0]:
                # Simulates psycopg2's InFailedSqlTransaction: any query on
                # this connection raises until rollback. The runner must
                # NOT reach this state on a fresh connection.
                raise RuntimeError("current transaction is aborted (InFailedSqlTransaction)")
            if "statement_timeout" in sql:
                return
            if "chunks_detailed_size" in sql:
                global_chunk_idx[0] += 1
                idx = global_chunk_idx[0]
                if idx == fail_index:
                    # Simulate a per-chunk failure (relation missing);
                    # poison this connection so subsequent execute() raises.
                    self._poisoned[0] = True
                    raise RuntimeError("relation does not exist")
                self._last_row = (realistic[idx],)

        def fetchone(self) -> tuple | None:
            return self._last_row

    class _FakeConn:
        def __init__(self) -> None:
            self._poisoned = [False]
            self._cursor = _FakeCursor(self._poisoned)

        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def cursor(self) -> _FakeCursor:
            return self._cursor

        def close(self) -> None:
            return None

    connect_calls: list[str] = []

    def _fake_connect(url: str) -> _FakeConn:
        connect_calls.append(url)
        return _FakeConn()

    # Inject the fake psycopg2 module lookup used by
    # ``_default_measure_chunk_bytes`` — the function does ``import psycopg2``
    # inside its body, so we monkeypatch ``sys.modules`` entry.
    import types

    fake_module = types.SimpleNamespace(connect=_fake_connect)
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake_module)  # type: ignore[arg-type]

    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", f"chk-{i}", delta_days=60 + i) for i in range(_NUM)
    ]
    measured = retention._default_measure_chunk_bytes(config, chunks)
    # Each chunk got its own connection — no shared-transaction poisoning.
    assert len(connect_calls) == _NUM
    # Failing chunk records 0; neighbours preserve realistic bytes.
    assert measured[chunks[0].qualified_name] == 1_111
    assert measured[chunks[1].qualified_name] == 2_222
    assert measured[chunks[2].qualified_name] == 0  # failed chunk
    assert measured[chunks[3].qualified_name] == 4_444  # NOT zeroed
    assert measured[chunks[4].qualified_name] == 5_555  # NOT zeroed


# ---------------------------------------------------------------------------
# #1125 — default measurement path is compression-aware
# ---------------------------------------------------------------------------

# Live node-27 probe (TimescaleDB 2.10.2, 2026-08-01, recorded in
# openspec/changes/fix-retention-freed-bytes-compressed/design.md D1):
# compressed chunk ``_hyper_3_14_chunk`` reads 57,344 B through
# ``pg_total_relation_size`` (the 2026-07-25 under-report signature) but
# 5,904,531,456 B through ``chunks_detailed_size.total_bytes``.
_PROBE_MAIN_RELATION_BYTES = 57_344
_PROBE_TOTAL_BYTES = 5_904_531_456

# Synthetic (no live ``met`` probe is recorded in design D1). The met row
# exists to pin the hypertable regclass parameter and the value round-trip
# for the second retained hypertable, not to assert a live number.
_MET_TOTAL_BYTES = 1_073_741_824

# The exact statement ``_default_measure_chunk_bytes`` must issue. Pinning the
# whole string (not substrings) is what makes a projected-column swap
# (``total_bytes`` -> ``table_bytes``, which would resurrect the under-report
# defect with a still-"chunks_detailed_size"-shaped query) and a predicate
# reorder (which would silently mis-bind the two %s params) go red.
_EXPECTED_MEASURE_SQL = (
    "SELECT total_bytes FROM chunks_detailed_size(%s::regclass) "
    "WHERE chunk_schema = %s AND chunk_name = %s"
)

# The operator-facing half of the same statement, byte-identical with the two
# permanent doc surfaces (receipts README resolution note + design #855 H4).
# Same byte-identity discipline as the wire-code and lock-path rows above.
_DOC_MEASURE_SQL_PREFIX = "SELECT total_bytes FROM chunks_detailed_size("

_RECEIPTS_README_PATH = (
    _ROOT
    / "docs/runbooks/receipts/tier-node27-timeseries-storage"
    / "timeseries-retention/README.md"
)

# One JSON line on stderr per failed per-chunk measurement (design D2). It is
# warning vocabulary, NOT a wire code: it never reaches the receipt and is not
# a member of ``WIRE_CODES``.
_MEASURE_WARNING = "freed_bytes measurement failed; recording 0"

# A URI DSN whose password AND libpq role name both appear verbatim in the
# error text psycopg2 raises on an auth failure. Shared by the two redaction
# rows (query-time failure and connect-time failure) so both assert the
# scrubbing against the same secret.
_MEASURE_PROBE_DSN = "postgresql://alice:supersekret@127.0.0.1:55432/nhms"


class _MeasureProbe:
    """Recorder for the fake psycopg2 driving ``_default_measure_chunk_bytes``.

    ``completions`` gets one entry per connection context-manager exit: True
    when the ``with connection:`` block unwound cleanly (psycopg2 COMMITs),
    False when it unwound with an exception (psycopg2 ROLLBACKs). Without it a
    recorded ``0`` is ambiguous — the D2-mandated coercion path and the
    best-effort except path both write 0, so a mutation that deletes the
    coercion would still "pass" while degrading every NULL/edge measurement
    into a swallowed exception.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.connect_calls: list[str] = []
        self.completions: list[bool] = []

    @property
    def measure_statements(self) -> list[tuple[str, tuple | None]]:
        """Executed statements excluding the ``SET statement_timeout`` prelude."""
        return [(sql, prm) for sql, prm in self.executed if "statement_timeout" not in sql]

    @property
    def timeout_statements(self) -> list[tuple[str, tuple | None]]:
        """The ``SET statement_timeout`` prelude of every connection."""
        return [(sql, prm) for sql, prm in self.executed if "statement_timeout" in sql]


# H12: every measurement connection MUST cap itself at 60 s before issuing the
# now hypertable-wide size walk — deleting the prelude turns a lock-blocked
# walk into an unbounded wait inside the wrapper/systemd wall.
_EXPECTED_TIMEOUT_STATEMENT = (f"SET statement_timeout = {retention._QUERY_TIMEOUT_MS}", None)


def _install_fake_measure_psycopg2(
    monkeypatch: pytest.MonkeyPatch,
    responder: Any,
    *,
    connect_error: BaseException | None = None,
) -> _MeasureProbe:
    """Inject a fake ``psycopg2`` driving ``_default_measure_chunk_bytes``.

    ``responder(params)`` returns the row ``fetchone()`` yields for the
    measurement statement (or raises to simulate a per-chunk failure).

    ``connect_error``, when given, is raised by the FIRST ``psycopg2.connect``
    call only; every later call connects normally. This is the one failure the
    responder cannot reach — it never runs, because there is no cursor — so it
    is the only way to exercise the connect call's placement inside the
    per-chunk ``try``.
    """
    probe = _MeasureProbe()

    class _FakeCursor:
        def __init__(self) -> None:
            self._row: tuple | None = None

        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def execute(self, sql: str, params: tuple | None = None) -> None:
            probe.executed.append((sql, params))
            if "statement_timeout" in sql:
                return
            self._row = responder(params)

        def fetchone(self) -> tuple | None:
            return self._row

    class _FakeConn:
        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            # psycopg2's connection context manager COMMITs on a clean exit and
            # ROLLBACKs on an exception; record which happened.
            probe.completions.append(exc_type is None)
            return None

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def close(self) -> None:
            return None

    def _fake_connect(url: str) -> _FakeConn:
        probe.connect_calls.append(url)
        if connect_error is not None and len(probe.connect_calls) == 1:
            raise connect_error
        return _FakeConn()

    import types

    monkeypatch.setitem(
        __import__("sys").modules, "psycopg2", types.SimpleNamespace(connect=_fake_connect)
    )
    return probe


@pytest.mark.parametrize(
    ("schema", "hypertable", "chunk_label", "total_bytes"),
    [
        ("hydro", "river_timeseries", "_hyper_3_14_chunk", _PROBE_TOTAL_BYTES),
        ("met", "forcing_station_timeseries", "_hyper_5_21_chunk", _MET_TOTAL_BYTES),
    ],
    ids=["hydro-river", "met-forcing-station"],
)
def test_default_measure_chunk_bytes_uses_compression_aware_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schema: str,
    hypertable: str,
    chunk_label: str,
    total_bytes: int,
) -> None:
    """#1125: a compressed chunk's freed_bytes carries the compressed sibling.

    The old ``pg_total_relation_size(chunk)`` query sized only the main chunk
    relation (57,344 B on the live probe). The runner MUST query
    ``chunks_detailed_size(<hypertable>::regclass)`` filtered by
    ``(chunk_schema, chunk_name)`` and record its ``total_bytes``.

    Both retained hypertables are exercised so the regclass parameter cannot be
    hardcoded to ``hydro.river_timeseries``; the SQL is asserted whole so the
    projected column and the predicate order are both pinned.
    """
    probe = _install_fake_measure_psycopg2(monkeypatch, lambda _params: (total_bytes,))
    config = _build_config(tmp_path, enforce=True)
    chunk = _chunk(schema, hypertable, chunk_label, delta_days=60, is_compressed=True)

    measured = retention._default_measure_chunk_bytes(config, [chunk])

    assert measured == {chunk.qualified_name: total_bytes}
    assert measured[chunk.qualified_name] != _PROBE_MAIN_RELATION_BYTES
    assert len(probe.measure_statements) == 1
    measure_sql, params = probe.measure_statements[0]
    assert measure_sql == _EXPECTED_MEASURE_SQL
    assert params == (f"{schema}.{hypertable}", "_timescaledb_internal", chunk_label)
    assert len(probe.connect_calls) == 1
    assert probe.completions == [True]
    assert probe.executed[0] == _EXPECTED_TIMEOUT_STATEMENT
    assert probe.timeout_statements == [_EXPECTED_TIMEOUT_STATEMENT]
    # The clean path is silent — the D2 diagnostic belongs to the failure path
    # only, so a successful measurement must not add stderr noise to the
    # wrapper's `retention.log`.
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("row", [None, (None,)], ids=["no-row", "null-total-bytes"])
def test_default_measure_chunk_bytes_missing_row_records_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    row: tuple | None,
) -> None:
    """#1125 D2: the filtered function may return no row (chunk dropped or
    renamed between enumeration and measurement) or a NULL ``total_bytes``;
    both coerce to 0 rather than raising.

    The 0 must come from the coercion, NOT from the best-effort except branch:
    the measurement statement is asserted executed and the connection block
    asserted to have unwound cleanly, so a lost coercion (``int(None)`` ->
    TypeError -> except -> 0) is red instead of indistinguishable.
    """
    probe = _install_fake_measure_psycopg2(monkeypatch, lambda _params: row)
    config = _build_config(tmp_path, enforce=True)
    chunk = _chunk("hydro", "river_timeseries", "chk-vanished", delta_days=60)

    measured = retention._default_measure_chunk_bytes(config, [chunk])

    assert measured == {chunk.qualified_name: 0}
    assert probe.measure_statements == [
        (
            _EXPECTED_MEASURE_SQL,
            ("hydro.river_timeseries", "_timescaledb_internal", "chk-vanished"),
        )
    ]
    assert len(probe.connect_calls) == 1
    assert probe.completions == [True]
    assert probe.timeout_statements == [_EXPECTED_TIMEOUT_STATEMENT]
    # Second, independent witness that this 0 is the coercion and not the
    # best-effort except branch: the failure path ALWAYS emits the D2
    # diagnostic line, so silence here proves no exception was swallowed.
    assert capsys.readouterr().err == ""


def test_default_measure_chunk_bytes_failure_records_zero_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1125 D2: a per-chunk query failure records 0 for that chunk only; the
    remaining chunks are still measured on their own fresh connections.

    The failure ALSO emits exactly one JSON diagnostic line on stderr naming
    the chunk and the cause — without it the receipt's ``0`` is
    indistinguishable from a genuinely empty chunk. The whole parsed object is
    asserted (all three keys), so dropping the line, dropping the ``chunk``
    key, printing non-JSON, or printing to stdout are each red.
    """
    sizes = {
        "_timescaledb_internal.chk-a": 1_234_567,
        "_timescaledb_internal.chk-c": 7_654_321,
    }

    def _responder(params: tuple | None) -> tuple | None:
        assert params is not None
        name = f"{params[1]}.{params[2]}"
        if name == "_timescaledb_internal.chk-b":
            raise RuntimeError('function chunks_detailed_size(regclass) failed for "chk-b"')
        return (sizes[name],)

    probe = _install_fake_measure_psycopg2(monkeypatch, _responder)
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", label, delta_days=60 + i)
        for i, label in enumerate(("chk-a", "chk-b", "chk-c"))
    ]

    measured = retention._default_measure_chunk_bytes(config, chunks)

    assert measured == {
        "_timescaledb_internal.chk-a": 1_234_567,
        "_timescaledb_internal.chk-b": 0,
        "_timescaledb_internal.chk-c": 7_654_321,
    }
    assert len(probe.connect_calls) == len(chunks)
    # Only the failing chunk's block unwound with an exception — this is the
    # counterpart of the missing-row test: here the 0 legitimately comes from
    # the except branch, and the neighbours still complete cleanly.
    assert probe.completions == [True, False, True]
    assert probe.timeout_statements == [_EXPECTED_TIMEOUT_STATEMENT] * len(chunks)

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [line for line in captured.err.splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "warning": _MEASURE_WARNING,
        "chunk": "_timescaledb_internal.chk-b",
        "error": 'function chunks_detailed_size(regclass) failed for "chk-b"',
    }


def test_default_measure_chunk_bytes_uncoercible_value_records_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1125 D2: the coercion is PART of the per-chunk measurement, so a value
    that cannot be coerced is a per-chunk failure — 0 + diagnostic + continue,
    never a whole-tick abort.

    This pins the coercion's *placement*, which no other row does: only a
    row whose `total_bytes` actually raises in `int()` distinguishes
    "coercion inside the connection block and inside the per-chunk try" from
    a refactor that hoists it out. Hoisted out of the `with connection:`
    block the failing chunk would COMMIT instead of ROLLBACK
    (`completions == [True, ...]`); hoisted out of the `try` the exception
    would escape and take the whole tick down with it.
    """
    sizes: dict[str, Any] = {
        "_timescaledb_internal.chk-bad": "18 GB",  # driver/type surprise
        "_timescaledb_internal.chk-ok": 4_242,
    }

    def _responder(params: tuple | None) -> tuple | None:
        assert params is not None
        return (sizes[f"{params[1]}.{params[2]}"],)

    probe = _install_fake_measure_psycopg2(monkeypatch, _responder)
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", label, delta_days=60 + i)
        for i, label in enumerate(("chk-bad", "chk-ok"))
    ]

    measured = retention._default_measure_chunk_bytes(config, chunks)

    assert measured == {
        "_timescaledb_internal.chk-bad": 0,
        "_timescaledb_internal.chk-ok": 4_242,
    }
    assert probe.completions == [False, True]
    lines = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["warning"] == _MEASURE_WARNING
    assert payload["chunk"] == "_timescaledb_internal.chk-bad"
    assert "invalid literal for int()" in payload["error"]


def test_measure_failure_diagnostic_redacts_dsn_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The D2 diagnostic must not leak the DSN password or the libpq role name.

    A psycopg2 connection failure echoes both back verbatim
    (``FATAL:  password authentication failed for user "alice"`` plus, for
    URI DSNs, the connection string itself). The wrapper redirects stderr into
    a world-readable-ish `retention.log`, so the diagnostic goes through the
    shared redaction policy first. Recording semantics are unchanged: still 0,
    still continue.
    """
    dsn = _MEASURE_PROBE_DSN
    message = (
        'connection to server at "127.0.0.1", port 55432 failed: '
        'FATAL:  password authentication failed for user "alice" '
        f"(tried {dsn})"
    )

    def _responder(_params: tuple | None) -> tuple | None:
        raise RuntimeError(message)

    _install_fake_measure_psycopg2(monkeypatch, _responder)
    config = _build_config(tmp_path, enforce=True, database_url=dsn)
    chunk = _chunk("hydro", "river_timeseries", "chk-auth", delta_days=60)

    measured = retention._default_measure_chunk_bytes(config, [chunk])

    assert measured == {chunk.qualified_name: 0}

    captured = capsys.readouterr()
    assert "supersekret" not in captured.err
    assert "alice" not in captured.err
    assert REDACTION_MARKER not in captured.err  # rendered as *** for operators
    payload = json.loads(captured.err.strip())
    assert set(payload) == {"warning", "chunk", "error"}
    assert payload["warning"] == _MEASURE_WARNING
    assert payload["chunk"] == chunk.qualified_name
    assert "password authentication failed" in payload["error"]


def test_measure_connect_failure_records_zero_redacts_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1125 D2: a chunk whose CONNECTION cannot even be opened records 0 and
    the next chunk is still measured on its own fresh connection.

    ``psycopg2.connect`` sits inside the per-chunk ``try``; every other failure
    row in this file needs a live cursor and therefore cannot see that
    placement. Hoisting the connect above the loop (one shared connection) or
    outside the ``try`` turns this per-chunk fault into a whole-tick abort —
    chunk two would never be measured. The libpq text embeds the DSN role, so
    the redaction is asserted on the same line.
    """
    error = psycopg2.OperationalError(
        'connection to server at "127.0.0.1", port 55432 failed: '
        'FATAL:  password authentication failed for user "alice"'
    )
    probe = _install_fake_measure_psycopg2(
        monkeypatch, lambda _params: (4_242,), connect_error=error
    )
    config = _build_config(tmp_path, enforce=True, database_url=_MEASURE_PROBE_DSN)
    chunks = [
        _chunk("hydro", "river_timeseries", label, delta_days=60 + i)
        for i, label in enumerate(("chk-noconn", "chk-ok"))
    ]

    measured = retention._default_measure_chunk_bytes(config, chunks)

    # Continue semantics: the failed chunk records 0, the neighbour is measured.
    assert measured == {
        "_timescaledb_internal.chk-noconn": 0,
        "_timescaledb_internal.chk-ok": 4_242,
    }
    assert probe.connect_calls == [_MEASURE_PROBE_DSN, _MEASURE_PROBE_DSN]
    # Only the second chunk ever entered a connection block — the first never
    # obtained a connection to commit or roll back.
    assert probe.completions == [True]
    assert probe.timeout_statements == [_EXPECTED_TIMEOUT_STATEMENT]

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [line for line in captured.err.splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == {"warning", "chunk", "error"}
    assert payload["warning"] == _MEASURE_WARNING
    assert payload["chunk"] == "_timescaledb_internal.chk-noconn"
    assert "password authentication failed" in payload["error"]
    # The libpq role name is scrubbed before the wrapper captures stderr.
    assert "alice" not in captured.err


def test_measure_sql_prefix_byte_identical_with_docs() -> None:
    """Byte-identity: the measurement statement's operator-facing prefix is
    pinned in the two permanent doc surfaces.

    Same discipline as the wire-code and lock-path rows: a future edit that
    changes the projected column or the function (e.g. back to
    ``pg_total_relation_size``, or to ``table_bytes``) without touching the
    receipts README resolution note and design #855 H4 goes red here instead
    of silently re-opening the 2026-07-25 under-report with docs that claim
    otherwise.
    """
    assert _EXPECTED_MEASURE_SQL.startswith(_DOC_MEASURE_SQL_PREFIX)
    readme_text = _RECEIPTS_README_PATH.read_text(encoding="utf-8")
    design_text = _DESIGN_PATH.read_text(encoding="utf-8")
    assert _DOC_MEASURE_SQL_PREFIX in readme_text, (
        "receipts README resolution note must name the measurement statement"
    )
    assert _DOC_MEASURE_SQL_PREFIX in design_text, "design #855 H4 must name the statement"


# The token §8.6's disambiguation procedure greps for. It is a PREFIX of the
# full warning literal, so a rename of the tail alone still greps.
_MEASURE_WARNING_GREP_TOKEN = "freed_bytes measurement failed"  # §8.6 operator procedure

# The §8.6 item 5 command as the operator copy-pastes it. Quoting matters: this
# exact string occurs ONLY in §8.6's fenced block, so it pins that section
# specifically — §8.2.1 names the same token in backticks, which does not
# match, and an unquoted substring check would have been satisfied by §8.2.1
# alone (making the §8.6 anchor vacuous).
_MEASURE_WARNING_GREP_FENCE = f"grep '{_MEASURE_WARNING_GREP_TOKEN}'"


def test_measure_warning_byte_identical_with_runbook() -> None:
    """Byte-identity: the D2 stderr warning is anchored to the runbook.

    §8.2.1 documents the literal line and §8.6 item 5 hands the operator a
    `grep` command for its prefix. A rename of the code string that does not
    touch the runbook would silently break that procedure — the operator would
    grep a literal the runner no longer emits and read "no hit" as "the 0 is
    real". The three rows are independently falsifiable: the prefix relation
    (code side), §8.2.1's full literal, and §8.6's executable command.
    """
    assert _MEASURE_WARNING.startswith(_MEASURE_WARNING_GREP_TOKEN)
    runbook_text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    assert _MEASURE_WARNING in runbook_text, (
        "runbook §8.2.1 must carry the full warning literal"
    )
    assert _MEASURE_WARNING_GREP_FENCE in runbook_text, (
        "runbook §8.6 item 5 must carry the operator's grep command verbatim"
    )


# ---------------------------------------------------------------------------
# RF-F1 R2 — loader-side FormatChecker symmetry with emit side
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F1-fix — negative-age freshness guard (defensive against clock skew)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration marker — metadata table row counts unchanged (§6.1 row 4).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_metadata_table_row_counts_unchanged_under_enforce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.1 row 4 belt-and-braces: enforce mode MUST NOT target metadata tables.

    Structural guarantee already holds via TARGET_HYPERTABLES; this test
    additionally asserts that with a real fetch stub returning ONLY D3
    chunk rows, the runner never emits a drop_chunks call for any metadata
    or coverage table name — i.e. every chunk it touches belongs to
    ``TARGET_HYPERTABLES``.
    """
    if os.environ.get("NHMS_RUN_INTEGRATION") != "1":
        pytest.skip("NHMS_RUN_INTEGRATION not set")
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-r", delta_days=60),
        _chunk("met", "forcing_station_timeseries", "chk-f", delta_days=61),
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    for drop_call in [c for c in stub.calls if c[0] == "drop"]:
        # every drop call targets a chunk from the two D3 hypertables only.
        chunk_qualified = drop_call[1]
        assert chunk_qualified.startswith("_timescaledb_internal.")


# ---------------------------------------------------------------------------
# CLI + wrapper contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("env-mode", "ENV_FILE_MODE_UNSAFE"),
        ("env-symlink", "ENV_FILE_SYMLINK_FORBIDDEN"),
        ("env-missing", "ENV_FILE_MISSING"),
        ("relative-env", "ENV_FILE_NOT_ABSOLUTE"),
    ],
)
def test_wrapper_rejects_unsafe_env_file(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    wrapper = _WRAPPER_PATH
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    if case == "env-mode":
        env_file.chmod(0o644)
    elif case == "env-symlink":
        target = tmp_path / "real.env"
        target.write_text("", encoding="utf-8")
        target.chmod(0o600)
        env_file.unlink()
        env_file.symlink_to(target)
    elif case == "env-missing":
        env_file.unlink()
    process_env = {
        **os.environ,
        "NODE27_TIMESERIES_RETENTION_ENV_FILE": (
            "relative.env" if case == "relative-env" else str(env_file)
        ),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    combined = result.stderr + result.stdout + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert expected_reason in combined


def test_wrapper_paths_absolute() -> None:
    text = _WRAPPER_PATH.read_text(encoding="utf-8")
    assert "REPO=" in text
    assert "NODE27_TIMESERIES_RETENTION_LOG_ROOT" in text
    assert "flock" in text  # bootstrap-lock preserved
    assert "0600" in text or "600" in text


# ---------------------------------------------------------------------------
# Systemd unit shape
# ---------------------------------------------------------------------------


def test_service_bootstraps_log_dir() -> None:
    service_text = _SERVICE_PATH.read_text(encoding="utf-8")
    assert (
        "ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-timeseries-retention-logs"
        in service_text
    )
    assert (
        "StandardOutput=append:/home/nwm/node27-timeseries-retention-logs/systemd.log"
        in service_text
    )
    assert (
        "ExecStart=/home/nwm/NWM/scripts/node27_timeseries_retention_once.sh"
        in service_text
    )


def test_timer_calendar_matches_fixture() -> None:
    timer_text = _TIMER_PATH.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 05:15:00 UTC" in timer_text
    assert "Unit=nhms-node27-timeseries-retention.service" in timer_text
    assert "WantedBy=timers.target" in timer_text


def test_env_example_lists_all_h13_keys() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    for key in (
        "DATABASE_URL",
        "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS",
        "NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND",
        "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_LOCK_PATH",
        "NODE27_TIMESERIES_RETENTION_ENFORCE",
        "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE",
    ):
        assert re.search(rf"^#?{re.escape(key)}=", text, flags=re.MULTILINE), f"missing {key}"


# ---------------------------------------------------------------------------
# G F2 — H7 straddling chunk: range_start < cutoff < range_end NOT included.
# G F3 — Mixed-compressed: eligible list contains compressed + uncompressed.
# G Schema C1 — jsonschema validation on every surviving refused shape.
# G Schema C2 — FormatChecker on salvage_backed_windows date-time.
# ---------------------------------------------------------------------------


def test_chunk_straddling_cutoff_is_not_eligible() -> None:
    """G F2: SQL predicate ``range_end <= cutoff`` excludes chunks whose
    ``range_end > cutoff`` even if ``range_start < cutoff``. The predicate
    lives in ``_CHUNK_QUERY``; sanity-check the shape here (real filter
    exercised by ``_default_fetch_chunks`` at the DB layer).
    """
    query = retention._CHUNK_QUERY
    assert "range_end <= %s" in query
    # A straddling chunk (range_start < cutoff < range_end) has
    # range_end > cutoff, so the non-strict predicate rejects it — the
    # entire chunk range is NOT older than the drop window. The runner
    # therefore never sees straddling chunks in ``eligible[]``.


def test_retention_cutoff_uses_display_watermark_not_the_wall_clock(
    tmp_path: Path,
) -> None:
    """The drop cutoff is derived from the display watermark passed in as
    ``reference_time``; ``generated_at`` still stamps the wall clock.
    """
    wall_time = datetime(2026, 7, 22, 0, tzinfo=UTC)
    reference_time = datetime(2026, 7, 11, 12, tzinfo=UTC)
    config = _build_config(tmp_path)
    stub = _StubRunner([])

    receipt = retention.run_retention(
        config,
        wall_time,
        reference_time=reference_time,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "dry-run"
    assert receipt["generated_at"] == "2026-07-22T00:00:00Z"
    assert receipt["reference_time"] == "2026-07-11T12:00:00Z"
    assert receipt["cutoff"] == "2026-06-27T12:00:00Z"
    assert ("fetch", datetime(2026, 6, 27, 12, tzinfo=UTC)) in stub.calls


def test_mixed_compressed_and_uncompressed_chunks_both_drop(tmp_path: Path) -> None:
    """G F3: eligible chunks list may mix ``is_compressed=True`` and ``=False``;
    both flow through the drop path unchanged. Divergence from #851
    compression sibling: compressed chunks older than 14 d ARE retention
    targets (see H3 comment in ``_CHUNK_QUERY``).
    """
    config = _build_config(tmp_path, enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-compressed", delta_days=60, is_compressed=True),
        _chunk("met", "forcing_station_timeseries", "chk-plain", delta_days=61, is_compressed=False),
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    dropped_names = {c["name"] for c in receipt["dropped_chunks"]}
    assert dropped_names == {
        "_timescaledb_internal.chk-compressed",
        "_timescaledb_internal.chk-plain",
    }


@pytest.mark.parametrize(
    "wire_code",
    [
        retention.CODE_RETENTION_CONFIG_INVALID,
        retention.CODE_RETENTION_CONCURRENT_INVOCATION,
        retention.CODE_RETENTION_DROP_FAILED,
        retention.CODE_RETENTION_UNCAUGHT_ERROR,
    ],
)
def test_refused_receipt_shape_validates_against_schema(wire_code: str) -> None:
    """G Schema C1: a refused receipt for each surviving wire code is
    schema-conformant and self-describes the `disabled` deletion authority.
    """
    receipt = retention.build_receipt("refused", _NOW, refusal_reason=wire_code)
    jsonschema.validate(receipt, _load_schema())
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}


def test_enforced_receipt_salvage_backed_window_datetime_format_enforced() -> None:
    """G Schema C2: FormatChecker enforces ``format: date-time`` on
    salvage-backed window ``start``/``end``. Reject a bad-format string
    via the retention module's own ``_validate_receipt`` (which registers
    a custom date-time checker that reuses ``_parse_iso`` — same acceptance
    oracle as the emitter).
    """
    bad_receipt = {
        "schema_version": "1.1",
        "generated_at": _iso(_NOW),
        "mode": "enforce",
        "outcome": "enforced",
        "archive_gate": {
            "mode": "disabled",
            "adr_reference": (
                "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"
            ),
        },
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [{"start": "not-a-datetime", "end": _iso(_NOW)}],
    }
    with pytest.raises(jsonschema.ValidationError):
        retention._validate_receipt(bad_receipt)


def test_enforced_receipt_generated_at_datetime_format_enforced() -> None:
    """G Schema C2 (symmetric): ``generated_at`` bad format is also caught."""
    bad_receipt = {
        "schema_version": "1.1",
        "generated_at": "not-a-datetime",
        "mode": "enforce",
        "outcome": "enforced",
        "archive_gate": {
            "mode": "disabled",
            "adr_reference": (
                "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"
            ),
        },
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        retention._validate_receipt(bad_receipt)


# ---------------------------------------------------------------------------
# #1369/#1370 — the archive gate is `disabled` and nothing else
# (ADR 0002 Revision 2026-08-11)
#
# Two orthogonal axes remain: dry-run/enforce x outcome branch. The gate mode
# is a constant: `disabled` is the only accepted value, and the `enabled` mode
# retired with the archive lane (#1370). The pins below are written against
# the fixture's own strings (env key, enum value, ADR reference constant),
# never echoed from the module, so a rename in the runner cannot silently drag
# the oracle along.
# ---------------------------------------------------------------------------


_ARCHIVE_GATE_ENV_KEY = "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE"
# Verbatim from docs/adr/0002-node27-timeseries-hot-cold-tiering.md
# "Revision 2026-08-11 — archive lane retired; delete-without-archive authorized".
_ADR_REFERENCE = "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"
_ADR_PATH_ANCHOR = "docs/adr/0002-node27-timeseries-hot-cold-tiering.md"
_ADR_REVISION_ANCHOR = "Revision 2026-08-11"

# #1370 diagnostics contract: refusing an unset / `enabled` / bogus gate value
# must name the retirement authority AND tell the operator what to set. Both
# fragments are spelled literally here — they are the operator-facing text the
# spec scenario pins, not an echo of the runner's f-string.
_RETIREMENT_DIAGNOSTIC = "archive lane permanently retired (ADR 0002 Revision 2026-08-11)"
_EXPLICIT_DISABLED_INSTRUCTION = f"set {_ARCHIVE_GATE_ENV_KEY}=disabled"

_RUNNER_OWN_CODES = frozenset(
    {
        "RETENTION_CONFIG_INVALID",
        "RETENTION_CONCURRENT_INVOCATION",
        "RETENTION_DROP_FAILED",
        "RETENTION_UNCAUGHT_ERROR",
    }
)


def test_wire_codes_are_exactly_the_four_runner_own_codes() -> None:
    """#1370: after the archive family retired, `WIRE_CODES` is the runner's
    own refusal vocabulary and nothing else.
    """
    assert retention.WIRE_CODES == _RUNNER_OWN_CODES


# --- D1 mode parse table (#1370: `disabled` or refuse) --------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("disabled", id="disabled"),
        pytest.param("  disabled  ", id="disabled-surrounding-spaces"),
        pytest.param("\tdisabled\n", id="disabled-surrounding-tabs"),
        pytest.param("DISABLED", id="disabled-uppercase"),
        pytest.param("Disabled", id="disabled-mixed-case"),
    ],
)
def test_archive_gate_env_parse_table_accepts_only_disabled(tmp_path: Path, raw: str) -> None:
    """D1: strip+lower must equal ``disabled``; every accepted spelling of it
    resolves to the one surviving mode.
    """
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: raw})
    config = retention.config_from_args(_args(), env)
    assert config.archive_gate == "disabled"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="unset"),
        pytest.param("enabled", id="retired-enabled"),
        pytest.param("\tEnabled\n", id="retired-enabled-mixed-case"),
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("disable", id="typo-disable"),
        pytest.param("true", id="truthy-true"),
        pytest.param("1", id="truthy-one"),
        pytest.param("0", id="falsy-zero"),
        pytest.param("off", id="off"),
        pytest.param("enabled disabled", id="both"),
    ],
)
def test_archive_gate_anything_but_disabled_is_config_invalid(
    tmp_path: Path, raw: str | None
) -> None:
    """D1: the archive lane is retired, so `disabled` is the only mode.

    Unset is refused too — the fail-closed direction is preserved (an unset
    variable never silently deletes), it just refuses as config-invalid now
    instead of running an archive gate that can never be satisfied.
    """
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: raw})
    with pytest.raises(retention.RetentionConfigError) as error:
        retention.config_from_args(_args(), env)
    message = str(error.value)
    assert _ARCHIVE_GATE_ENV_KEY in message
    assert _RETIREMENT_DIAGNOSTIC in message
    assert _EXPLICIT_DISABLED_INSTRUCTION in message


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="unset"),
        pytest.param("enabled", id="retired-enabled"),
        pytest.param("disable", id="typo-disable"),
    ],
)
def test_main_non_disabled_archive_gate_exits_two_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str | None,
) -> None:
    """D1 end-to-end: exit 2, RETENTION_CONFIG_INVALID on stderr, NO receipt,
    and the diagnostics carry the retirement authority + the instruction.
    """
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: raw})
    monkeypatch.delenv(_ARCHIVE_GATE_ENV_KEY, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])

    code = retention.main(argv=[], now=_NOW)

    assert code == 2
    assert not receipt_path.exists()
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["code"] == retention.CODE_RETENTION_CONFIG_INVALID
    assert _ARCHIVE_GATE_ENV_KEY in payload["reason"]
    assert _RETIREMENT_DIAGNOSTIC in payload["reason"]
    assert _EXPLICIT_DISABLED_INSTRUCTION in payload["reason"]


def test_archive_gate_cli_disabled_beats_a_retired_env_value(tmp_path: Path) -> None:
    """CLI wins over env, matching the ``--enforce`` precedent."""
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: "enabled"})
    config = retention.config_from_args(_args(archive_gate="disabled"), env)
    assert config.archive_gate == "disabled"


def test_archive_gate_cli_enabled_is_refused_even_over_a_disabled_env(
    tmp_path: Path,
) -> None:
    """The CLI cannot resurrect the retired mode: `--archive-gate enabled`
    parses (argparse choices keep both values so the refusal carries the wire
    code and the ADR text) and is then refused by the resolver.
    """
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: "disabled"})
    with pytest.raises(retention.RetentionConfigError) as error:
        retention.config_from_args(_args(archive_gate="enabled"), env)
    assert _RETIREMENT_DIAGNOSTIC in str(error.value)


def test_parser_archive_gate_choices_keep_both_values(tmp_path: Path) -> None:
    """`enabled` stays an argparse choice on purpose: rejecting it in argparse
    would emit a bare usage error with no wire code and no ADR citation. The
    single refusal path is ``_resolve_archive_gate``.
    """
    parser = retention._parser()
    assert parser.parse_args([]).archive_gate is None
    assert parser.parse_args(["--archive-gate", "disabled"]).archive_gate == "disabled"
    assert parser.parse_args(["--archive-gate", "enabled"]).archive_gate == "enabled"
    with pytest.raises(SystemExit):
        parser.parse_args(["--archive-gate", "disable"])


# --- the retired gate machinery is gone -----------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "load_completeness_receipt",
        "load_drill_receipt",
        "check_completeness_gate",
        "check_drill_gate",
        "derive_salvage_backed_windows",
        "ReceiptGateError",
    ],
)
def test_retired_gate_machinery_is_absent_from_the_module(symbol: str) -> None:
    """#1370: the loaders and both gate adjudications no longer exist."""
    assert not hasattr(retention, symbol)


@pytest.mark.parametrize(
    "field_name",
    [
        "completeness_receipt_path",
        "drill_receipt_path",
        "completeness_max_age_hours",
        "drill_max_age_days",
    ],
)
def test_retired_config_fields_are_gone(tmp_path: Path, field_name: str) -> None:
    config = retention.config_from_args(_args(), _base_env(tmp_path))
    assert not hasattr(config, field_name)


@pytest.mark.parametrize(
    "env_key",
    [
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS",
        "NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS",
    ],
)
def test_retired_env_keys_are_ignored_not_required(tmp_path: Path, env_key: str) -> None:
    """A box that still carries a retired key in its env file keeps working:
    the runner never reads it, and never demands it either.
    """
    env = _base_env(tmp_path, **{env_key: "/leftover/value"})
    config = retention.config_from_args(_args(), env)
    assert config.archive_gate == "disabled"

    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])
    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )
    assert receipt["outcome"] == "dry-run"
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}


# --- tasks 2.3: D2 behavior -----------------------------------------------


def test_disabled_dry_run_receipt_shape(tmp_path: Path) -> None:
    config = _build_config(tmp_path, archive_gate="disabled", per_tick_bound=2)
    chunks = [
        _chunk("hydro", "river_timeseries", f"chk-{i}", delta_days=60 - i) for i in range(3)
    ]
    stub = _StubRunner(chunks)

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "dry-run"
    assert receipt["mode"] == "dry-run"
    assert receipt["candidate_chunks"] == [
        "_timescaledb_internal.chk-0",
        "_timescaledb_internal.chk-1",
    ]
    assert receipt["deferred_remainder"] == ["_timescaledb_internal.chk-2"]
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}
    assert not any(call[0] == "drop" for call in stub.calls)
    jsonschema.validate(receipt, _load_schema())


def test_disabled_enforce_drops_and_records_authorization(tmp_path: Path) -> None:
    """Spec scenario: disabled + enforce deletes without archive receipts and
    the receipt records the authorization, with ``salvage_backed_windows``
    pinned to the empty list (no archive endorsement exists).
    """
    config = _build_config(tmp_path, archive_gate="disabled", enforce=True)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-a", delta_days=60),
        _chunk("met", "forcing_station_timeseries", "chk-b", delta_days=61),
    ]
    stub = _StubRunner(chunks, measured={"_timescaledb_internal.chk-a": 7})

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "enforced"
    assert [item["name"] for item in receipt["dropped_chunks"]] == [
        "_timescaledb_internal.chk-a",
        "_timescaledb_internal.chk-b",
    ]
    assert receipt["salvage_backed_windows"] == []
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}
    assert receipt["schema_version"] == "1.1"
    # H4 ordering unchanged: measure precedes every drop.
    assert [call[0] for call in stub.calls] == ["fetch", "measure", "drop", "drop"]
    jsonschema.validate(receipt, _load_schema())


def test_boundary_partial_chunk_is_a_drop_candidate(tmp_path: Path) -> None:
    """Spec scenario: a chunk that straddles what used to be the inventory
    coverage boundary is a drop candidate — the completeness-bounds deferral
    retired with the archive lane (runbook §8.5).

    The two chunks below are the exact fixture the retired bounds partition
    split: `chk-partial` began before a `_NOW - 65 d` coverage start,
    `chk-covered` lay wholly inside it. Both are now candidates.
    """
    partial = _chunk("hydro", "river_timeseries", "chk-partial", delta_days=60, duration_days=7)
    covered = _chunk("hydro", "river_timeseries", "chk-covered", delta_days=53, duration_days=7)

    receipt = retention.run_retention(
        _build_config(tmp_path, per_tick_bound=5),
        _NOW,
        fetch_chunks=_StubRunner([partial, covered]).fetch,
    )

    assert receipt["candidate_chunks"] == [
        partial.qualified_name,
        covered.qualified_name,
    ]
    assert receipt["deferred_remainder"] == []
    jsonschema.validate(receipt, _load_schema())


# --- every refusal is one of the four runner-own codes --------------------


def _assert_disabled_refusal(receipt: Mapping[str, Any]) -> None:
    assert receipt["outcome"] == "refused"
    prefix = str(receipt["refusal_reason"]).split(":", maxsplit=1)[0]
    assert prefix not in _RETIRED_ARCHIVE_FAMILY_CODES, prefix
    assert prefix in _RUNNER_OWN_CODES, prefix
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}
    jsonschema.validate(receipt, _load_schema())


def test_disabled_drop_failure_refusal_stays_runner_own(tmp_path: Path) -> None:
    config = _build_config(tmp_path, archive_gate="disabled", enforce=True)
    chunks = [_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)]
    stub = _StubRunner(chunks, drop_error={"chk-a": RuntimeError("drop blew up")})

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    _assert_disabled_refusal(receipt)
    assert receipt["refusal_reason"].startswith(retention.CODE_RETENTION_DROP_FAILED)


def test_disabled_concurrent_invocation_refusal_carries_archive_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    lock_path = Path(env["NODE27_TIMESERIES_RETENTION_LOCK_PATH"])
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code = retention.main(argv=[], now=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["refusal_reason"] == retention.CODE_RETENTION_CONCURRENT_INVOCATION
    _assert_disabled_refusal(receipt)
    assert retention.CODE_RETENTION_CONCURRENT_INVOCATION in capsys.readouterr().err


def test_disabled_uncaught_error_refusal_carries_archive_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def _bang_fetch(
        config: retention.RetentionConfig, cutoff: datetime
    ) -> list[retention.ChunkRow]:
        raise RuntimeError("catalog probe blew up")

    code = retention.main(argv=[], now=_NOW, fetch_chunks=_bang_fetch)

    assert code == 1
    receipt = json.loads(
        Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_text(encoding="utf-8")
    )
    _assert_disabled_refusal(receipt)
    assert receipt["refusal_reason"].startswith(
        f"{retention.CODE_RETENTION_UNCAUGHT_ERROR}:RuntimeError"
    )


def test_disabled_main_enforce_end_to_end_publishes_enforced_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-entrypoint pin: env-only disabled config (no archive path vars),
    enforce on, receipt published and schema-valid.
    """
    env = _base_env(tmp_path, NODE27_TIMESERIES_RETENTION_ENFORCE="1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])

    code = retention.main(
        argv=[],
        now=_NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert code == 0
    receipt = json.loads(
        Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"]).read_text(encoding="utf-8")
    )
    assert receipt["outcome"] == "enforced"
    assert receipt["salvage_backed_windows"] == []
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}
    jsonschema.validate(receipt, _load_schema())


# --- tasks 2.5: D3 schema pins --------------------------------------------


def _enforced_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1.1",
        "generated_at": _iso(_NOW),
        "mode": "enforce",
        "outcome": "enforced",
        "archive_gate": {"mode": "disabled", "adr_reference": _ADR_REFERENCE},
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [],
    }
    document.update(overrides)
    return document


def test_schema_version_is_bumped_to_1_1() -> None:
    schema = _load_schema()
    assert schema["properties"]["schema_version"]["const"] == "1.1"
    assert retention.SCHEMA_VERSION == "1.1"
    assert "archive_gate" in schema["required"]


def test_disabled_enforced_receipt_document_is_valid() -> None:
    """Positive: disabled enforced with an empty salvage list validates."""
    jsonschema.validate(_enforced_document(), _load_schema())
    retention._validate_receipt(_enforced_document())


@pytest.mark.parametrize(
    ("label", "document"),
    [
        pytest.param(
            "missing-archive-gate",
            {k: v for k, v in _enforced_document().items() if k != "archive_gate"},
            id="missing-archive-gate",
        ),
        pytest.param(
            "disabled-without-adr",
            _enforced_document(archive_gate={"mode": "disabled"}),
            id="disabled-without-adr-reference",
        ),
        pytest.param(
            "disabled-wrong-adr",
            _enforced_document(
                archive_gate={
                    "mode": "disabled",
                    "adr_reference": "docs/adr/0002-node27-timeseries-hot-cold-tiering.md",
                }
            ),
            id="disabled-non-const-adr-reference",
        ),
        pytest.param(
            "enabled-with-adr",
            _enforced_document(
                archive_gate={"mode": "enabled", "adr_reference": _ADR_REFERENCE}
            ),
            id="enabled-carrying-adr-reference",
        ),
        pytest.param(
            "unknown-mode",
            _enforced_document(archive_gate={"mode": "bypassed"}),
            id="mode-outside-enum",
        ),
        pytest.param(
            "extra-key",
            _enforced_document(
                archive_gate={
                    "mode": "disabled",
                    "adr_reference": _ADR_REFERENCE,
                    "note": "why not",
                }
            ),
            id="archive-gate-extra-property",
        ),
        pytest.param(
            "stale-schema-version",
            _enforced_document(schema_version="1.0"),
            id="schema-version-still-1-0",
        ),
    ],
)
def test_schema_rejects_unauditable_gate_records(label: str, document: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, _load_schema())


def test_build_receipt_rejects_unknown_archive_gate_mode() -> None:
    with pytest.raises(ValueError, match="archive gate"):
        retention.build_receipt("dry-run", _NOW, archive_gate="bypassed")


@pytest.mark.parametrize("outcome", ["dry-run", "refused", "enforced"])
@pytest.mark.parametrize("gate", ["enabled", "disabled"])
def test_build_receipt_carries_archive_gate_on_all_three_branches(
    outcome: str, gate: str
) -> None:
    kwargs: dict[str, Any] = {"archive_gate": gate}
    if outcome == "refused":
        kwargs["refusal_reason"] = retention.CODE_RETENTION_UNCAUGHT_ERROR
    receipt = retention.build_receipt(outcome, _NOW, **kwargs)
    expected = {"mode": gate}
    if gate == "disabled":
        expected["adr_reference"] = _ADR_REFERENCE
    assert receipt["archive_gate"] == expected
    assert receipt["schema_version"] == "1.1"


def test_shipped_receipt_example_matches_schema_1_1() -> None:
    example = json.loads(
        (_ROOT / "schemas/examples/timeseries_retention_receipt.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert example["schema_version"] == "1.1"
    assert example["archive_gate"]["mode"] in {"enabled", "disabled"}
    jsonschema.validate(example, _load_schema())


# --- tasks 2.6: documentation byte anchors --------------------------------


def _runbook_section(heading: str, next_heading: str) -> str:
    text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.index(next_heading, start)
    return text[start:end]


def test_runbook_8_4_preconditions_cite_the_adr_revision() -> None:
    """§8.4 must present disabled mode as an ADR-cited alternative to the
    two-receipt preconditions — verbatim path + revision anchors.
    """
    section = _runbook_section("### 8.4 How to run", "### 8.5 Reading the receipt")
    assert _ADR_PATH_ANCHOR in section
    assert _ADR_REVISION_ANCHOR in section
    assert _ARCHIVE_GATE_ENV_KEY in section


def test_runbook_8_5_documents_archive_gate_field_and_boundary_partial_change() -> None:
    """§8.5 must document the field AND the boundary-partial semantics change.

    The field-name anchors alone are satisfied by a §8.5 that never says what
    `disabled` mode does to boundary-partial chunks, so the spec THEN-clause's
    §8.5 leg needs the semantic sentence pinned too: in `disabled` mode such
    chunks are NOT deferred, they enter `candidate_chunks[]` and are dropped.
    """
    section = _runbook_section("### 8.5 Reading the receipt", "### 8.6 Recovery")
    assert "archive_gate" in section
    assert _ARCHIVE_GATE_ENV_KEY in section
    assert "boundary-partial" in section
    assert "NOT deferred" in section
    assert "candidate_chunks" in section


def test_env_example_warns_about_the_missing_archive_backstop() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert re.search(rf"^#?{re.escape(_ARCHIVE_GATE_ENV_KEY)}=", text, flags=re.MULTILINE)
    assert _ADR_PATH_ANCHOR in text
    assert _ADR_REVISION_ANCHOR in text
    assert "irreversible" in text.lower()


def test_runbook_never_uses_the_bare_archive_gate_token() -> None:
    """The reverse wire-code walk treats a bare ``RETENTION_ARCHIVE_GATE``
    token as an orphan code; the env var must always appear fully qualified.
    """
    runbook_text = _RUNBOOK_PATH.read_text(encoding="utf-8")
    bare = re.findall(r"(?<![A-Z0-9_])RETENTION_ARCHIVE_GATE", runbook_text)
    assert not bare, "runbook must spell the env key in full"
