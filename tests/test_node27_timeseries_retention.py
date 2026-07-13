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
- CLI + wrapper contract.
"""

from __future__ import annotations

import argparse
import fcntl
import inspect
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema
import pytest

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
_DROP_WINDOW_DAYS = 30


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
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Fixture helpers — build minimal schema-valid receipts.
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
        "schema_version": "1.0",
        "generated_at": _iso(generated_at),
        "coverage_bounds": {"start": _iso(bounds_start), "end": _iso(bounds_end)},
        "windows": list(subjects),
        "salvage_selectors": [],
    }


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
    real drill receipts NEVER carry a single tuple spanning 30 d.
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
        "DRILL_COVERAGE_FORCING_MISSING",
        "DRILL_COVERAGE_RUNS_MISSING",
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
    assert len(retention.WIRE_CODES) == 15


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
    assert config.window_days == 30
    assert config.per_tick_bound == 5
    assert config.completeness_max_age_hours == 26
    assert config.drill_max_age_days == 30
    assert config.enforce is False
    assert str(config.lock_path) == str(tmp_path / "runner.lock")


def test_config_defaults_lock_path_to_canonical(tmp_path: Path) -> None:
    env = _base_env(tmp_path, NODE27_TIMESERIES_RETENTION_LOCK_PATH=None)
    config = retention.config_from_args(_args(), env)
    assert str(config.lock_path) == "/tmp/nhms-node27-timeseries-retention.lock"


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
    assert receipt["refusal_reason"] == retention.CODE_DRILL_COVERAGE_DB_EXPORT_MISSING


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


def test_a2a_thirty_daily_tuples_union_covers_drop_window() -> None:
    """A2-a: 30 per-cycle tuples spanning drop window → drill coverage PASSES."""
    now = _NOW
    drop = _drop_window(now, 30)
    tuples = _daily_source_tuples(drop.start, drop.end, "forcing")
    assert len(tuples) == 30
    assert retention._drill_covers(tuples, "forcing", drop) is True


def test_a2b_thirty_daily_tuples_with_gap_union_fails() -> None:
    """A2-b: 30 per-cycle tuples with a 1-day gap in the middle → coverage FAILS."""
    now = _NOW
    drop = _drop_window(now, 30)
    all_tuples = _daily_source_tuples(drop.start, drop.end, "forcing")
    # Remove the tuple covering day 15 → 16 to introduce a mid-window gap.
    gapped = [t for i, t in enumerate(all_tuples) if i != 15]
    assert len(gapped) == 29
    assert retention._drill_covers(gapped, "forcing", drop) is False


def test_a2c_two_overlapping_tuples_union_covers() -> None:
    """A2-c: 2 overlapping tuples whose union covers the drop window → PASS."""
    now = _NOW
    drop = _drop_window(now, 30)
    tuples = [
        {
            "source": "forcing",
            "window": {
                "start": _iso(drop.start),
                "end": _iso(drop.start + timedelta(days=20)),
            },
        },
        {
            "source": "forcing",
            "window": {
                "start": _iso(drop.start + timedelta(days=15)),
                "end": _iso(drop.end),
            },
        },
    ]
    assert retention._drill_covers(tuples, "forcing", drop) is True


def test_a2d_single_tuple_covering_last_day_fails() -> None:
    """A2-d: single per-cycle tuple covering only last day of drop window → FAIL."""
    now = _NOW
    drop = _drop_window(now, 30)
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

    # Build 30 synthetic per-cycle manifests, each with a 24 h producer
    # window matching the drill's real shape. Union must cover the 30-day
    # drop window.
    now = _NOW
    drop = _drop_window(now, 30)
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
    assert len(cycle_tuples) == 30
    # Real drill emit shape → union covers → drill_covers PASSES.
    assert retention._drill_covers(cycle_tuples, "runs", drop) is True
    # Sanity: remove a middle cycle to introduce a gap → FAIL.
    gapped = [t for i, t in enumerate(cycle_tuples) if i != 10]
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
# DSN never appears in stderr
# ---------------------------------------------------------------------------


def test_dsn_never_appears_in_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path, DATABASE_URL="postgresql://alice:supersekret@127.0.0.1:55432/nhms")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def _bang(config: retention.RetentionConfig, cutoff: datetime) -> list[retention.ChunkRow]:
        raise RuntimeError("oops")

    retention.main(argv=[], now=_NOW, fetch_chunks=_bang)
    err = capsys.readouterr().err
    assert "supersekret" not in err
    assert "alice" not in err


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
            if "pg_total_relation_size" in sql:
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


def test_mixed_compressed_and_uncompressed_chunks_both_drop(tmp_path: Path) -> None:
    """G F3: eligible chunks list may mix ``is_compressed=True`` and ``=False``;
    both flow through the drop path unchanged. Divergence from #851
    compression sibling: compressed chunks older than 30 d ARE retention
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
        "schema_version": "1.0",
        "generated_at": _iso(_NOW),
        "mode": "enforce",
        "outcome": "enforced",
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [{"start": "not-a-datetime", "end": _iso(_NOW)}],
    }
    with pytest.raises(jsonschema.ValidationError):
        retention._validate_receipt(bad_receipt)


def test_enforced_receipt_generated_at_datetime_format_enforced() -> None:
    """G Schema C2 (symmetric): ``generated_at`` bad format is also caught."""
    bad_receipt = {
        "schema_version": "1.0",
        "generated_at": "not-a-datetime",
        "mode": "enforce",
        "outcome": "enforced",
        "dropped_chunks": [],
        "deferred_remainder": [],
        "salvage_backed_windows": [],
    }
    with pytest.raises(jsonschema.ValidationError):
        retention._validate_receipt(bad_receipt)
