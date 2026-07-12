"""Unit tests for the node-27 timeseries compression runner (issue #851)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from scripts import node27_timeseries_compression as compression

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _ROOT / "schemas/timeseries_compression_receipt.schema.json"
_MIGRATION_PATH = _ROOT / "db/migrations/000047_hypertable_compression_settings.sql"
_RUNNER_SOURCE_PATH = _ROOT / "scripts/node27_timeseries_compression.py"
_WRAPPER_PATH = _ROOT / "scripts/node27_timeseries_compression_once.sh"

_NOW = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {"enforce": False, "receipt_path": None, "lock_path": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _base_env(tmp_path: Path, *, override: dict[str, str | None] | None = None) -> dict[str, str]:
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "5",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    if override:
        for k, v in override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return env


def _chunk(
    schema_name: str,
    hyper: str,
    label: str,
    *,
    now: datetime = _NOW,
    delta_days: float,
) -> compression.ChunkRow:
    end = now - timedelta(days=delta_days)
    start = end - timedelta(days=7)
    return compression.ChunkRow(
        hypertable_schema=schema_name,
        hypertable_name=hyper,
        chunk_schema="_timescaledb_internal",
        chunk_name=label,
        range_start=start,
        range_end=end,
        is_compressed=False,
    )


# ---------------------------------------------------------------------------
# Config parse fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": ""}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "0"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "-1"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "not-a-number"}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": None}, "LAG_SECONDS"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "0"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": ""}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "-3"}, "PER_TICK_BOUND"),
        ({"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": None}, "PER_TICK_BOUND"),
        ({"DATABASE_URL": None}, "DATABASE_URL"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": None}, "receipt path"),
        ({"NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": None}, "lock path"),
        ({"NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": "relative/receipt.json"}, "absolute"),
        ({"NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": "relative.lock"}, "absolute"),
    ],
)
def test_config_parse_fails_closed(tmp_path: Path, override: dict[str, str | None], match: str) -> None:
    env = _base_env(tmp_path, override=override)
    with pytest.raises(compression.CompressionConfigError, match=match):
        compression.config_from_args(_args(), env)


def test_config_parse_happy_path(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(), env)
    assert config.lag_seconds == 604800
    assert config.per_tick_bound == 5
    assert config.enforce is False
    assert config.database_url.startswith("postgresql://")


# ---------------------------------------------------------------------------
# Chunk classification
# ---------------------------------------------------------------------------


def test_classify_partitions_by_lag_window() -> None:
    inside = _chunk("hydro", "river_timeseries", "recent", delta_days=3)
    outside = _chunk("hydro", "river_timeseries", "old", delta_days=10)
    selected, deferred, skipped = compression._classify(
        [inside, outside], now_utc=_NOW, lag_seconds=7 * 86400, per_tick_bound=5
    )
    assert [c.chunk_name for c in selected] == ["old"]
    assert deferred == []
    assert [c.chunk_name for c in skipped] == ["recent"]


def test_classify_respects_per_tick_bound() -> None:
    chunks = [
        _chunk("hydro", "river_timeseries", f"c{i:02d}", delta_days=30 - i)
        for i in range(8)
    ]
    selected, deferred, skipped = compression._classify(
        chunks, now_utc=_NOW, lag_seconds=7 * 86400, per_tick_bound=3
    )
    assert len(selected) == 3
    assert len(deferred) == 5
    assert skipped == []
    # Ordering: selected must be a strict prefix of deferred keyed by input order.
    assert [c.chunk_name for c in selected + deferred] == [c.chunk_name for c in chunks]


# ---------------------------------------------------------------------------
# Dry-run vs enforce
# ---------------------------------------------------------------------------


# Byte-size shape for the shared stubs. before returns 1 GiB per chunk, after
# returns 512 MiB — the delta proves the runner routes ``after=True`` to a
# distinct catalog lookup (cand-A). Tests that don't distinguish should assert
# on ``before_bytes`` only.
_STUB_BEFORE_BYTES = 1_073_741_824  # 1 GiB
_STUB_AFTER_BYTES = 536_870_912  # 512 MiB


def _install_stubs(monkeypatch: pytest.MonkeyPatch, *, chunks: list[compression.ChunkRow]) -> dict[str, list]:
    calls: dict[str, list] = {"compress": [], "measure": [], "measure_after": []}

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after:
            calls["measure_after"].append(chunk.chunk_name)
            return _STUB_AFTER_BYTES
        calls["measure"].append(chunk.chunk_name)
        return _STUB_BEFORE_BYTES

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        calls["compress"].append(chunk.chunk_name)

    return calls, fake_fetch, fake_measure, fake_compress


def test_dry_run_never_compresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=False), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "old-1", delta_days=10),
        _chunk("met", "forcing_station_timeseries", "old-2", delta_days=12),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    assert calls["compress"] == []
    assert receipt["mode"] == "dry-run"
    for descriptor in receipt["selected"]:
        assert descriptor["after_bytes"] is None
    assert receipt["outcome"] == "clean"


def test_enforce_calls_compress_for_each_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "old-a", delta_days=10),
        _chunk("hydro", "river_timeseries", "old-b", delta_days=11),
        _chunk("met", "forcing_station_timeseries", "old-c", delta_days=12),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    assert calls["compress"] == ["old-a", "old-b", "old-c"]
    # cand-A: the second measure call must be routed with after=True so that
    # the default DB implementation would query the compressed sibling
    # relation instead of the truncated origin chunk.
    assert calls["measure_after"] == ["old-a", "old-b", "old-c"]
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    forcing = receipt["per_table_totals"]["met.forcing_station_timeseries"]
    assert river["chunks_compressed"] == 2
    assert forcing["chunks_compressed"] == 1
    assert river["before_bytes"] == 2 * _STUB_BEFORE_BYTES
    assert forcing["before_bytes"] == _STUB_BEFORE_BYTES
    # after_bytes reflects the (fake) compressed footprint, not the origin.
    assert river["after_bytes"] == 2 * _STUB_AFTER_BYTES
    assert forcing["after_bytes"] == _STUB_AFTER_BYTES
    assert river["after_bytes"] < river["before_bytes"]
    assert forcing["after_bytes"] < forcing["before_bytes"]
    # And each per-chunk descriptor must carry the compressed after size.
    for descriptor in receipt["selected"]:
        assert descriptor["after_bytes"] == _STUB_AFTER_BYTES
        assert descriptor["before_bytes"] == _STUB_BEFORE_BYTES
    assert receipt["outcome"] == "clean"


def test_per_chunk_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "bad", delta_days=11),
    ]

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        return 100

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        if chunk.chunk_name == "bad":
            raise RuntimeError("simulated compress_chunk failure")

    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    assert "error" not in by_name["good"]
    assert by_name["good"]["after_bytes"] == 100
    assert "error" in by_name["bad"]
    assert by_name["bad"]["after_bytes"] is None
    assert receipt["outcome"] == "partial"


def test_measure_after_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cand-F: after-measurement failure MUST not cascade to other chunks."""
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "measure-failure", delta_days=11),
    ]

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after and chunk.chunk_name == "measure-failure":
            raise RuntimeError("simulated pg_total_relation_size timeout")
        return 200 if not after else 50

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        return None

    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    assert "error" not in by_name["good"]
    assert by_name["good"]["after_bytes"] == 50
    assert by_name["measure-failure"]["after_bytes"] is None
    assert "measure_chunk_bytes(after) failed" in by_name["measure-failure"]["error"]
    assert receipt["outcome"] == "partial"
    # The compressed chunk still counts in the roll-up so operators can see
    # that the mutation happened, even though after_bytes is unknown.
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    assert river["chunks_compressed"] == 2
    assert river["before_bytes"] == 200 + 200
    # cand-H: mixed success + after-fail in the same table MUST poison
    # per-table after_bytes to null — a partial sum over successful chunks
    # would mislead the (before-after)/before savings computation.
    assert river["after_bytes"] is None


