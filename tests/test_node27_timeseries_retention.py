"""Unit tests for the node-27 timeseries retention runner (issue #855 §6.1 + §6.2).

Covers:

- H1 completeness receipt authority + bounds/gap/pending refusal ordering.
- H2 drill per-source coverage + FAIL / stale / missing refusal ordering.
- H3 per-tick bound + deferred_remainder.
- H4 freed_bytes measured BEFORE drop (mock-ordering assertion).
- H5 per-chunk drop failure → whole-tick refused (H5 fail-closed).
- H6 wire codes byte-identical across code / runbook §8.2 / design #855.
- H7 boundary predicate ``range_end <= cutoff``.
- H8 freshness at boundary + past.
- H9 salvage_backed_windows derivation.
- H10 _default_lock_path() byte-identity + zero-arg signature parity.
- H11 governance registration (covered in test_node27_resource_governance.py).
- H17 zero-eligible enforce → outcome=enforced, all arrays empty, exit 0.
- Config parse fail-closed rows.
- Concurrent-invocation flock path → RETENTION_CONCURRENT_INVOCATION.
- Uncaught error path → RETENTION_UNCAUGHT_ERROR.
- #1213 credential redaction of every persisted error surface (receipt file
  bytes + stderr/wrapper log) on the drop-phase and uncaught-fallback paths.
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
        "completeness_receipt_path": None,
        "drill_receipt_path": None,
        "archive_gate": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal schema-valid receipts.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# #1175: the db-export coverage refusal carries a DETAIL SUFFIX localizing the
# shortfall — the FIRST uncovered salvage-backed target in ascending derivation
# order, CLIPPED to the drop window (`<start>/<end>`), or the dedicated
# `no-derivable-window` token for the #1162 D2 empty-derivation branch. The bare
# code stays the registered `WIRE_CODES` member and a strict prefix of both
# forms. These helpers spell the wire token and the separator out literally so
# the assertions are an oracle for the format, not an echo of the emitter.
_DB_EXPORT_MISSING_NO_DERIVABLE = "DRILL_COVERAGE_DB_EXPORT_MISSING:no-derivable-window"


def _db_export_missing(start: datetime, end: datetime) -> str:
    """Expected `refusal_reason` for a per-window db-export shortfall (#1175).

    ``start`` / ``end`` are the CLIPPED bounds the caller derives from its own
    fixture windows ∩ drop window — an inverted clip is rendered verbatim.
    """
    return f"DRILL_COVERAGE_DB_EXPORT_MISSING:{_iso(start)}/{_iso(end)}"


def _completeness_receipt(
    *,
    generated_at: datetime = _NOW - timedelta(hours=1),
    bounds_start: datetime | None = None,
    bounds_end: datetime | None = None,
    subjects: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if bounds_start is None:
        bounds_start = _NOW - timedelta(days=365)
    if bounds_end is None:
        bounds_end = _NOW
    if subjects is None:
        subjects = [
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=60)),
                    "end": _iso(_NOW - timedelta(days=59)),
                },
                "coverage": "product-archive",
                "verdict": "complete",
            }
        ]
    return {
        "schema_version": "1.1",
        "generated_at": _iso(generated_at),
        "outcome": (
            "complete" if all(subject.get("verdict") == "complete" for subject in subjects) else "incomplete"
        ),
        "coverage_bounds": {"start": _iso(bounds_start), "end": _iso(bounds_end)},
        "windows": list(subjects),
        "salvage_selectors": [],
    }


def test_blocked_upstream_receipt_is_distinguishable_from_missing_path(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive-completeness.json"
    blocked = {
        "schema_version": "1.1",
        "generated_at": _iso(_NOW),
        "outcome": "blocked",
        "refusal_reason": "EVIDENCE_BLOCKED",
    }
    path.write_text(json.dumps(blocked), encoding="utf-8")
    assert retention.load_completeness_receipt(path) == blocked
    path.unlink()
    with pytest.raises(retention.ReceiptGateError) as missing:
        retention.load_completeness_receipt(path)
    assert missing.value.code == retention.CODE_COMPLETENESS_RECEIPT_MISSING


def _daily_coverage_tuples(
    start: datetime, end: datetime, source: str
) -> list[dict[str, Any]]:
    """Emit per-cycle 24 h coverage tuples (mirrors the drill's real emit shape).

    The archive rebuild drill emits one coverage tuple per verified product
    manifest (typically one daily cycle → one 24 h window). A retention
    drop window spanning N days is normally covered by N daily tuples whose
    UNION spans the drop window — no single tuple contains the whole drop
    window on its own.

    A2 fixture helper — pattern-level fix for #854 R1 fake-oracle-in-tests:
    real drill receipts NEVER carry a single tuple spanning the full retention window.
    """
    tuples: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=1), end)
        tuples.append(
            {
                "source": source,
                "window": {"start": _iso(cursor), "end": _iso(window_end)},
            }
        )
        cursor = window_end
    return tuples


def _drill_receipt(
    *,
    generated_at: datetime = _NOW - timedelta(days=1),
    verdict: str = "PASS",
    forcing_window: tuple[datetime, datetime] | None = None,
    runs_window: tuple[datetime, datetime] | None = None,
    db_export_window: tuple[datetime, datetime] | None = None,
    forcing_tuples: Sequence[Mapping[str, Any]] | None = None,
    runs_tuples: Sequence[Mapping[str, Any]] | None = None,
    db_export_tuples: Sequence[Mapping[str, Any]] | None = None,
    differences: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a drill receipt fixture emitting per-cycle 24 h coverage tuples.

    Callers that pass a scalar ``forcing_window=(a, b)`` receive per-day
    tuples covering ``[a, b]`` via :func:`_daily_coverage_tuples`; callers
    that want a custom shape (gap in the middle, overlapping cycles,
    single-day-only coverage) pass explicit ``*_tuples`` sequences instead.

    Passing ``*_window=None`` still means "no coverage for this source"
    (matches legacy shape); passing ``*_tuples=[]`` also means "no
    coverage". A ``forcing_window`` without an explicit ``forcing_tuples``
    override is auto-day-split.
    """
    # Default covers [_NOW - 100 d, _NOW] as ~100 per-day tuples for both
    # timeseries sources — enough union to cover any drop window used in
    # tests (chunks are typically 60-90 days old). The wide default keeps
    # tests focused on gate behavior rather than boundary arithmetic.
    if forcing_tuples is None and forcing_window is None:
        forcing_window = (_NOW - timedelta(days=100), _NOW)
    if runs_tuples is None and runs_window is None:
        runs_window = (_NOW - timedelta(days=100), _NOW)
    coverage: list[dict[str, Any]] = []
    if forcing_tuples is not None:
        coverage.extend(dict(t) for t in forcing_tuples)
    elif forcing_window is not None:
        coverage.extend(_daily_coverage_tuples(forcing_window[0], forcing_window[1], "forcing"))
    if runs_tuples is not None:
        coverage.extend(dict(t) for t in runs_tuples)
    elif runs_window is not None:
        coverage.extend(_daily_coverage_tuples(runs_window[0], runs_window[1], "runs"))
    if db_export_tuples is not None:
        coverage.extend(dict(t) for t in db_export_tuples)
    elif db_export_window is not None:
        coverage.extend(_daily_coverage_tuples(db_export_window[0], db_export_window[1], "db-export"))
    receipt: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": _iso(generated_at),
        "verdict": verdict,
        "staging_database": {
            "database": "nhms_drill",
            "schema": "archive_drill_20260710",
            "instance_id": "node27-primary-pg15",
        },
        "coverage": coverage,
    }
    if verdict == "PASS":
        receipt["comparisons"] = {
            "cycles": ["runs-cycle-1"],
            "selectors": [],
            "counts": [{"item": "runs-cycle-1", "expected": 10, "actual": 10}],
        }
    else:
        receipt["differences"] = list(differences or [])
        if not receipt["differences"]:
            receipt["differences"] = [
                {"item": "drill", "expected": {"code": "STAGING_COUNT_MISMATCH"}, "actual": {"row_count": 0}}
            ]
    return receipt


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
    completeness_path = tmp_path / "completeness.json"
    drill_path = tmp_path / "drill.json"
    receipt_path = tmp_path / "receipt.json"
    lock_path = tmp_path / "runner.lock"
    if not completeness_path.exists():
        _write_json(completeness_path, _completeness_receipt())
    if not drill_path.exists():
        _write_json(drill_path, _drill_receipt())
    kwargs: dict[str, Any] = {
        "database_url": "postgresql://user:pw@127.0.0.1:55432/nhms",
        "window_days": _DROP_WINDOW_DAYS,
        "per_tick_bound": 5,
        "completeness_receipt_path": completeness_path,
        "drill_receipt_path": drill_path,
        "completeness_max_age_hours": 26,
        "drill_max_age_days": 30,
        "receipt_path": receipt_path,
        "lock_path": lock_path,
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


_EXPECTED_WIRE_CODES = frozenset(
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
        "RETENTION_CONFIG_INVALID",
        "RETENTION_CONCURRENT_INVOCATION",
        "RETENTION_DROP_FAILED",
        "RETENTION_UNCAUGHT_ERROR",
    }
)


def test_wire_codes_match_fixture_exactly() -> None:
    """H6: WIRE_CODES frozenset content is byte-identical with the fixture."""
    assert retention.WIRE_CODES == _EXPECTED_WIRE_CODES
    assert len(retention.WIRE_CODES) == 17


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
    completeness_path = tmp_path / "completeness.json"
    drill_path = tmp_path / "drill.json"
    if not completeness_path.exists():
        _write_json(completeness_path, _completeness_receipt())
    if not drill_path.exists():
        _write_json(drill_path, _drill_receipt())
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH": str(completeness_path),
        "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH": str(drill_path),
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
    assert config.completeness_max_age_hours == 26
    assert config.drill_max_age_days == 30
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
        ({"NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS": "0"}, "COMPLETENESS_MAX_AGE_HOURS"),
        ({"NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS": "-1"}, "DRILL_MAX_AGE_DAYS"),
        ({"NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH": None}, "COMPLETENESS_RECEIPT_PATH"),
        ({"NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH": None}, "DRILL_RECEIPT_PATH"),
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
# H1 completeness receipt authority — one refusal per case (spec §6.1 row 1)
# ---------------------------------------------------------------------------


def test_completeness_receipt_missing_refuses(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    # Delete completeness receipt.
    config.completeness_receipt_path.unlink()
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_MISSING
    assert stub.calls == []  # never fetched
    jsonschema.validate(receipt, _load_schema())


def test_completeness_receipt_stale_refuses(tmp_path: Path) -> None:
    stale = _completeness_receipt(generated_at=_NOW - timedelta(hours=27))
    completeness_path = tmp_path / "completeness.json"
    _write_json(completeness_path, stale)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_STALE
    jsonschema.validate(receipt, _load_schema())


def test_completeness_bounds_insufficient_refuses(tmp_path: Path) -> None:
    """H1 (a): coverage_bounds must fully contain the drop window."""
    completeness = _completeness_receipt(
        # bounds narrower than the drop window's start.
        bounds_start=_NOW - timedelta(days=40),
        bounds_end=_NOW - timedelta(days=32),
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-old", delta_days=80, duration_days=7),
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT
    jsonschema.validate(receipt, _load_schema())


def test_boundary_partial_chunk_is_deferred_while_fully_covered_chunk_progresses(
    tmp_path: Path,
) -> None:
    """A physical chunk may start before the first evidenced row window.

    That boundary-partial chunk remains intact, but it must not globally
    block the next chunk whose complete range is inside the receipt bounds.
    """
    completeness = _completeness_receipt(
        bounds_start=_NOW - timedelta(days=65),
        bounds_end=_NOW,
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path, per_tick_bound=5, enforce=False)
    partial = _chunk(
        "hydro", "river_timeseries", "chk-partial", delta_days=60, duration_days=7
    )
    covered = _chunk(
        "hydro", "river_timeseries", "chk-covered", delta_days=53, duration_days=7
    )
    stub = _StubRunner([partial, covered])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "dry-run"
    assert receipt["candidate_chunks"] == [covered.qualified_name]
    assert receipt["deferred_remainder"] == [partial.qualified_name]
    assert not any(call[0] == "drop" for call in stub.calls)
    jsonschema.validate(receipt, _load_schema())


def test_completeness_gap_in_drop_window_refuses(tmp_path: Path) -> None:
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "runs",
                "subject": {"run_id": "run-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=63)),
                },
                "coverage": "none",
                "verdict": "gap",
            }
        ]
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW


def test_completeness_pending_in_drop_window_refuses(tmp_path: Path) -> None:
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=63)),
                },
                "coverage": "hot-object-store",
                "verdict": "pending-archive",
            }
        ]
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW


# ---------------------------------------------------------------------------
# H2 drill receipt — one refusal per shortfall (spec §6.1 row 2)
# ---------------------------------------------------------------------------


def test_drill_receipt_missing_refuses(tmp_path: Path) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    config.drill_receipt_path.unlink()
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_RECEIPT_MISSING


def test_db_export_recovery_participates_in_forcing_coverage_union(tmp_path: Path) -> None:
    """Verified DB-export objects recover forcing rows when no product
    forcing package exists for the historical interval.
    """
    start = _NOW - timedelta(days=67)
    end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "forc-salvaged"},
                "window": {"start": _iso(start), "end": _iso(end)},
                "coverage": "db-export",
                "verdict": "complete",
            }
        ]
    )
    drill = _drill_receipt(
        forcing_tuples=[],
        runs_window=(start, end),
        db_export_window=(start, end),
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path, enforce=False)
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-salvaged", delta_days=60, duration_days=7
    )
    stub = _StubRunner([chunk])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "dry-run"
    assert receipt["candidate_chunks"] == [chunk.qualified_name]
    jsonschema.validate(receipt, _load_schema())


def test_drill_receipt_stale_refuses(tmp_path: Path) -> None:
    stale_drill = _drill_receipt(generated_at=_NOW - timedelta(days=45))
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", stale_drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_RECEIPT_STALE


def test_drill_receipt_fail_refuses(tmp_path: Path) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(verdict="FAIL"),
    )
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_RECEIPT_FAIL


def test_drill_coverage_forcing_missing_refuses(tmp_path: Path) -> None:
    """A2 real-shape: drill emits per-cycle daily runs tuples, ZERO forcing tuples."""
    # Provide only runs coverage; drill lacks forcing coverage entirely.
    drill = _drill_receipt(forcing_tuples=[])
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_COVERAGE_FORCING_MISSING


def test_drill_coverage_runs_missing_refuses(tmp_path: Path) -> None:
    """A2 real-shape: drill emits per-cycle daily forcing tuples, ZERO runs tuples."""
    drill = _drill_receipt(runs_tuples=[])
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_COVERAGE_RUNS_MISSING


def test_drill_coverage_db_export_missing_refuses(tmp_path: Path) -> None:
    """H2: db-export required iff completeness has db-export subject overlap."""
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-salvage"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=63)),
                },
                "coverage": "db-export",
                "verdict": "complete",
            }
        ]
    )
    # Drill has forcing + runs but NO db-export coverage.
    drill = _drill_receipt(db_export_window=None)
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    # #1175: drop window is the eligible chunk's range [72 d, 65 d]; the sole
    # salvage subject [70 d, 63 d] clips to [70 d, 65 d].
    assert receipt["refusal_reason"] == _db_export_missing(
        _NOW - timedelta(days=70), _NOW - timedelta(days=65)
    )


def test_drill_coverage_db_export_not_required_without_completeness_overlap(
    tmp_path: Path,
) -> None:
    """H2 symmetry: no completeness db-export subject → no db-export required."""
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    # No db-export coverage in drill either — should still pass since
    # completeness carries no db-export subject overlapping the drop window.
    _write_json(tmp_path / "drill.json", _drill_receipt(db_export_window=None))
    config = _build_config(tmp_path, enforce=True)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"


# ---------------------------------------------------------------------------
# #1162 — the db-export leg is salvage-window-scoped: drill `db-export`
# coverage is required only over each salvage-backed window (completeness
# `coverage=db-export` + `verdict=complete` subject overlapping the drop
# window) intersected with the drop window, evaluated per window. The
# forcing / runs legs keep whole-drop-window UNION semantics.
# ---------------------------------------------------------------------------


def _db_export_subject(
    start: datetime,
    end: datetime,
    *,
    version: str = "fv-salvage",
    verdict: str = "complete",
) -> dict[str, Any]:
    return {
        "lane": "forcing",
        "subject": {"forcing_version_id": version},
        "window": {"start": _iso(start), "end": _iso(end)},
        "coverage": "db-export",
        "verdict": verdict,
    }


def _product_archive_subject(
    start: datetime, end: datetime, *, version: str = "fv-product"
) -> dict[str, Any]:
    return {
        "lane": "forcing",
        "subject": {"forcing_version_id": version},
        "window": {"start": _iso(start), "end": _iso(end)},
        "coverage": "product-archive",
        "verdict": "complete",
    }


def _db_export_tuples(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Per-cycle 24 h `db-export` tuples spanning ``[start, end]``."""
    return _daily_coverage_tuples(start, end, "db-export")


def _run_dry(tmp_path: Path, completeness: Mapping[str, Any], drill: Mapping[str, Any],
             chunk: retention.ChunkRow) -> dict[str, Any]:
    """Drive the full runner in dry-run so the gate verdict is observable."""
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path)
    stub = _StubRunner([chunk])
    return retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )


def test_drill_db_export_scoped_to_salvage_window_admits_mixed_drop_window(
    tmp_path: Path,
) -> None:
    """#1162 regression anchor: a drop window straddling the salvage-era
    boundary is admissible.

    The salvage-backed sub-window sits at the head of the drop window and the
    drill's `db-export` union covers exactly that sub-window; the remainder is
    product-archive-backed and has (and should have) no db-export package.
    The whole-drop-window db-export requirement deadlocked this shape.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    salvage_end = _NOW - timedelta(days=70)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, salvage_end),
            _product_archive_subject(salvage_end, drop_end),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, salvage_end))
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-mixed", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt.get("refusal_reason") is None
    assert receipt["outcome"] == "dry-run"
    assert receipt["candidate_chunks"] == [chunk.qualified_name]


def test_drill_db_export_gap_inside_salvage_window_still_refuses(tmp_path: Path) -> None:
    """#1162: narrowing the span must not weaken the check — a hole inside the
    salvage-backed window (∩ drop window) still refuses."""
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    salvage_end = _NOW - timedelta(days=70)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, salvage_end),
            _product_archive_subject(salvage_end, drop_end),
        ]
    )
    # Union covers [74 d, 72 d] and [71 d, 70 d] — a 24 h hole at [72 d, 71 d].
    drill = _drill_receipt(
        db_export_tuples=(
            _db_export_tuples(drop_start, _NOW - timedelta(days=72))
            + _db_export_tuples(_NOW - timedelta(days=71), salvage_end)
        )
    )
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-gap", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    # #1175: the single salvage target [74 d, 70 d] is already inside the drop
    # window, so the suffix renders it unchanged.
    assert receipt["refusal_reason"] == _db_export_missing(drop_start, salvage_end)


def test_drill_db_export_multiple_salvage_windows_each_must_be_covered(
    tmp_path: Path,
) -> None:
    """#1162: two non-adjacent salvage windows are judged INDEPENDENTLY —
    covering only one refuses (this is the per-window vs. any-window and
    per-window vs. hull discriminator; the space between the two windows is
    product-archive-backed and is never required)."""
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    first_end = _NOW - timedelta(days=72)
    second_start = _NOW - timedelta(days=68)
    second_end = _NOW - timedelta(days=66)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, first_end, version="fv-salvage-a"),
            _db_export_subject(second_start, second_end, version="fv-salvage-b"),
            _product_archive_subject(first_end, second_start, version="fv-product-mid"),
            _product_archive_subject(second_end, drop_end, version="fv-product-tail"),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, first_end))
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-multi-partial", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    # #1175: the FIRST window is covered, so the suffix names the SECOND
    # (uncovered) target [68 d, 66 d] — not the first, not the drop window.
    assert receipt["refusal_reason"] == _db_export_missing(second_start, second_end)


def test_drill_db_export_multiple_salvage_windows_all_covered_admits(
    tmp_path: Path,
) -> None:
    """#1162: both salvage windows covered → admitted even though the drill's
    db-export union has a hole BETWEEN them (that stretch is product-archive
    backed, so a hull/whole-window reading would wrongly refuse)."""
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    first_end = _NOW - timedelta(days=72)
    second_start = _NOW - timedelta(days=68)
    second_end = _NOW - timedelta(days=66)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, first_end, version="fv-salvage-a"),
            _db_export_subject(second_start, second_end, version="fv-salvage-b"),
            _product_archive_subject(first_end, second_start, version="fv-product-mid"),
            _product_archive_subject(second_end, drop_end, version="fv-product-tail"),
        ]
    )
    drill = _drill_receipt(
        db_export_tuples=(
            _db_export_tuples(drop_start, first_end)
            + _db_export_tuples(second_start, second_end)
        )
    )
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-multi-full", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt.get("refusal_reason") is None
    assert receipt["outcome"] == "dry-run"


def test_drill_db_export_refusal_names_the_kth_uncovered_salvage_window(
    tmp_path: Path,
) -> None:
    """#1175 localization oracle: with THREE salvage-backed targets and the gap
    in the MIDDLE one, the suffix names exactly that window.

    The neighbouring two-window row has its gap in the LAST target, so it
    cannot tell "first uncovered target" apart from "last target" or "last
    uncovered target". Here the first and third targets are fully covered by
    the drill's db-export union, so any emitter that reports targets[0],
    targets[-1], the hull of the targets, or the drop window itself renders a
    different interval than the asserted [70 d, 68 d].
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    first_start, first_end = drop_start, _NOW - timedelta(days=72)
    second_start, second_end = _NOW - timedelta(days=70), _NOW - timedelta(days=68)
    third_start, third_end = _NOW - timedelta(days=66), _NOW - timedelta(days=64)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(first_start, first_end, version="fv-salvage-a"),
            _product_archive_subject(first_end, second_start, version="fv-product-1"),
            _db_export_subject(second_start, second_end, version="fv-salvage-b"),
            _product_archive_subject(second_end, third_start, version="fv-product-2"),
            _db_export_subject(third_start, third_end, version="fv-salvage-c"),
            _product_archive_subject(third_end, drop_end, version="fv-product-3"),
        ]
    )
    # Covers the first and third salvage windows; the second has NO db-export
    # tuple at all.
    drill = _drill_receipt(
        db_export_tuples=(
            _db_export_tuples(first_start, first_end)
            + _db_export_tuples(third_start, third_end)
        )
    )
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-kth", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt["refusal_reason"] == _db_export_missing(second_start, second_end)


def test_drill_db_export_empty_salvage_derivation_refuses_fail_closed() -> None:
    """#1162 D2 (function-level, defence in depth): a `coverage=db-export`
    subject that overlaps the drop window but is NOT `verdict=complete`
    derives ZERO salvage-backed windows — the drill leg must treat that as
    unsatisfied, never as satisfied, even when the drill's db-export union
    covers the whole drop window.

    This shape is intercepted twice upstream: the completeness receipt schema
    rejects `db-export` + `pending-archive` at load
    (`coverage_verdict_contract`,
    `schemas/archive_completeness_receipt.schema.json:159-183`), and the
    completeness gate refuses in-window `pending-archive` subjects before the
    drill gate ever runs. It is therefore driven at function level with a
    hand-built completeness dict — the `run_retention` / `_write_json`
    end-to-end path cannot carry it. The case locks the drill leg's own
    fail-closed behaviour, not an observable production path.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = {
        "schema_version": "1.1",
        "generated_at": _iso(_NOW - timedelta(hours=1)),
        "outcome": "incomplete",
        "coverage_bounds": {
            "start": _iso(_NOW - timedelta(days=365)),
            "end": _iso(_NOW),
        },
        "windows": [
            _db_export_subject(drop_start, drop_end, verdict="pending-archive")
        ],
        "salvage_selectors": [],
    }
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, drop_end))

    reasons = retention.check_drill_gate(
        drill,
        completeness_receipt=completeness,
        drop_window=retention.DropWindow(start=drop_start, end=drop_end),
        max_age_days=30,
        now=_NOW,
    )

    # #1175: no window is derivable, so the refusal carries the dedicated
    # payload rather than an interval.
    assert reasons == [_DB_EXPORT_MISSING_NO_DERIVABLE]


def test_drill_db_export_salvage_window_clipped_at_drop_window_start(
    tmp_path: Path,
) -> None:
    """#1162 clip (left): a salvage subject starting BEFORE the drop window is
    required only over the intersection — the drill covers nothing older than
    `drop.start` and is still admitted."""
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    salvage_end = _NOW - timedelta(days=70)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(_NOW - timedelta(days=80), salvage_end),
            _product_archive_subject(salvage_end, drop_end),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, salvage_end))
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-clip-left", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt.get("refusal_reason") is None
    assert receipt["outcome"] == "dry-run"