def test_after_fail_poisons_per_table_after_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cand-H: mixed success + after-fail in one hypertable nulls after_bytes.

    Constructs two chunks in ``hydro.river_timeseries``: A succeeds with a
    real after measurement, B fails on after-measure. The per-table roll-up
    MUST show ``chunks_compressed=2`` and ``before_bytes = A_before + B_before``
    but ``after_bytes = None`` (poisoned by the invariant), so a downstream
    consumer cannot compute an inflated savings ratio from the partial sum.
    """
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunk_a = _chunk("hydro", "river_timeseries", "mixed-A", delta_days=10)
    chunk_b = _chunk("hydro", "river_timeseries", "mixed-B", delta_days=11)

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return [chunk_a, chunk_b]

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if after and chunk.chunk_name == "mixed-B":
            raise RuntimeError("simulated after-measure failure on mixed-B")
        if not after:
            return 400 if chunk.chunk_name == "mixed-A" else 800
        # after=True, success (chunk_a only)
        return 100

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        return None

    receipt = compression.build_receipt(
        config,
        now_utc=_NOW,
        fetch_chunks=fake_fetch,
        measure_chunk_bytes=fake_measure,
        compress_chunk=fake_compress,
    )
    river = receipt["per_table_totals"]["hydro.river_timeseries"]
    assert river["chunks_compressed"] == 2
    assert river["before_bytes"] == 400 + 800
    assert river["after_bytes"] is None
    assert receipt["outcome"] == "partial"


def test_measure_before_failure_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cand-F: before-measurement failure MUST not cascade or leak partial roll-up."""
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "good", delta_days=10),
        _chunk("hydro", "river_timeseries", "before-fail", delta_days=11),
    ]
    compressed_names: list[str] = []

    def fake_fetch(dsn: str) -> list[compression.ChunkRow]:
        return list(chunks)

    def fake_measure(dsn: str, chunk: compression.ChunkRow, *, after: bool = False) -> int:
        if not after and chunk.chunk_name == "before-fail":
            raise RuntimeError("simulated catalog probe error")
        return 300 if not after else 60

    def fake_compress(dsn: str, chunk: compression.ChunkRow) -> None:
        compressed_names.append(chunk.chunk_name)

    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    by_name = {d["chunk_name"]: d for d in receipt["selected"]}
    # Failing chunk carries the error and never entered compress_chunk.
    assert "error" in by_name["before-fail"]
    assert "measure_chunk_bytes(before) failed" in by_name["before-fail"]["error"]
    assert by_name["before-fail"]["before_bytes"] == 0
    assert by_name["before-fail"]["after_bytes"] is None
    assert compressed_names == ["good"]
    assert receipt["outcome"] == "partial"