def test_drill_db_export_salvage_window_clipped_at_drop_window_end(
    tmp_path: Path,
) -> None:
    """#1162 clip (right): a salvage subject running PAST the drop window end
    (the live shape — 7 d forcing_version windows routinely overrun a chunk
    boundary) is required only over the intersection."""
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    salvage_start = _NOW - timedelta(days=64)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(salvage_start, _NOW - timedelta(days=50)),
            _product_archive_subject(drop_start, salvage_start),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(salvage_start, drop_end))
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-clip-right", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt.get("refusal_reason") is None
    assert receipt["outcome"] == "dry-run"


def _drill_gate_reasons(
    completeness: Mapping[str, Any],
    drill: Mapping[str, Any],
    drop_window: retention.DropWindow,
) -> list[str]:
    """Call the H2 gate directly so the db-export leg's FULL verdict list is
    observable.

    The runner collapses the gate to a single `refusal_reason`, which cannot
    distinguish "refused once" from "refused once per salvage window" — the
    clip-edge cases below assert on the exact reason LIST.
    """
    return retention.check_drill_gate(
        drill,
        completeness_receipt=completeness,
        drop_window=drop_window,
        max_age_days=30,
        now=_NOW,
    )


def test_drill_db_export_refusal_renders_clipped_not_raw_subject_bounds() -> None:
    """#1175: a subject overrunning the drop window on BOTH sides (the live
    shape — 7 d forcing_version windows vs. chunk boundaries) is reported by its
    CLIPPED bounds.

    The raw subject spans [80 d, 50 d]; the requirement — and therefore the
    refusal — is only [74 d, 60 d]. An emitter echoing `target["start"]` /
    `target["end"]` would name a window the retention tick never judged and
    would send the operator hunting outside the drop window.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    raw_start = _NOW - timedelta(days=80)
    raw_end = _NOW - timedelta(days=50)
    completeness = _completeness_receipt(
        subjects=[_db_export_subject(raw_start, raw_end, version="fv-salvage-overrun")]
    )
    # Union stops 5 d short of the clipped end → the clipped target is uncovered.
    drill = _drill_receipt(
        db_export_tuples=_db_export_tuples(drop_start, _NOW - timedelta(days=65))
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    assert reasons == [_db_export_missing(drop_start, drop_end)]
    assert _iso(raw_start) not in reasons[0]
    assert _iso(raw_end) not in reasons[0]


def test_drill_db_export_shortfall_at_clipped_window_end_refuses() -> None:
    """#1162 right-edge oracle: a 6 h db-export shortfall at the END of each
    salvage window (∩ drop window) refuses.

    Two salvage windows are in scope — one wholly inside the drop window, one
    overhanging `drop.end` (the live shape) — and the drill's db-export union
    stops 6 h short of BOTH clipped ends. A clip that is even one day loose on
    the right would admit this receipt; the single-element reason list also
    pins that the leg early-returns on the FIRST shortfall rather than
    accumulating one code per uncovered window.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    inner_start = _NOW - timedelta(days=72)
    inner_end = _NOW - timedelta(days=70)
    overhang_start = _NOW - timedelta(days=64)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(inner_start, inner_end, version="fv-salvage-inner"),
            # Runs 10 d past drop.end → clipped to [64 d, drop.end].
            _db_export_subject(
                overhang_start, _NOW - timedelta(days=50), version="fv-salvage-overhang"
            ),
        ]
    )
    drill = _drill_receipt(
        db_export_tuples=(
            _db_export_tuples(inner_start, _NOW - timedelta(days=70, hours=6))
            + _db_export_tuples(overhang_start, _NOW - timedelta(days=60, hours=6))
        )
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    # #1175: both targets fall short, and the early return surfaces the FIRST
    # in ascending order — the inner window [72 d, 70 d].
    assert reasons == [_db_export_missing(inner_start, inner_end)]


def test_drill_db_export_shortfall_at_clipped_window_start_refuses() -> None:
    """#1162 left-edge oracle: a 6 h db-export shortfall at the START of each
    salvage window (∩ drop window) refuses.

    Mirror of the right-edge case — one salvage window overhangs `drop.start`
    and one sits wholly inside, and the drill's db-export union begins 6 h too
    late for BOTH clipped starts. A clip that is loose on the left would admit
    this receipt.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    overhang_end = _NOW - timedelta(days=70)
    inner_start = _NOW - timedelta(days=66)
    inner_end = _NOW - timedelta(days=64)
    completeness = _completeness_receipt(
        subjects=[
            # Starts 6 d before drop.start → clipped to [drop.start, 70 d].
            _db_export_subject(
                _NOW - timedelta(days=80), overhang_end, version="fv-salvage-overhang"
            ),
            _db_export_subject(inner_start, inner_end, version="fv-salvage-inner"),
        ]
    )
    drill = _drill_receipt(
        db_export_tuples=(
            _db_export_tuples(_NOW - timedelta(days=73, hours=18), overhang_end)
            + _db_export_tuples(_NOW - timedelta(days=65, hours=18), inner_end)
        )
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    # #1175: the first target is the overhanging subject, and the suffix carries
    # its CLIPPED start (`drop.start`), not the raw 80 d subject start.
    assert reasons == [_db_export_missing(drop_start, overhang_end)]


def test_drill_db_export_zero_length_clipped_window_is_still_evaluated() -> None:
    """#1162: a salvage window whose end exactly TOUCHES `drop.start` clips to
    a zero-length target, and that target is still judged (tasks 1.1) — it is
    not silently skipped as "nothing to cover".

    The drill carries real db-export tuples that stop strictly before
    `drop.start`, so the refusal comes from the zero-length target genuinely
    not being covered, not from the empty-tuple path.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[_db_export_subject(_NOW - timedelta(days=81), drop_start)]
    )
    drill = _drill_receipt(
        db_export_tuples=_db_export_tuples(
            _NOW - timedelta(days=81), _NOW - timedelta(days=75)
        )
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    # #1175: the zero-length clip renders as the same instant twice.
    assert reasons == [_db_export_missing(drop_start, drop_start)]


def test_drill_db_export_zero_length_clipped_window_covered_admits() -> None:
    """#1162 admit half of the zero-length oracle: the same touching-endpoint
    subject ADMITS once the drill's db-export union actually contains that
    instant.

    Paired with the refuse case above this pins the fail-closed guard to
    `end < start` (genuinely inverted) rather than `end <= start`: a guard
    that also tripped on a zero-length target would refuse this receipt even
    though the required instant is covered, i.e. it would fail OPEN-closed on
    a live shape (a 7 d forcing_version window ending exactly on a chunk
    boundary).
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[_db_export_subject(_NOW - timedelta(days=81), drop_start)]
    )
    drill = _drill_receipt(
        db_export_tuples=_db_export_tuples(_NOW - timedelta(days=81), drop_start)
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    assert reasons == []


def test_drill_db_export_inverted_subject_window_refuses_fail_closed() -> None:
    """#1162: an INVERTED completeness subject window (`end` before `start`)
    clips to an inverted target, which is refused fail-closed.

    Without the guard the inverted interval is vacuously "covered" by any
    tuple straddling it — here a single 1 s db-export tuple — so a corrupt
    receipt would ADMIT the drop. Symmetric with the inverted-tuple defence
    in `_tuples_cover_window`.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    inverted_start = _NOW - timedelta(days=64)  # LATER than the window `end`
    inverted_end = _NOW - timedelta(days=70)
    completeness = _completeness_receipt(
        subjects=[_db_export_subject(inverted_start, inverted_end)]
    )
    drill = _drill_receipt(
        db_export_tuples=[
            {
                "source": "db-export",
                "window": {
                    "start": _iso(inverted_end),
                    "end": _iso(inverted_end + timedelta(seconds=1)),
                },
            }
        ]
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    # #1175: the inverted clip [64 d, 70 d] is rendered VERBATIM — a refusal
    # naming an interval whose end precedes its start is the diagnosis that the
    # completeness subject itself is corrupt.
    assert reasons == [_db_export_missing(inverted_start, inverted_end)]


def test_drill_db_export_inverted_target_after_a_covered_target_refuses() -> None:
    """#1162: the inverted-clip guard is applied to EVERY target, not just the
    first one the loop happens to visit.

    Targets are sorted ascending, so the valid subject ([72 d, 69 d], fully
    covered by the drill) is judged first and the corrupt inverted subject
    second. The drill's db-export union is deliberately chosen to straddle the
    inverted interval, so a guard that only defended the first target would
    hand `[64 d, 70 d]` to `_drill_covers`, get a vacuous True, and ADMIT the
    drop on a corrupt receipt.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    valid_start = _NOW - timedelta(days=72)
    valid_end = _NOW - timedelta(days=69)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(valid_start, valid_end, version="fv-salvage-valid"),
            # `start` is LATER than `end` → clips to an inverted target.
            _db_export_subject(
                _NOW - timedelta(days=64),
                _NOW - timedelta(days=70),
                version="fv-salvage-inverted",
            ),
        ]
    )
    # Union [72 d, 69 d] straddles the inverted interval [64 d, 70 d]:
    # union.start <= 64 d and union.end >= 70 d, so `_tuples_cover_window`
    # would vacuously accept it.
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(valid_start, valid_end))

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    # #1175: the first target is covered, so the suffix names the SECOND —
    # rendered inverted, exactly as the corrupt subject clips.
    assert reasons == [
        _db_export_missing(_NOW - timedelta(days=64), _NOW - timedelta(days=70))
    ]


def test_drill_db_export_salvage_subject_outside_drop_window_is_not_a_target() -> None:
    """#1162: only salvage-backed windows that OVERLAP the drop window become
    db-export targets.

    A long-past `coverage=db-export` subject ([200 d, 193 d]) sits far outside
    the [74 d, 60 d] drop window; the drill's db-export union covers only the
    in-window subject. Requiring drill coverage for out-of-window salvage
    subjects would refuse this admissible drop — the ancient subject clips to
    an inverted interval, which the fail-closed guard then rejects — so the
    gate would be unsatisfiable for any historical salvage era.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    in_window_start = _NOW - timedelta(days=70)
    in_window_end = _NOW - timedelta(days=66)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(
                _NOW - timedelta(days=200),
                _NOW - timedelta(days=193),
                version="fv-salvage-ancient",
            ),
            _db_export_subject(
                in_window_start, in_window_end, version="fv-salvage-in-window"
            ),
        ]
    )
    drill = _drill_receipt(
        db_export_tuples=_db_export_tuples(in_window_start, in_window_end)
    )

    reasons = _drill_gate_reasons(
        completeness, drill, retention.DropWindow(start=drop_start, end=drop_end)
    )

    assert reasons == []


# ---------------------------------------------------------------------------
# #1207 layer 1 — drill derivation-window guard. The drill records the drop
# window it was invoked with (`salvage_derivation.drop_window`, #1206); a
# drill that declared a NARROWER judgment span than the retention drop window
# cannot vouch for that drop, because its coverage tuples carry no subject
# identity (subject A's wide tuple would substitute for a never-derived,
# never-restore-verified subject B).
# ---------------------------------------------------------------------------


# Exact windows from the issue #1207 read-only probe.
_ISSUE_A_START = datetime(2026, 6, 14, tzinfo=UTC)
_ISSUE_A_END = datetime(2026, 6, 28, tzinfo=UTC)
_ISSUE_B_START = datetime(2026, 6, 20, tzinfo=UTC)
_ISSUE_B_END = datetime(2026, 6, 27, tzinfo=UTC)
_ISSUE_DRILL_START = datetime(2026, 6, 18, tzinfo=UTC)
_ISSUE_DRILL_END = datetime(2026, 6, 19, tzinfo=UTC)
_ISSUE_DROP_START = datetime(2026, 6, 18, tzinfo=UTC)
_ISSUE_DROP_END = datetime(2026, 6, 25, tzinfo=UTC)


def _salvage_derivation(window: Mapping[str, Any] | None) -> dict[str, Any]:
    """A schema-valid `salvage_derivation` section (#1206 drill emit shape).

    ``window`` is the recorded `--drop-window-*` interval, or ``None`` for a
    drill that ran un-narrowed (`oneOf: [window, null]` per
    `schemas/archive_rebuild_drill_receipt.schema.json`).
    """
    return {
        "completeness_receipt_path": "/home/nwm/audit-logs/completeness-receipt.json",
        "drop_window": dict(window) if window is not None else None,
        "candidate_count": 2,
        "derived_count": 1,
        "skipped": [],
    }


def _issue_1207_receipts(
    derivation: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the issue's A/B scenario.

    Completeness carries TWO db-export/complete subjects — A `[06-14, 06-28]`
    and B `[06-20, 06-27]` — and the drill carries ONLY A's full-window
    db-export tuples. Both subjects clip into the `[06-18, 06-25]` drop
    window, and A's tuples span both clipped targets, so the db-export leg is
    satisfied by evidence that never covered B. ``derivation=None`` builds the
    no-derivation-section receipt (the pre-guard shape).
    """
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(_ISSUE_A_START, _ISSUE_A_END, version="fv-salvage-a"),
            _db_export_subject(_ISSUE_B_START, _ISSUE_B_END, version="fv-salvage-b"),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(_ISSUE_A_START, _ISSUE_A_END))
    if derivation is not None:
        drill["salvage_derivation"] = dict(derivation)
    return completeness, drill


def _issue_1207_drop_window() -> retention.DropWindow:
    return retention.DropWindow(start=_ISSUE_DROP_START, end=_ISSUE_DROP_END)


def test_drill_derivation_window_narrower_than_drop_refuses_issue_1207_replay() -> None:
    """#1207 (a): the issue's A/B replay flips PASS → REFUSE.

    The drill declared it judged only `[06-18, 06-19]` while retention wants
    to drop `[06-18, 06-25]`; subject B `[06-20, 06-27]` was therefore never
    derived, never restore-verified, and only subject A's wide tuple vouches
    for it. The guard must fire FIRST — before any coverage-union evidence
    from that run is consulted.
    """
    completeness, drill = _issue_1207_receipts(
        _salvage_derivation({"start": _iso(_ISSUE_DRILL_START), "end": _iso(_ISSUE_DRILL_END)})
    )

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons[0] == retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW
    assert reasons == [retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW]


def test_drill_no_derivation_section_keeps_pre_guard_behavior() -> None:
    """#1207 (b) + pinned residual: the SAME A/B receipt without the
    `salvage_derivation` section still PASSES.

    Two receipt populations legitimately lack the section — receipts
    predating #1206 and today's explicit-manifest drills (`--salvage-manifest`
    without `--completeness-receipt`), which never write it. Their behavior is
    unchanged by design (D2), which also pins the residual: cross-subject
    tuple substitution survives on that path and is only closed by layer 2
    per-subject attribution. This assertion is simultaneously the pre-fix
    oracle — it is exactly the PASS the guard flips above.
    """
    completeness, drill = _issue_1207_receipts(None)
    assert "salvage_derivation" not in drill

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == []


def test_drill_derivation_window_null_passes_guard() -> None:
    """#1207 (c): `drop_window: null` = the drill ran un-narrowed → pass."""
    completeness, drill = _issue_1207_receipts(_salvage_derivation(None))

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == []


def test_drill_derivation_window_strictly_containing_passes_guard() -> None:
    """#1207 (d1): a drill window strictly wider on BOTH sides passes.

    The drill judged `[06-14, 06-28]` — a superset of the `[06-18, 06-25]`
    drop window — so both subjects were in its derivation set.
    """
    completeness, drill = _issue_1207_receipts(
        _salvage_derivation({"start": _iso(_ISSUE_A_START), "end": _iso(_ISSUE_A_END)})
    )

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == []


def test_drill_derivation_window_exactly_equal_passes_guard() -> None:
    """#1207 (d2) BOUNDARY: drill window EXACTLY EQUAL to the retention drop
    window passes.

    Runbook §7.5's standard invocation tells operators to paste the §7.3
    step-3 interval verbatim into `--drop-window-start/--drop-window-end`.
    That interval is a documented CONSERVATIVE SUPERSET of the runner's own
    drop window (the runner additionally intersects the eligible chunks with
    the completeness `coverage_bounds`), so the standard invocation records
    a window that is EQUAL to or WIDER than the retention drop window, never
    narrower — equality is the tight end of that range and must pass. A
    strict-inequality containment test would refuse it.
    """
    completeness, drill = _issue_1207_receipts(
        _salvage_derivation({"start": _iso(_ISSUE_DROP_START), "end": _iso(_ISSUE_DROP_END)})
    )

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == []


def test_drill_derivation_window_narrowed_only_at_start_refuses() -> None:
    """#1207 (d3) BOUNDARY: narrowing on the START side alone still refuses.

    The drill judged `[06-20, 06-28]` — its END covers the `[06-18, 06-25]`
    drop window completely, only its START is late. Subject A `[06-14,
    06-28]` overhangs the drop start and was still derived, so its tuples
    vouch for `[06-18, 06-20)` that this drill never judged.

    This row kills the start-side conjunct: a predicate that dropped
    `start <= drop_window.start` (checking only the end), or that compared
    the start against the wrong endpoint (`start <= drop_window.end`), would
    admit this receipt. The (a) replay above narrows BOTH sides and so
    survives either mutation.
    """
    completeness, drill = _issue_1207_receipts(
        _salvage_derivation({"start": _iso(_ISSUE_B_START), "end": _iso(_ISSUE_A_END)})
    )

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == [retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW]


@pytest.mark.parametrize(
    ("label", "section"),
    [
        ("not-a-mapping", ["not", "a", "mapping"]),
        (
            "drop-window-key-missing",
            {
                "completeness_receipt_path": "/home/nwm/audit-logs/completeness-receipt.json",
                "candidate_count": 2,
                "derived_count": 1,
                "skipped": [],
            },
        ),
        ("window-not-a-mapping", {"drop_window": "2026-06-18T00:00:00Z/2026-06-25T00:00:00Z"}),
        ("start-unparseable", {"drop_window": {"start": "not-a-timestamp", "end": "2026-06-25T00:00:00Z"}}),
        ("start-not-a-string", {"drop_window": {"start": 20260618, "end": "2026-06-25T00:00:00Z"}}),
        (
            "inverted",
            {"drop_window": {"start": "2026-06-25T00:00:00Z", "end": "2026-06-18T00:00:00Z"}},
        ),
    ],
)
def test_drill_derivation_section_unusable_shape_refuses(
    label: str, section: Any
) -> None:
    """#1207 (e): a `salvage_derivation` section that EXISTS but cannot be
    judged is never evidence — refuse fail-closed with the same code.

    Symmetric with the inverted-tuple defence in `_tuples_cover_window`.
    Most of these shapes are intercepted upstream by `load_drill_receipt`'s
    jsonschema validation (→ `DRILL_RECEIPT_MISSING`), so this is
    defence-in-depth at the pure-function seam; the INVERTED window is the
    one shape the schema cannot express, so it is the only row here that can
    reach the gate on the production path. It does not isolate the explicit
    `end < start` branch — containment already refuses any inverted window
    against a well-ordered drop window (see the branch comment in
    `_drill_derivation_window_contains`); the row pins the OUTCOME, not the
    branch that produces it.
    """
    completeness, drill = _issue_1207_receipts(None)
    drill["salvage_derivation"] = section

    reasons = _drill_gate_reasons(completeness, drill, _issue_1207_drop_window())

    assert reasons == [retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW], label


def test_drill_derivation_window_guard_surfaces_as_refusal_reason(tmp_path: Path) -> None:
    """#1207 (f) integration: the guard's code reaches the receipt surface.

    Drives the full runner (fake chunk/measure/drop seams) with a
    schema-valid narrowed drill receipt: the eligible chunk's range is the
    `[74 d, 60 d]` drop window while the drill recorded a 2 d judgment span
    inside it, so `run_retention` must publish `outcome=refused` with
    `refusal_reason = DRILL_DERIVATION_WINDOW_TOO_NARROW`.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[_db_export_subject(drop_start, drop_end, version="fv-salvage-wide")]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, drop_end))
    drill["salvage_derivation"] = _salvage_derivation(
        {
            "start": _iso(_NOW - timedelta(days=70)),
            "end": _iso(_NOW - timedelta(days=68)),
        }
    )
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-narrow-drill", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW
    assert receipt["refusal_reason"] in retention.WIRE_CODES
    jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# #1220 snapshot binding — the gate's REQUIREMENT set comes from the
# completeness receipt it loads NOW (rewritten in place daily), its EVIDENCE
# from a drill receipt up to 30 d old. A db-export subject added after the
# drill entered the requirement set never having been restore-verified, and
# an older subject's identity-less tuple vouched for it. The drill now
# records the db-export universe of the snapshot it consumed and the gate
# binds every target window to it.
# ---------------------------------------------------------------------------


# Windows from the issue's in-memory v1/v2 replay.
_SNAP_A_START = datetime(2026, 6, 1, tzinfo=UTC)
_SNAP_A_END = datetime(2026, 6, 30, tzinfo=UTC)
_SNAP_B_START = datetime(2026, 6, 10, tzinfo=UTC)
_SNAP_B_END = datetime(2026, 6, 20, tzinfo=UTC)
_SNAP_DROP_START = datetime(2026, 6, 5, tzinfo=UTC)
_SNAP_DROP_END = datetime(2026, 6, 15, tzinfo=UTC)
# A later db-export subject that does NOT overlap the drop window.
_SNAP_D_START = datetime(2026, 8, 1, tzinfo=UTC)
_SNAP_D_END = datetime(2026, 8, 10, tzinfo=UTC)

_UNSET = object()


def _win(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": _iso(start), "end": _iso(end)}


def _bound_salvage_derivation(
    *,
    drop_window: Mapping[str, Any] | None = None,
    db_export_windows: Any = _UNSET,
    completeness_generated_at: str | None = None,
) -> dict[str, Any]:
    """`salvage_derivation` with the #1220 snapshot-binding fields.

    Sibling of :func:`_salvage_derivation` (the #1206/#1207 shape). Passing
    ``db_export_windows=_UNSET`` reproduces the post-#1206 pre-binding
    population: the section exists, the recorded universe does not.
    """
    section = _salvage_derivation(drop_window)
    if db_export_windows is not _UNSET:
        section["db_export_windows"] = db_export_windows
    if completeness_generated_at is not None:
        section["completeness_generated_at"] = completeness_generated_at
    return section


def _snapshot_completeness(
    subjects: Sequence[tuple[datetime, datetime, str]],
    *,
    generated_at: datetime = _NOW - timedelta(hours=1),
) -> dict[str, Any]:
    return _completeness_receipt(
        generated_at=generated_at,
        subjects=[
            _db_export_subject(start, end, version=version)
            for start, end, version in subjects
        ],
    )


def _snapshot_drill(
    derivation: Mapping[str, Any] | None,
    *,
    runs_tuples: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A PASS drill whose db-export union spans subject A's FULL window.

    That union is exactly the substitution vehicle: it covers every clipped
    target in these fixtures, so without the binding guard the db-export leg
    returns empty reasons for a subject the drill never saw.
    """
    drill = _drill_receipt(
        db_export_tuples=_db_export_tuples(_SNAP_A_START, _SNAP_A_END),
        runs_tuples=runs_tuples,
    )
    if derivation is not None:
        drill["salvage_derivation"] = dict(derivation)
    return drill


def _snapshot_drop_window() -> retention.DropWindow:
    return retention.DropWindow(start=_SNAP_DROP_START, end=_SNAP_DROP_END)


def test_snapshot_drift_with_new_subject_refuses_issue_1220_replay() -> None:
    """#1220 (a): the issue's v1/v2 replay flips PASS → REFUSE.

    The drill consumed completeness v1 (subject A `[06-01, 06-30]` only) and
    recorded that universe; at gate time v2 additionally carries subject B
    `[06-10, 06-20]`, added by a backfill after the drill. B was never
    restore-verified, yet A's wide db-export tuple spans B's clipped target.
    """
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons[0] == retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND
    assert reasons == [retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND]


def test_snapshot_drift_without_recorded_universe_keeps_pre_fix_behavior() -> None:
    """#1220 (a) pre-fix oracle + residual pin: the SAME drift PASSES when the
    drill receipt has a `salvage_derivation` section but no
    `db_export_windows` field.

    That is the post-#1206 / pre-#1220 receipt population: the guard is
    dormant for them (design D5-(a)), and this empty reason list is exactly
    the false PASS the row above flips.
    """
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(_bound_salvage_derivation())
    assert "db_export_windows" not in drill["salvage_derivation"]

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


def test_snapshot_drift_without_derivation_section_keeps_pre_fix_behavior() -> None:
    """#1220 (a) compat front half: the same drift with NO `salvage_derivation`
    section at all still PASSES (pre-#1206 receipts and explicit-manifest
    drills — the population #1207's guard also skips)."""
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(None)
    assert "salvage_derivation" not in drill

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


def test_snapshot_identical_universe_passes() -> None:
    """#1220 (b): the drill recorded exactly the gate-time universe → pass."""
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[
                _win(_SNAP_A_START, _SNAP_A_END),
                _win(_SNAP_B_START, _SNAP_B_END),
            ],
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


def test_snapshot_requirement_shrink_passes() -> None:
    """#1220 (b): recorded `[A, B]`, gate-time only A → pass.

    Membership is a SUBSET test in the target → recorded direction: a subject
    that disappeared since the drill removes a requirement and can never make
    the drill's evidence insufficient. An implementation that compared the
    two sets for equality (or required recorded ⊆ targets) would refuse here.
    """
    completeness = _snapshot_completeness([(_SNAP_A_START, _SNAP_A_END, "fv-salvage-a")])
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[
                _win(_SNAP_A_START, _SNAP_A_END),
                _win(_SNAP_B_START, _SNAP_B_END),
            ],
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


def test_snapshot_daily_regeneration_alone_never_refuses() -> None:
    """#1220 (c) / design D1: a NEWER completeness snapshot with the SAME
    db-export windows passes.

    The audit rewrites the completeness receipt in place every day while the
    drill budget is 30 d, so at nearly every tick the loaded receipt is newer
    than the drill's. Refusing on `generated_at` inequality (or a digest)
    would refuse ~every tick after day one — `completeness_generated_at` is
    recorded for diagnostics only and is never a refusal input.
    """
    completeness = _snapshot_completeness(
        [(_SNAP_A_START, _SNAP_A_END, "fv-salvage-a")],
        generated_at=_NOW - timedelta(minutes=5),
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
            completeness_generated_at=_iso(_NOW - timedelta(days=20)),
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


def test_snapshot_new_subject_outside_drop_window_passes() -> None:
    """#1220 (d): binding is judged over the drop-filtered TARGETS only.

    The gate-time receipt gained subject D `[08-01, 08-10]`, disjoint from
    the `[06-05, 06-15]` drop window, so D is not a requirement of this tick.
    Binding the whole gate-time db-export universe instead would refuse
    whenever any db-export subject appears anywhere — the daily-outage
    direction design D1 rejects.
    """
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_D_START, _SNAP_D_END, "fv-salvage-d"),
        ]
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
        )
    )
    assert retention.derive_salvage_backed_windows(
        completeness, _snapshot_drop_window()
    ) == [_win(_SNAP_A_START, _SNAP_A_END)]

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == []


@pytest.mark.parametrize(
    ("label", "target"),
    [
        # A backfill EXTENDED subject A: same start, later end.
        ("shared-start-longer-end", (_SNAP_A_START, datetime(2026, 7, 15, tzinfo=UTC))),
        # Same end, earlier start.
        ("shared-end-earlier-start", (datetime(2026, 5, 20, tzinfo=UTC), _SNAP_A_END)),
    ],
)
def test_snapshot_one_sided_window_change_refuses(
    label: str, target: tuple[datetime, datetime]
) -> None:
    """#1220 (e): membership is exact on BOTH endpoints.

    A window sharing exactly one endpoint with a recorded window covers rows
    the drill never verified (the extension), so a start-only or end-only
    comparison would admit it.
    """
    completeness = _snapshot_completeness([(target[0], target[1], "fv-salvage-a")])
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == [retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND], label


def test_snapshot_empty_recorded_universe_refuses() -> None:
    """#1220 (f): `db_export_windows: []` with a non-empty requirement set
    refuses.

    An empty recorded universe has no members — the drill consumed a snapshot
    with no db-export/complete subject at all, so every gate-time target is
    drift. An `if not recorded: return True` short-circuit dies here.
    """
    completeness = _snapshot_completeness([(_SNAP_A_START, _SNAP_A_END, "fv-salvage-a")])
    drill = _snapshot_drill(_bound_salvage_derivation(db_export_windows=[]))

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == [retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND]


@pytest.mark.parametrize(
    ("label", "recorded"),
    [
        ("not-a-list", "2026-06-01T00:00:00Z/2026-06-30T00:00:00Z"),
        ("mapping-not-a-list", {"start": "2026-06-01T00:00:00Z", "end": "2026-06-30T00:00:00Z"}),
        ("entry-not-a-mapping", ["2026-06-01T00:00:00Z"]),
        ("entry-missing-end", [{"start": "2026-06-01T00:00:00Z"}]),
        ("entry-start-not-a-string", [{"start": 20260601, "end": "2026-06-30T00:00:00Z"}]),
    ],
)
def test_snapshot_unusable_recorded_universe_refuses(label: str, recorded: Any) -> None:
    """#1220 (g): a recorded universe that exists but cannot be judged is
    never evidence — refuse fail-closed.

    Unreachable through `load_drill_receipt` now that the schema types the
    field; this is defence-in-depth at the pure-function seam, symmetric with
    #1207's unusable-section rows.
    """
    completeness = _snapshot_completeness([(_SNAP_A_START, _SNAP_A_END, "fv-salvage-a")])
    drill = _snapshot_drill(_bound_salvage_derivation(db_export_windows=recorded))

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == [retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND], label


def test_snapshot_binds_helper_refuses_non_mapping_section() -> None:
    """Defence-in-depth: `_drill_snapshot_binds` on its own refuses a
    `salvage_derivation` that is not an object.

    Unreachable via `check_drill_gate` — #1207's guard refuses a non-Mapping
    section with `DRILL_DERIVATION_WINDOW_TOO_NARROW` before this helper runs
    (pinned by `test_drill_derivation_section_unusable_shape_refuses`) — so
    the branch is asserted at the helper seam instead of the gate seam.
    """
    assert (
        retention._drill_snapshot_binds(
            {"salvage_derivation": ["not", "a", "mapping"]},
            [_win(_SNAP_A_START, _SNAP_A_END)],
        )
        is False
    )


def test_snapshot_empty_derivation_still_refuses_db_export_missing_first() -> None:
    """#1220 (h) precedence: the empty-derivation refusal (#1162 D2) precedes
    the binding check.

    The completeness receipt has an overlapping db-export subject whose
    verdict is not `complete`, so the requirement set is EMPTY — there is
    nothing to bind, and the surfaced code must stay
    `DRILL_COVERAGE_DB_EXPORT_MISSING`.

    The recorded universe is deliberately UNUSABLE (`"not-a-list"`), which
    `_drill_snapshot_binds` refuses regardless of the target set: the binding
    guard would surface `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND` if it ran
    first, so the code below is a real ordering pin rather than a shape both
    legs happen to accept.

    Same function-level convention as
    `test_drill_db_export_empty_salvage_derivation_refuses_fail_closed`:
    `db-export` + `pending-archive` is rejected by the completeness receipt
    schema at load, so this shape is driven at the pure-function seam.
    """
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(
                _SNAP_A_START, _SNAP_A_END, version="fv-salvage-a", verdict="pending-archive"
            )
        ]
    )
    drill = _snapshot_drill(_bound_salvage_derivation(db_export_windows="not-a-list"))

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    # #1175: the D2 payload, not an interval — nothing was derivable to name.
    assert reasons == [_DB_EXPORT_MISSING_NO_DERIVABLE]


def test_snapshot_narrowed_drill_refuses_with_window_code_first() -> None:
    """#1220 (h) precedence + design D5-(d) tripwire: #1207's containment
    guard outranks the binding guard.

    This ordering is load-bearing, not cosmetic: the recorded universe is
    UNFILTERED, so "recorded ⇒ the drill actually derived it" only holds
    because containment already forced retention-drop ⊆ drill-drop. If this
    row ever starts reporting the binding code, #1207's guard was weakened or
    reordered and the binding guard silently degraded with it.
    """
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            drop_window=_win(_SNAP_DROP_START, datetime(2026, 6, 7, tzinfo=UTC)),
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
        )
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == [retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW]


def test_snapshot_missing_runs_coverage_refuses_before_binding() -> None:
    """#1220 (h) precedence: the runs leg outranks the binding guard — the
    binding check lives INSIDE the db-export leg, not at whole-gate level."""
    completeness = _snapshot_completeness(
        [
            (_SNAP_A_START, _SNAP_A_END, "fv-salvage-a"),
            (_SNAP_B_START, _SNAP_B_END, "fv-salvage-b"),
        ]
    )
    drill = _snapshot_drill(
        _bound_salvage_derivation(
            db_export_windows=[_win(_SNAP_A_START, _SNAP_A_END)],
        ),
        runs_tuples=[],
    )

    reasons = _drill_gate_reasons(completeness, drill, _snapshot_drop_window())

    assert reasons == [retention.CODE_DRILL_COVERAGE_RUNS_MISSING]