# ---------------------------------------------------------------------------
# Lock contention
# ---------------------------------------------------------------------------


def test_main_exits_zero_on_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Pre-hold the lock in the same process.
    lock_path = Path(env["NODE27_TIMESERIES_COMPRESSION_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt_path = Path(env["NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH"])
    receipt_before = receipt_path.exists()
    try:
        code = compression.main(argv=[], now_utc=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert code == 0
    diagnostic = json.loads(capsys.readouterr().err.strip())
    assert diagnostic["status"] == "skipped"
    assert diagnostic["reason"] == "lock-contended"
    assert "secretpw" not in json.dumps(diagnostic)
    assert receipt_path.exists() == receipt_before


# ---------------------------------------------------------------------------
# Receipt schema + semantic contract
# ---------------------------------------------------------------------------


def test_receipt_validates_against_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _base_env(tmp_path)
    config = compression.config_from_args(_args(enforce=True), env)
    chunks = [
        _chunk("hydro", "river_timeseries", "sel-a", delta_days=10),
        _chunk("hydro", "river_timeseries", "sel-b", delta_days=11),
        _chunk("hydro", "river_timeseries", "def-a", delta_days=12),
        _chunk("met", "forcing_station_timeseries", "def-b", delta_days=13),
        _chunk("met", "forcing_station_timeseries", "def-c", delta_days=14),
        _chunk("met", "forcing_station_timeseries", "def-d", delta_days=15),
        _chunk("met", "forcing_station_timeseries", "skip-1", delta_days=1),
    ]
    calls, fake_fetch, fake_measure, fake_compress = _install_stubs(monkeypatch, chunks=chunks)
    # per_tick_bound=5, we should see 5 selected, 1 deferred, 1 skipped
    receipt = compression.build_receipt(
        config, now_utc=_NOW,
        fetch_chunks=fake_fetch, measure_chunk_bytes=fake_measure, compress_chunk=fake_compress,
    )
    jsonschema.validate(receipt, _load_schema())
    assert len(receipt["selected"]) == 5
    assert len(receipt["deferred"]) == 1
    assert len(receipt["skipped"]) == 1
    # Disjointness by (hypertable_schema, hypertable_name, chunk_name)
    def _key(d):
        return (d["hypertable_schema"], d["hypertable_name"], d["chunk_name"])
    selected_keys = {_key(d) for d in receipt["selected"]}
    deferred_keys = {_key(d) for d in receipt["deferred"]}
    skipped_keys = {_key(d) for d in receipt["skipped"]}
    assert selected_keys.isdisjoint(deferred_keys)
    assert selected_keys.isdisjoint(skipped_keys)
    assert deferred_keys.isdisjoint(skipped_keys)
    # per_table_totals should aggregate the selected before_bytes
    for schema_name, hyper in compression.HYPERTABLES:
        key = f"{schema_name}.{hyper}"
        expected_before = sum(
            d["before_bytes"]
            for d in receipt["selected"]
            if d["hypertable_schema"] == schema_name and d["hypertable_name"] == hyper
        )
        assert receipt["per_table_totals"][key]["before_bytes"] == expected_before


def test_example_validates_against_schema() -> None:
    example = json.loads((_ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text())
    jsonschema.validate(example, _load_schema())


# ---------------------------------------------------------------------------
# cand-C: negative schema coverage — receipts violating the fresh contract
# rows in design.md (fixture Receipt schema) MUST be rejected.
# ---------------------------------------------------------------------------


def _example_receipt() -> dict:
    return json.loads(
        (_ROOT / "schemas/examples/timeseries_compression_receipt.example.json").read_text(
            encoding="utf-8"
        )
    )


def test_schema_rejects_selected_entry_without_chunk_name() -> None:
    receipt = _example_receipt()
    del receipt["selected"][0]["chunk_name"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_per_table_total_missing_chunks_compressed() -> None:
    receipt = _example_receipt()
    del receipt["per_table_totals"]["hydro.river_timeseries"]["chunks_compressed"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_invalid_mode_enum() -> None:
    receipt = _example_receipt()
    receipt["mode"] = "compress"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_unknown_top_level_key() -> None:
    receipt = _example_receipt()
    receipt["unknown_field"] = "x"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


def test_schema_rejects_per_table_totals_missing_forcing_key() -> None:
    receipt = _example_receipt()
    del receipt["per_table_totals"]["met.forcing_station_timeseries"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(receipt, _load_schema())


# ---------------------------------------------------------------------------
# Migration text guardrails
# ---------------------------------------------------------------------------


def test_migration_contains_verbatim_segmentby_orderby() -> None:
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "hydro.river_timeseries" in text
    assert "met.forcing_station_timeseries" in text
    assert "timescaledb.compress_segmentby = 'run_id, river_network_version_id, river_segment_id'" in text
    assert "timescaledb.compress_orderby = 'variable, valid_time'" in text
    assert "timescaledb.compress_segmentby = 'forcing_version_id, station_id'" in text


def test_migration_does_not_add_compression_policy() -> None:
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    # D3 forbids background policy jobs. Grep the executable statements only,
    # not the prose header (which explains WHY policy jobs are rejected).
    executable = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    assert "add_compression_policy" not in executable


def test_migration_alter_statements_are_disjoint_and_order_independent() -> None:
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    # Strip comment lines, then split on the ALTER-TABLE boundary.
    executable = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    alters = re.findall(r"ALTER\s+TABLE\s+(\S+)\s+SET\s*\(", executable)
    assert alters == ["hydro.river_timeseries", "met.forcing_station_timeseries"]
    # Both statements touch disjoint tables so their apply order is
    # semantically irrelevant.
    assert set(alters) == {"hydro.river_timeseries", "met.forcing_station_timeseries"}


def test_migration_has_no_transaction_wrapper() -> None:
    """cand-D: partial-apply idempotency assumes each ALTER runs standalone.

    A ``DO $$``/``BEGIN``/``END $$`` block, or the explicit
    ``START TRANSACTION`` / ``COMMIT`` / ``ROLLBACK`` / ``SAVEPOINT`` verbs,
    would collapse both statements into one transaction and break the
    "re-run the migration after a partial apply completes the second
    statement" guarantee that the header prose (and design D3) promises.
    """
    text = _MIGRATION_PATH.read_text(encoding="utf-8")
    executable = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    forbidden = re.search(
        r"\bDO\s*\$\$|\bBEGIN\b|\bEND\s*\$\$|\bSTART\s+TRANSACTION\b|\bCOMMIT\b|\bROLLBACK\b|\bSAVEPOINT\b",
        executable,
        flags=re.IGNORECASE,
    )
    assert forbidden is None, (
        f"migration must not wrap ALTERs in a transaction block; matched: {forbidden.group(0)!r}"
    )


# ---------------------------------------------------------------------------
# Runner source: catalog-only guard
# ---------------------------------------------------------------------------


def test_chunk_query_filters_out_already_compressed_chunks() -> None:
    # cand-E: assert on the runtime constant so refactors that move the
    # literal into a comment or unrelated docstring can't accidentally
    # silence the guard.
    query = compression._CHUNK_QUERY
    assert "is_compressed = false" in query


def test_chunk_query_does_not_scan_detail_hypertables() -> None:
    query = compression._CHUNK_QUERY
    assert "timescaledb_information.chunks" in query
    # The runner MUST NOT read hydro.river_timeseries or
    # met.forcing_station_timeseries rows directly; those literals in the
    # source are only allowed as tuple filters against the catalog view.
    lines = [line for line in query.splitlines() if line.strip()]
    for line in lines:
        if "hydro.river_timeseries" in line or "met.forcing_station_timeseries" in line:
            # Must be inside the tuple filter (as string literals with quotes).
            assert "'" in line, f"detail hypertable referenced outside string literal: {line!r}"


# ---------------------------------------------------------------------------
# DSN masking
# ---------------------------------------------------------------------------


def test_mask_dsn_strips_credentials() -> None:
    masked = compression._mask_dsn("postgresql://user:secretpw@127.0.0.1:55432/nhms")
    assert "secretpw" not in masked
    assert "user" not in masked
    assert "127.0.0.1" in masked
    assert "55432" in masked


def test_dsn_never_appears_in_lock_contention_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    lock_path = Path(env["NODE27_TIMESERIES_COMPRESSION_LOCK_PATH"])
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        compression.main(argv=[], now_utc=_NOW)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    err = capsys.readouterr().err
    assert "secretpw" not in err
    assert "user:" not in err


def test_dsn_never_appears_in_config_failure_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://alice:supersekret@127.0.0.1:55432/nhms")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS", "")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND", "5")
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH", str(tmp_path / "receipt.json"))
    monkeypatch.setenv("NODE27_TIMESERIES_COMPRESSION_LOCK_PATH", str(tmp_path / "runner.lock"))
    code = compression.main(argv=[], now_utc=_NOW)
    assert code == 1
    err = capsys.readouterr().err
    assert "supersekret" not in err
    assert "alice" not in err


# ---------------------------------------------------------------------------
# Wrapper shell-contract (parametrized) — mirrors #849 audit-side coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("relative-wrapper-path", "wrapper paths must be absolute"),
        ("env-mode", "env file must have mode 0600"),
        ("env-symlink", "env file must be a regular non-symlink file"),
        ("missing-python", "python executable is unavailable"),
        ("missing-script", "compression entrypoint is unavailable or a symlink"),
        ("symlink-script", "compression entrypoint is unavailable or a symlink"),
    ],
)
def test_compression_wrapper_rejects_unsafe_runtime_contract(
    tmp_path: Path, case: str, expected_reason: str
) -> None:
    wrapper = _WRAPPER_PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stat_shim = bin_dir / "stat"
    stat_shim.write_text(
        "#!/bin/sh\n"
        "for last do :; done\n"
        "case \"$last\" in\n"
        "  *bad-mode.env) printf '644\\n' ;;\n"
        "  *) printf '600\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stat_shim.chmod(0o700)

    python_bin = tmp_path / "python"
    python_bin.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    python_bin.chmod(0o700)
    entrypoint = tmp_path / "compression.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")

    env_file = tmp_path / ("bad-mode.env" if case == "env-mode" else "runner.env")
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    if case == "env-symlink":
        target = tmp_path / "real.env"
        env_file.rename(target)
        env_file.symlink_to(target)

    configured_python = str(python_bin)
    if case == "missing-python":
        configured_python = str(tmp_path / "missing-python")

    configured_script = str(entrypoint)
    if case == "missing-script":
        configured_script = str(tmp_path / "missing-script.py")
    elif case == "symlink-script":
        script_link = tmp_path / "compression-link.py"
        script_link.symlink_to(entrypoint)
        configured_script = str(script_link)

    process_env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NODE27_TIMESERIES_COMPRESSION_ENV_FILE": (
            "relative.env" if case == "relative-wrapper-path" else str(env_file)
        ),
        "NODE27_TIMESERIES_COMPRESSION_PYTHON": configured_python,
        "NODE27_TIMESERIES_COMPRESSION_SCRIPT": configured_script,
    }
    result = subprocess.run(
        ["/bin/sh", str(wrapper)],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    failure = json.loads(result.stderr.strip())
    assert failure == {"status": "failed", "reason": expected_reason}