def test_snapshot_unbound_surfaces_as_refusal_reason(tmp_path: Path) -> None:
    """#1220 (i) integration: the guard's code reaches the receipt surface.

    Drives the full runner (fake chunk/measure/drop seams): the eligible
    chunk's range is the `[74 d, 60 d]` drop window, the drill recorded only
    subject A's window, and the gate-time completeness receipt carries a
    second db-export/complete subject inside that drop window.
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, drop_end, version="fv-salvage-a"),
            _db_export_subject(
                _NOW - timedelta(days=70), _NOW - timedelta(days=65), version="fv-salvage-b"
            ),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, drop_end))
    drill["salvage_derivation"] = _bound_salvage_derivation(
        db_export_windows=[_win(drop_start, drop_end)],
        completeness_generated_at=_iso(_NOW - timedelta(days=2)),
    )
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-unbound-drill", delta_days=60, duration_days=14
    )

    receipt = _run_dry(tmp_path, completeness, drill, chunk)

    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_DRILL_COMPLETENESS_SNAPSHOT_UNBOUND
    assert receipt["refusal_reason"] in retention.WIRE_CODES
    jsonschema.validate(receipt, _load_schema())


def test_enforced_receipt_echoes_unclipped_salvage_subject_windows(tmp_path: Path) -> None:
    """#1162 tasks 1.4: `salvage_backed_windows[]` echoes the RAW completeness
    subject windows; only the GATE clips them to the drop window.

    The subject overhangs the drop window on both sides ([80 d, 50 d] vs. a
    [74 d, 60 d] drop window), so a receipt that reported the clipped interval
    would be visibly different — and would misreport the operator's manual
    `COPY FROM` recovery scope (runbook §3.2) as narrower than it is.
    """
    subject_start = _NOW - timedelta(days=80)
    subject_end = _NOW - timedelta(days=50)
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    _write_json(
        tmp_path / "completeness.json",
        _completeness_receipt(subjects=[_db_export_subject(subject_start, subject_end)]),
    )
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, drop_end)),
    )
    config = _build_config(tmp_path, enforce=True)
    # A single 14 d chunk whose range is exactly the drop window.
    chunk = _chunk(
        "met", "forcing_station_timeseries", "chk-overhang", delta_days=60, duration_days=14
    )
    stub = _StubRunner([chunk])

    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )

    assert receipt["outcome"] == "enforced"
    assert receipt["salvage_backed_windows"] == [
        {"start": _iso(subject_start), "end": _iso(subject_end)}
    ]


# ---------------------------------------------------------------------------
# H2 UNION-of-tuples semantics (A2 — same-class:fake-oracle-in-tests fix
# for #854 R1). The drill emits per-cycle 24 h coverage tuples; the runner
# refuses only when the UNION does not cover the drop window.
# ---------------------------------------------------------------------------


def _daily_source_tuples(
    start: datetime, end: datetime, source: str
) -> list[dict[str, Any]]:
    """Local shim mirroring _daily_coverage_tuples for readability in tests."""
    return _daily_coverage_tuples(start, end, source)


def _drop_window(now: datetime, days: int) -> retention.DropWindow:
    return retention.DropWindow(start=now - timedelta(days=days), end=now)


def test_a2a_fourteen_daily_tuples_union_covers_drop_window() -> None:
    """A2-a: 14 per-cycle tuples spanning drop window → drill coverage PASSES."""
    now = _NOW
    drop = _drop_window(now, _DROP_WINDOW_DAYS)
    tuples = _daily_source_tuples(drop.start, drop.end, "forcing")
    assert len(tuples) == _DROP_WINDOW_DAYS
    assert retention._drill_covers(tuples, "forcing", drop) is True


def test_a2b_fourteen_daily_tuples_with_gap_union_fails() -> None:
    """A2-b: 14 per-cycle tuples with a 1-day gap in the middle → coverage FAILS."""
    now = _NOW
    drop = _drop_window(now, _DROP_WINDOW_DAYS)
    all_tuples = _daily_source_tuples(drop.start, drop.end, "forcing")
    # Remove the tuple covering day 7 → 8 to introduce a mid-window gap.
    gapped = [t for i, t in enumerate(all_tuples) if i != 7]
    assert len(gapped) == _DROP_WINDOW_DAYS - 1
    assert retention._drill_covers(gapped, "forcing", drop) is False


def test_a2c_two_overlapping_tuples_union_covers() -> None:
    """A2-c: 2 overlapping tuples whose union covers the drop window → PASS."""
    now = _NOW
    drop = _drop_window(now, _DROP_WINDOW_DAYS)
    tuples = [
        {
            "source": "forcing",
            "window": {
                "start": _iso(drop.start),
                "end": _iso(drop.start + timedelta(days=9)),
            },
        },
        {
            "source": "forcing",
            "window": {
                "start": _iso(drop.start + timedelta(days=7)),
                "end": _iso(drop.end),
            },
        },
    ]
    assert retention._drill_covers(tuples, "forcing", drop) is True


def test_a2d_single_tuple_covering_last_day_fails() -> None:
    """A2-d: single per-cycle tuple covering only last day of drop window → FAIL."""
    now = _NOW
    drop = _drop_window(now, _DROP_WINDOW_DAYS)
    tuples = [
        {
            "source": "forcing",
            "window": {
                "start": _iso(drop.end - timedelta(days=1)),
                "end": _iso(drop.end),
            },
        },
    ]
    assert retention._drill_covers(tuples, "forcing", drop) is False


def test_a2e_real_shape_uses_drill_identity_window(tmp_path: Path) -> None:
    """A2-e (real-shape integration): craft coverage tuples from N synthetic
    cycle times via the drill module's ``_identity_window`` emit shape.

    This closes the same-class:fake-oracle-in-tests gap from #854 R1: unit
    tests exercise the exact tuple shape the drill produces per cycle,
    not a synthetic single-tuple stand-in.
    """
    from scripts.node27_archive_rebuild_drill import _identity_window as drill_identity_window

    # Build 14 synthetic per-cycle manifests, each with a 24 h producer
    # window matching the drill's real shape. Union must cover the 14-day
    # drop window.
    now = _NOW
    drop = _drop_window(now, _DROP_WINDOW_DAYS)
    cycle_tuples: list[dict[str, Any]] = []
    cursor = drop.start
    while cursor < drop.end:
        cycle_end = min(cursor + timedelta(days=1), drop.end)
        # Fabricate a manifest with the same producer-time shape the drill
        # would emit; delegate window derivation to the drill module.
        manifest = {
            "producer": {"start_time": _iso(cursor), "end_time": _iso(cycle_end)},
            "identity": {"cycle_time": _iso(cursor)},
        }
        window = drill_identity_window(manifest)
        cycle_tuples.append({"source": "runs", "window": window})
        cursor = cycle_end
    assert len(cycle_tuples) == _DROP_WINDOW_DAYS
    # Real drill emit shape → union covers → drill_covers PASSES.
    assert retention._drill_covers(cycle_tuples, "runs", drop) is True
    # Sanity: remove a middle cycle to introduce a gap → FAIL.
    gapped = [t for i, t in enumerate(cycle_tuples) if i != 7]
    assert retention._drill_covers(gapped, "runs", drop) is False


def test_a2_full_runner_accepts_union_covering_per_cycle_drill_receipt(
    tmp_path: Path,
) -> None:
    """A2 end-to-end: full runner accepts a drill receipt whose forcing/runs
    coverage is per-cycle daily tuples (real drill emit shape) — not a
    single synthetic tuple. This is the pattern-level closure for
    #854 R1 (fake-oracle-in-tests): if the drill receipt is realistic,
    the runner must still accept it.
    """
    now = _NOW
    # Chunks: 60 days back, 7 days duration → drop window ≈ [now-67d, now-60d].
    chunks = [_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)]
    # Drill emits ~7 daily forcing + 7 daily runs tuples covering the drop
    # window plus a small safety margin (mirrors production drill cadence).
    forcing_tuples = _daily_source_tuples(
        now - timedelta(days=70), now - timedelta(days=58), "forcing"
    )
    runs_tuples = _daily_source_tuples(
        now - timedelta(days=70), now - timedelta(days=58), "runs"
    )
    assert len(forcing_tuples) == 12
    assert len(runs_tuples) == 12
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(forcing_tuples=forcing_tuples, runs_tuples=runs_tuples),
    )
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, now, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced", receipt


def test_a2_full_runner_refuses_when_per_cycle_forcing_tuples_have_gap(
    tmp_path: Path,
) -> None:
    """A2 end-to-end refusal: per-cycle drill receipt with a mid-drop-window
    forcing gap → DRILL_COVERAGE_FORCING_MISSING (union does NOT cover).
    """
    now = _NOW
    chunks = [_chunk("hydro", "river_timeseries", "chk-a", delta_days=60, duration_days=7)]
    forcing_all = _daily_source_tuples(
        now - timedelta(days=70), now - timedelta(days=58), "forcing"
    )
    # Drop the tuple sitting inside the drop window ([now-67d, now-60d]).
    forcing_gapped = [
        t
        for t in forcing_all
        if not (
            _iso(now - timedelta(days=65)) <= t["window"]["start"] < _iso(now - timedelta(days=63))
        )
    ]
    assert len(forcing_gapped) < len(forcing_all)
    runs_tuples = _daily_source_tuples(
        now - timedelta(days=70), now - timedelta(days=58), "runs"
    )
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(forcing_tuples=forcing_gapped, runs_tuples=runs_tuples),
    )
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, now, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_DRILL_COVERAGE_FORCING_MISSING


# ---------------------------------------------------------------------------
# H3 per-tick bound + deferred_remainder (spec §6.1 row 3)
# ---------------------------------------------------------------------------


def test_per_tick_bound_selects_at_most_bound_and_defers_remainder(
    tmp_path: Path,
) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
# H8 freshness gates at boundary + past
# ---------------------------------------------------------------------------


def test_completeness_freshness_at_boundary_passes(tmp_path: Path) -> None:
    # generated_at exactly at the age-limit boundary — must still pass.
    generated_at = _NOW - timedelta(hours=26)
    _write_json(tmp_path / "completeness.json", _completeness_receipt(generated_at=generated_at))
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path, enforce=True)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"


def test_completeness_freshness_past_boundary_refuses(tmp_path: Path) -> None:
    generated_at = _NOW - timedelta(hours=27)
    _write_json(tmp_path / "completeness.json", _completeness_receipt(generated_at=generated_at))
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_STALE


def test_drill_freshness_at_boundary_passes(tmp_path: Path) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(generated_at=_NOW - timedelta(days=30)),
    )
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"


def test_drill_freshness_past_boundary_refuses(tmp_path: Path) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(generated_at=_NOW - timedelta(days=31)),
    )
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_RECEIPT_STALE


# ---------------------------------------------------------------------------
# H9 salvage_backed_windows derivation
# ---------------------------------------------------------------------------


def test_salvage_backed_windows_derived_from_completeness_db_export(
    tmp_path: Path,
) -> None:
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-a"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=90)),
                    "end": _iso(_NOW - timedelta(days=85)),
                },
                "coverage": "db-export",
                "verdict": "complete",
            },
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-b"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=90)),
                    "end": _iso(_NOW - timedelta(days=85)),
                },
                "coverage": "db-export",
                "verdict": "complete",
            },
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-c"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=80)),
                    "end": _iso(_NOW - timedelta(days=75)),
                },
                "coverage": "db-export",
                "verdict": "complete",
            },
        ]
    )
    _write_json(tmp_path / "completeness.json", completeness)
    drill = _drill_receipt(
        db_export_window=(_NOW - timedelta(days=95), _NOW - timedelta(days=70)),
    )
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path, enforce=True)
    # Two chunks, one covering days 90-83 and another covering days 80-73,
    # so the drop window spans day 90 through day 73 and overlaps both
    # completeness subject windows.
    chunks = [
        _chunk("hydro", "river_timeseries", "chk-a", delta_days=83, duration_days=7),
        _chunk("hydro", "river_timeseries", "chk-b", delta_days=73, duration_days=7),
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "enforced"
    windows = receipt["salvage_backed_windows"]
    # Deduped (fv-a and fv-b share the same window) and sorted ascending.
    assert windows == [
        {
            "start": _iso(_NOW - timedelta(days=90)),
            "end": _iso(_NOW - timedelta(days=85)),
        },
        {
            "start": _iso(_NOW - timedelta(days=80)),
            "end": _iso(_NOW - timedelta(days=75)),
        },
    ]


def test_salvage_backed_windows_empty_without_db_export_subject(tmp_path: Path) -> None:
    """H9: no db-export subject → empty array (schema-conformant)."""
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["salvage_backed_windows"] == []


# ---------------------------------------------------------------------------
# H17 zero-eligible enforce
# ---------------------------------------------------------------------------


def test_zero_eligible_enforce_produces_empty_enforced_receipt(tmp_path: Path) -> None:
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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


def test_dry_run_evaluates_gates_before_dryrun_branch(tmp_path: Path) -> None:
    """Behavior lock for runbook §8.5 claim: gates ARE evaluated in dry-run.

    Same-class:byte-identity-drift closure (R2 fix, mirrors #854 R2 discipline
    extension). Runbook §8.5 states: "Gates ARE evaluated in dry-run mode —
    a dry-run invocation that would refuse still emits a `refused` receipt
    (`mode=enforce` per the schema `oneOf`) so operators see the exact
    refusal reason before ever running enforce."

    A refactor moving gate checks after the dry-run branch would produce a
    dry-run receipt for a stale completeness input, silently invalidating
    the §8.5 claim. This test locks the order: with a stale completeness
    receipt (age > default 26 h) AND ``enforce=False``, the runner MUST
    surface the completeness-stale refusal (mode=enforce per schema oneOf),
    NOT emit a dry-run receipt.
    """
    stale_completeness = _completeness_receipt(generated_at=_NOW - timedelta(hours=27))
    _write_json(tmp_path / "completeness.json", stale_completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    # enforce=False → dry-run branch would fire if gates were skipped.
    config = _build_config(tmp_path, enforce=False)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    # Gate refusal fires BEFORE the dry-run branch — no candidate_chunks
    # emitted; refused receipt carries mode=enforce per schema oneOf pin.
    assert receipt["outcome"] == "refused"
    assert receipt["mode"] == "enforce"
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_STALE
    # Dry-run never calls drop (baseline invariant preserved).
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


def test_load_completeness_receipt_rejects_malformed_subject_window(tmp_path: Path) -> None:
    """RF-F1 R2 fix: loader ENFORCES format:date-time via _RECEIPT_FORMAT_CHECKER.

    Silent-False fallback in _subject_overlaps_drop would skip a gap subject
    with malformed window; loader-side FormatChecker refuses the receipt
    entirely so a bad-shape completeness receipt cannot masquerade as
    "no in-window subjects" and quietly reach the drop phase.

    Deviation record: schema violation is surfaced via existing
    CODE_COMPLETENESS_RECEIPT_MISSING code (no new SCHEMA_INVALID wire
    code) — matches the pre-existing loader contract that groups
    "receipt is not usable" causes under the missing code.
    """
    receipt = _completeness_receipt()
    receipt["windows"][0]["window"]["start"] = "not-a-datetime"
    completeness_path = tmp_path / "completeness.json"
    _write_json(completeness_path, receipt)
    with pytest.raises(retention.ReceiptGateError) as excinfo:
        retention.load_completeness_receipt(completeness_path)
    assert excinfo.value.code == retention.CODE_COMPLETENESS_RECEIPT_MISSING


def test_load_completeness_receipt_rejects_malformed_generated_at(tmp_path: Path) -> None:
    """RF-F1 R2 fix (symmetric): malformed top-level generated_at is refused
    at load. Without FormatChecker, jsonschema would treat ``format`` as
    informational and let the loader return a receipt with an unparseable
    timestamp, which would then be caught as STALE downstream — the wrong
    wire code for a shape defect.
    """
    receipt = _completeness_receipt()
    receipt["generated_at"] = "not-a-datetime"
    completeness_path = tmp_path / "completeness.json"
    _write_json(completeness_path, receipt)
    with pytest.raises(retention.ReceiptGateError) as excinfo:
        retention.load_completeness_receipt(completeness_path)
    assert excinfo.value.code == retention.CODE_COMPLETENESS_RECEIPT_MISSING


def test_load_drill_receipt_rejects_malformed_coverage_window(tmp_path: Path) -> None:
    """RF-F1 R2 fix (drill mirror): loader ENFORCES format:date-time on
    drill coverage tuples via _RECEIPT_FORMAT_CHECKER. Silent-False
    fallback in _tuples_cover_window would drop the malformed tuple and
    could silently emit a spurious DRILL_COVERAGE_<source>_MISSING (or
    worse, pass a UNION check that no longer reflects the receipt shape).
    """
    drill = _drill_receipt()
    # First coverage tuple → malformed start.
    assert drill["coverage"], "drill fixture must ship coverage tuples"
    drill["coverage"][0]["window"]["start"] = "not-a-datetime"
    drill_path = tmp_path / "drill.json"
    _write_json(drill_path, drill)
    with pytest.raises(retention.ReceiptGateError) as excinfo:
        retention.load_drill_receipt(drill_path)
    assert excinfo.value.code == retention.CODE_DRILL_RECEIPT_MISSING


def test_load_drill_receipt_rejects_malformed_generated_at(tmp_path: Path) -> None:
    """RF-F1 R2 fix (drill mirror, symmetric to completeness generated_at)."""
    drill = _drill_receipt()
    drill["generated_at"] = "not-a-datetime"
    drill_path = tmp_path / "drill.json"
    _write_json(drill_path, drill)
    with pytest.raises(retention.ReceiptGateError) as excinfo:
        retention.load_drill_receipt(drill_path)
    assert excinfo.value.code == retention.CODE_DRILL_RECEIPT_MISSING


# ---------------------------------------------------------------------------
# F1-fix — negative-age freshness guard (defensive against clock skew)
# ---------------------------------------------------------------------------


def test_completeness_receipt_future_dated_refuses_with_stale(tmp_path: Path) -> None:
    """F1-fix: a completeness receipt whose ``generated_at`` is IN THE
    FUTURE (clock skew or misconfigured emitter) MUST NOT be treated as
    fresh. Reuse STALE per H8 discipline (no new wire code).
    """
    future_completeness = _completeness_receipt(generated_at=_NOW + timedelta(minutes=5))
    _write_json(tmp_path / "completeness.json", future_completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_STALE


def test_drill_receipt_future_dated_refuses_with_stale(tmp_path: Path) -> None:
    """F1-fix: drill receipt future-dated → STALE (symmetric with completeness)."""
    future_drill = _drill_receipt(generated_at=_NOW + timedelta(minutes=5))
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", future_drill)
    config = _build_config(tmp_path, enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)])
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["outcome"] == "refused"
    assert receipt["refusal_reason"] == retention.CODE_DRILL_RECEIPT_STALE


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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS",
        "NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS",
        "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_LOCK_PATH",
        "NODE27_TIMESERIES_RETENTION_ENFORCE",
        "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE",
    ):
        assert re.search(rf"^#?{re.escape(key)}=", text, flags=re.MULTILINE), f"missing {key}"


# ---------------------------------------------------------------------------
# Refusal priority — completeness bounds before drill missing (spot-check).
# ---------------------------------------------------------------------------


def test_completeness_bounds_refuses_before_drill_missing(tmp_path: Path) -> None:
    """Refusal-order pin from brief: completeness bounds → gap → pending → drill missing → …

    A missing drill receipt + insufficient completeness bounds MUST surface
    the completeness bounds code (higher priority), not the drill missing.
    """
    completeness = _completeness_receipt(
        bounds_start=_NOW - timedelta(days=40),
        bounds_end=_NOW - timedelta(days=32),
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    config.drill_receipt_path.unlink()  # drill missing
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=80)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT


# ---------------------------------------------------------------------------
# E1-fix — additional refusal precedence pairs (runbook §8.2 priority chain).
# ---------------------------------------------------------------------------


def test_completeness_stale_refuses_before_drill_missing(tmp_path: Path) -> None:
    """STALE > DRILL_MISSING per §8.2 chain."""
    stale = _completeness_receipt(generated_at=_NOW - timedelta(hours=27))
    _write_json(tmp_path / "completeness.json", stale)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    config = _build_config(tmp_path)
    config.drill_receipt_path.unlink()  # drill missing
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=60)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_STALE


def test_completeness_gap_refuses_before_drill_stale(tmp_path: Path) -> None:
    """GAP > DRILL_STALE per §8.2 chain."""
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "runs",
                "subject": {"run_id": "run-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=63)),
                },
                "coverage": "none",
                "verdict": "gap",
            }
        ]
    )
    stale_drill = _drill_receipt(generated_at=_NOW - timedelta(days=45))
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", stale_drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW


def test_completeness_pending_refuses_before_drill_fail(tmp_path: Path) -> None:
    """PENDING > DRILL_FAIL per §8.2 chain."""
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=63)),
                },
                "coverage": "hot-object-store",
                "verdict": "pending-archive",
            }
        ]
    )
    failed_drill = _drill_receipt(verdict="FAIL")
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", failed_drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW


def test_completeness_bounds_refuses_before_drill_coverage_missing(tmp_path: Path) -> None:
    """BOUNDS > DRILL_COVERAGE_* per §8.2 chain."""
    completeness = _completeness_receipt(
        bounds_start=_NOW - timedelta(days=40),
        bounds_end=_NOW - timedelta(days=32),
    )
    # Drill missing forcing coverage entirely — lower-priority code.
    drill_missing_forcing = _drill_receipt(forcing_tuples=[])
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", drill_missing_forcing)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=80)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT


def test_drill_derivation_window_refuses_before_drill_coverage_missing(tmp_path: Path) -> None:
    """DRILL_DERIVATION_WINDOW_TOO_NARROW > DRILL_COVERAGE_* per §8.2 chain (#1207).

    The drill is BOTH narrowed (`salvage_derivation.drop_window` sits strictly
    inside the `[72 d, 65 d]` drop window) AND missing its forcing coverage
    leg entirely. The derivation-window guard outranks every coverage leg, so
    no coverage-union evidence from a run that never judged this span is
    consulted — the surfaced code is the window code, not FORCING_MISSING.
    """
    narrowed_drill = _drill_receipt(forcing_tuples=[])
    narrowed_drill["salvage_derivation"] = _salvage_derivation(
        {
            "start": _iso(_NOW - timedelta(days=70)),
            "end": _iso(_NOW - timedelta(days=67)),
        }
    )
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", narrowed_drill)
    config = _build_config(tmp_path)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=65)]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW


def test_drill_derivation_window_refuses_before_snapshot_unbound(tmp_path: Path) -> None:
    """DRILL_DERIVATION_WINDOW_TOO_NARROW > DRILL_COMPLETENESS_SNAPSHOT_UNBOUND
    per §8.2 chain (#1207 before #1220), at the receipt surface.

    The drill is BOTH narrowed (`salvage_derivation.drop_window` sits strictly
    inside the `[74 d, 60 d]` drop window) AND unbound (the gate-time
    completeness receipt gained a db-export/complete subject the drill's
    recorded universe never contained). The containment guard wins — and it
    has to: the recorded universe is unfiltered, so binding only implies
    "restore-verified" once containment holds (design D5-(d)).
    """
    drop_start = _NOW - timedelta(days=74)
    drop_end = _NOW - timedelta(days=60)
    completeness = _completeness_receipt(
        subjects=[
            _db_export_subject(drop_start, drop_end, version="fv-salvage-a"),
            _db_export_subject(
                _NOW - timedelta(days=70), _NOW - timedelta(days=65), version="fv-salvage-b"
            ),
        ]
    )
    drill = _drill_receipt(db_export_tuples=_db_export_tuples(drop_start, drop_end))
    drill["salvage_derivation"] = _bound_salvage_derivation(
        drop_window=_win(_NOW - timedelta(days=70), _NOW - timedelta(days=68)),
        db_export_windows=[_win(drop_start, drop_end)],
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", drill)
    config = _build_config(tmp_path)
    chunks = [
        _chunk(
            "met", "forcing_station_timeseries", "chk-narrow-unbound", delta_days=60, duration_days=14
        )
    ]
    stub = _StubRunner(chunks)
    receipt = retention.run_retention(
        config, _NOW, fetch_chunks=stub.fetch, measure_chunk_bytes=stub.measure, drop_chunk=stub.drop
    )
    assert receipt["refusal_reason"] == retention.CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW


# ---------------------------------------------------------------------------
# G F2 — H7 straddling chunk: range_start < cutoff < range_end NOT included.
# G F3 — Mixed-compressed: eligible list contains compressed + uncompressed.
# G Schema C1 — jsonschema validation on 8 uncovered receipt shapes.
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


def test_retention_cutoff_uses_display_watermark_but_gate_freshness_uses_wall_clock(
    tmp_path: Path,
) -> None:
    wall_time = datetime(2026, 7, 22, 0, tzinfo=UTC)
    reference_time = datetime(2026, 7, 11, 12, tzinfo=UTC)
    _write_json(
        tmp_path / "completeness.json",
        _completeness_receipt(
            generated_at=wall_time - timedelta(hours=1),
            bounds_start=reference_time - timedelta(days=365),
            bounds_end=reference_time,
        ),
    )
    _write_json(
        tmp_path / "drill.json",
        _drill_receipt(generated_at=wall_time - timedelta(days=1)),
    )
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
    _write_json(tmp_path / "completeness.json", _completeness_receipt())
    _write_json(tmp_path / "drill.json", _drill_receipt())
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
        retention.CODE_COMPLETENESS_RECEIPT_MISSING,
        retention.CODE_COMPLETENESS_RECEIPT_STALE,
        retention.CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT,
        retention.CODE_COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW,
        retention.CODE_COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW,
        retention.CODE_DRILL_RECEIPT_MISSING,
        retention.CODE_DRILL_RECEIPT_STALE,
        retention.CODE_DRILL_RECEIPT_FAIL,
    ],
)
def test_refused_receipt_shape_validates_against_schema(wire_code: str) -> None:
    """G Schema C1: refused receipt (one of 8 wire codes) is schema-conformant."""
    receipt = retention.build_receipt("refused", _NOW, refusal_reason=wire_code)
    jsonschema.validate(receipt, _load_schema())


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
        "archive_gate": {"mode": "enabled"},
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
        "archive_gate": {"mode": "enabled"},
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        retention._validate_receipt(bad_receipt)


# ---------------------------------------------------------------------------
# #1369 — explicit archive-gate disabled mode (ADR 0002 Revision 2026-08-11)
#
# Three orthogonal axes: gate mode (enabled/disabled) x dry-run/enforce x
# outcome branch. The pins below are written against the fixture's own
# strings (env key, enum values, ADR reference constant), never echoed from
# the module, so a rename in the runner cannot silently drag the oracle
# along.
# ---------------------------------------------------------------------------


_ARCHIVE_GATE_ENV_KEY = "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE"
# Verbatim from docs/adr/0002-node27-timeseries-hot-cold-tiering.md
# "Revision 2026-08-11 — archive lane retired; delete-without-archive authorized".
_ADR_REFERENCE = "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"
_ADR_PATH_ANCHOR = "docs/adr/0002-node27-timeseries-hot-cold-tiering.md"
_ADR_REVISION_ANCHOR = "Revision 2026-08-11"

# The thirteen archive-family wire codes. In disabled mode none of them is
# reachable; the four runner-own codes remain reachable.
_ARCHIVE_FAMILY_CODES = frozenset(
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
_RUNNER_OWN_CODES = frozenset(
    {
        "RETENTION_CONFIG_INVALID",
        "RETENTION_CONCURRENT_INVOCATION",
        "RETENTION_DROP_FAILED",
        "RETENTION_UNCAUGHT_ERROR",
    }
)


def test_archive_family_partition_covers_wire_codes_exactly() -> None:
    """The 13/4 split is a partition of WIRE_CODES — no code escapes the
    reachability matrix below by being absent from both halves.
    """
    assert len(_ARCHIVE_FAMILY_CODES) == 13
    assert len(_RUNNER_OWN_CODES) == 4
    assert _ARCHIVE_FAMILY_CODES.isdisjoint(_RUNNER_OWN_CODES)
    assert _ARCHIVE_FAMILY_CODES | _RUNNER_OWN_CODES == retention.WIRE_CODES


def _disabled_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """Env for disabled mode with BOTH archive receipt path vars absent."""
    base: dict[str, str | None] = {
        _ARCHIVE_GATE_ENV_KEY: "disabled",
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH": None,
        "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH": None,
    }
    base.update(overrides)
    return _base_env(tmp_path, **base)


# --- tasks 2.1: D1 mode parse table ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, "enabled", id="unset-defaults-enabled"),
        pytest.param("enabled", "enabled", id="enabled"),
        pytest.param("disabled", "disabled", id="disabled"),
        pytest.param("  disabled  ", "disabled", id="disabled-surrounding-spaces"),
        pytest.param("\tenabled\n", "enabled", id="enabled-surrounding-tabs"),
        pytest.param("DISABLED", "disabled", id="disabled-uppercase"),
        pytest.param("Enabled", "enabled", id="enabled-mixed-case"),
    ],
)
def test_archive_gate_env_parse_table(tmp_path: Path, raw: str | None, expected: str) -> None:
    """D1: unset → enabled; strip+lower must land inside the enum."""
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: raw})
    config = retention.config_from_args(_args(), env)
    assert config.archive_gate == expected


@pytest.mark.parametrize(
    "raw",
    [
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
def test_archive_gate_invalid_env_value_fails_closed(tmp_path: Path, raw: str) -> None:
    """D1: three-value risk switch — anything outside the enum must explode.

    No truthiness fallback (that is the ``--enforce`` precedent and it is
    deliberately NOT reused here).
    """
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: raw})
    with pytest.raises(retention.RetentionConfigError, match=_ARCHIVE_GATE_ENV_KEY):
        retention.config_from_args(_args(), env)


def test_main_invalid_archive_gate_exits_two_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D1 end-to-end: exit 2, RETENTION_CONFIG_INVALID on stderr, NO receipt."""
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: "disable"})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    receipt_path = Path(env["NODE27_TIMESERIES_RETENTION_RECEIPT_PATH"])

    code = retention.main(argv=[], now=_NOW)

    assert code == 2
    assert not receipt_path.exists()
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["code"] == retention.CODE_RETENTION_CONFIG_INVALID
    assert _ARCHIVE_GATE_ENV_KEY in payload["reason"]


@pytest.mark.parametrize(
    ("env_value", "cli_value", "expected"),
    [
        pytest.param("enabled", "disabled", "disabled", id="cli-disabled-beats-env-enabled"),
        pytest.param("disabled", "enabled", "enabled", id="cli-enabled-beats-env-disabled"),
        pytest.param(None, "disabled", "disabled", id="cli-disabled-without-env"),
    ],
)
def test_archive_gate_cli_overrides_env(
    tmp_path: Path, env_value: str | None, cli_value: str, expected: str
) -> None:
    """CLI wins over env, matching the ``--enforce`` precedent."""
    env = _base_env(tmp_path, **{_ARCHIVE_GATE_ENV_KEY: env_value})
    config = retention.config_from_args(_args(archive_gate=cli_value), env)
    assert config.archive_gate == expected


def test_parser_archive_gate_choices_are_the_enum() -> None:
    parser = retention._parser()
    assert parser.parse_args([]).archive_gate is None
    assert parser.parse_args(["--archive-gate", "disabled"]).archive_gate == "disabled"
    assert parser.parse_args(["--archive-gate", "enabled"]).archive_gate == "enabled"
    with pytest.raises(SystemExit):
        parser.parse_args(["--archive-gate", "disable"])


# --- tasks 2.2: D4 conditional path requirement (both directions) ---------


@pytest.mark.parametrize(
    "missing_key",
    [
        "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH",
        "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH",
    ],
)
@pytest.mark.parametrize(
    "gate", [pytest.param(None, id="gate-unset"), pytest.param("enabled", id="gate-enabled")]
)
def test_archive_gate_enabled_still_requires_both_archive_paths(
    tmp_path: Path, missing_key: str, gate: str | None
) -> None:
    """D4 direction 1: the enabled side is NOT relaxed by this change."""
    env = _base_env(tmp_path, **{missing_key: None, _ARCHIVE_GATE_ENV_KEY: gate})
    with pytest.raises(retention.RetentionConfigError, match=missing_key):
        retention.config_from_args(_args(), env)


def test_archive_gate_disabled_makes_both_archive_paths_optional(tmp_path: Path) -> None:
    """D4 direction 2: disabled + both path vars absent parses to ``None``.

    ``_resolve_path(..., required=False)`` raises by construction, so the
    only correct implementation is to NOT resolve these two paths at all.
    """
    config = retention.config_from_args(_args(), _disabled_env(tmp_path))
    assert config.archive_gate == "disabled"
    assert config.completeness_receipt_path is None
    assert config.drill_receipt_path is None


def test_archive_gate_disabled_ignores_paths_pointing_at_missing_files(
    tmp_path: Path,
) -> None:
    """Given-but-unread proof: the two env vars name files that do not exist
    and the tick still succeeds (an enabled-mode tick would refuse with
    ``COMPLETENESS_RECEIPT_MISSING``).
    """
    ghost_completeness = tmp_path / "ghost" / "completeness.json"
    ghost_drill = tmp_path / "ghost" / "drill.json"
    assert not ghost_completeness.exists() and not ghost_drill.exists()
    env = _base_env(
        tmp_path,
        **{
            _ARCHIVE_GATE_ENV_KEY: "disabled",
            "NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH": str(ghost_completeness),
            "NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH": str(ghost_drill),
        },
    )
    config = retention.config_from_args(_args(), env)
    assert config.completeness_receipt_path is None
    assert config.drill_receipt_path is None

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


def test_run_retention_disabled_never_reads_a_configured_receipt_path(
    tmp_path: Path,
) -> None:
    """Even when the config object still carries archive paths (hand-built or
    left over), disabled mode must not open them.
    """
    config = _build_config(
        tmp_path,
        archive_gate="disabled",
        completeness_receipt_path=tmp_path / "does-not-exist-completeness.json",
        drill_receipt_path=tmp_path / "does-not-exist-drill.json",
    )
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "dry-run"


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
    config = _build_config(
        tmp_path,
        archive_gate="disabled",
        enforce=True,
        completeness_receipt_path=None,
        drill_receipt_path=None,
    )
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


def test_disabled_enforce_salvage_windows_empty_even_with_db_export_subjects(
    tmp_path: Path,
) -> None:
    """The completeness receipt on disk carries db-export subjects that WOULD
    derive salvage-backed windows in enabled mode; disabled mode never looks.
    """
    completeness = _completeness_receipt(
        subjects=[
            {
                "lane": "forcing",
                "subject": {"forcing_version_id": "fv-1"},
                "window": {
                    "start": _iso(_NOW - timedelta(days=70)),
                    "end": _iso(_NOW - timedelta(days=50)),
                },
                "coverage": "db-export",
                "verdict": "complete",
            }
        ]
    )
    _write_json(tmp_path / "completeness.json", completeness)
    config = _build_config(tmp_path, archive_gate="disabled", enforce=True)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == "enforced"
    assert receipt["salvage_backed_windows"] == []


def test_boundary_partial_chunk_becomes_candidate_when_gate_disabled(
    tmp_path: Path,
) -> None:
    """Documented semantic change (design D2 consumer 2): the chunk that
    enabled mode bounds-defers is a drop candidate in disabled mode. Both
    modes run against the SAME fixture so the delta is the mode, nothing else.
    """
    completeness = _completeness_receipt(
        bounds_start=_NOW - timedelta(days=65),
        bounds_end=_NOW,
    )
    _write_json(tmp_path / "completeness.json", completeness)
    _write_json(tmp_path / "drill.json", _drill_receipt())
    partial = _chunk("hydro", "river_timeseries", "chk-partial", delta_days=60, duration_days=7)
    covered = _chunk("hydro", "river_timeseries", "chk-covered", delta_days=53, duration_days=7)

    enabled_receipt = retention.run_retention(
        _build_config(tmp_path, per_tick_bound=5),
        _NOW,
        fetch_chunks=_StubRunner([partial, covered]).fetch,
    )
    disabled_receipt = retention.run_retention(
        _build_config(tmp_path, per_tick_bound=5, archive_gate="disabled"),
        _NOW,
        fetch_chunks=_StubRunner([partial, covered]).fetch,
    )

    assert enabled_receipt["candidate_chunks"] == [covered.qualified_name]
    assert enabled_receipt["deferred_remainder"] == [partial.qualified_name]
    assert disabled_receipt["candidate_chunks"] == [
        partial.qualified_name,
        covered.qualified_name,
    ]
    assert disabled_receipt["deferred_remainder"] == []
    jsonschema.validate(disabled_receipt, _load_schema())


def test_bounds_insufficient_refusal_is_unreachable_when_gate_disabled(
    tmp_path: Path,
) -> None:
    """Same fixture that refuses BOUNDS_INSUFFICIENT in enabled mode runs
    clean in disabled mode (that refusal is an archive-family code).
    """
    completeness = _completeness_receipt(
        bounds_start=_NOW - timedelta(days=40),
        bounds_end=_NOW - timedelta(days=32),
    )
    _write_json(tmp_path / "completeness.json", completeness)
    chunks = [_chunk("hydro", "river_timeseries", "chk-old", delta_days=80, duration_days=7)]

    enabled_receipt = retention.run_retention(
        _build_config(tmp_path), _NOW, fetch_chunks=_StubRunner(chunks).fetch
    )
    disabled_receipt = retention.run_retention(
        _build_config(tmp_path, archive_gate="disabled"),
        _NOW,
        fetch_chunks=_StubRunner(chunks).fetch,
    )

    assert (
        enabled_receipt["refusal_reason"]
        == retention.CODE_COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT
    )
    assert disabled_receipt["outcome"] == "dry-run"
    assert disabled_receipt["candidate_chunks"] == ["_timescaledb_internal.chk-old"]


def test_enabled_mode_receipts_carry_mode_enabled_without_adr_reference(
    tmp_path: Path,
) -> None:
    """Default (env unset) receipts self-describe as ``enabled`` and MUST NOT
    cite the ADR revision — the citation is the disabled-mode authorization.
    """
    config = _build_config(tmp_path)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert config.archive_gate == "enabled"
    assert receipt["archive_gate"] == {"mode": "enabled"}
    assert "adr_reference" not in receipt["archive_gate"]


# --- tasks 2.4: unreachability of the archive family ----------------------


_ARCHIVE_GATE_FUNCTIONS = (
    "load_completeness_receipt",
    "load_drill_receipt",
    "check_completeness_gate",
    "check_drill_gate",
)


def _forbid_archive_gate_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ARCHIVE_GATE_FUNCTIONS:

        def _boom(*args: Any, __name: str = name, **kwargs: Any) -> Any:
            raise AssertionError(f"{__name} must not run in disabled mode")

        monkeypatch.setattr(retention, name, _boom)


@pytest.mark.parametrize("enforce", [False, True], ids=["dry-run", "enforce"])
def test_disabled_mode_never_calls_the_archive_gate_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce: bool
) -> None:
    _forbid_archive_gate_functions(monkeypatch)
    config = _build_config(tmp_path, archive_gate="disabled", enforce=enforce)
    stub = _StubRunner([_chunk("hydro", "river_timeseries", "chk-a", delta_days=60)])

    receipt = retention.run_retention(
        config,
        _NOW,
        fetch_chunks=stub.fetch,
        measure_chunk_bytes=stub.measure,
        drop_chunk=stub.drop,
    )

    assert receipt["outcome"] == ("enforced" if enforce else "dry-run")
    assert receipt["archive_gate"] == {"mode": "disabled", "adr_reference": _ADR_REFERENCE}
    jsonschema.validate(receipt, _load_schema())


def _assert_disabled_refusal(receipt: Mapping[str, Any]) -> None:
    assert receipt["outcome"] == "refused"
    prefix = str(receipt["refusal_reason"]).split(":", maxsplit=1)[0]
    assert prefix not in _ARCHIVE_FAMILY_CODES, prefix
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
    env = _disabled_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH", raising=False)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH", raising=False)
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
    env = _disabled_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH", raising=False)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH", raising=False)

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
    env = _disabled_env(tmp_path, NODE27_TIMESERIES_RETENTION_ENFORCE="1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH", raising=False)
    monkeypatch.delenv("NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH", raising=False)
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
    section = _runbook_section("### 8.5 Reading the receipt", "### 8.6 Recovery")
    assert "archive_gate" in section
    assert _ARCHIVE_GATE_ENV_KEY in section


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
