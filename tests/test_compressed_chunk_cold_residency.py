"""Unit/contract tests for the #1892 shell-first cold-residency seam.

Expected values are spec literals from
``openspec/changes/compressed-chunk-cold-tablespace-tiering`` and the pinned
node-27 snapshot, not recomputed from the implementation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    ALLOWED_HYPERTABLES,
    COLD_TABLESPACE_NAME,
    LIVE_CONTAINER_NAME,
    LIVE_PGDATA,
    LIVE_PORT,
    PINNED_IMAGE_ID,
    PINNED_TIMESCALEDB_VERSION,
    REJECTED_SEQUENCE_NAMES,
    CatalogChunk,
    CatalogRelation,
    ColdResidencyError,
    DurableIdentity,
    ResidencyMember,
    build_shell_first_plan,
    classify_eligibility,
    classify_reconciliation,
    classify_residency,
    compute_cutoff,
    origin_shell_is_not_complete,
    quote_ident,
    refuse_live_identity,
    resolve_residency_group,
    same_window_groups_are_separate,
    snapshot_image_identity,
)

_WATERMARK = datetime(2026, 7, 11, 12, tzinfo=UTC)
_LAG = 604800
_CUTOFF = datetime(2026, 7, 4, 12, tzinfo=UTC)
_RANGE_START = datetime(2026, 6, 27, 12, tzinfo=UTC)


def _chunk(**overrides: object) -> CatalogChunk:
    values: dict[str, object] = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "origin_oid": 10,
        "origin_schema": "_timescaledb_internal",
        "origin_name": "_hyper_1_1_chunk",
        "compressed_oid": 20,
        "compressed_schema": "_timescaledb_internal",
        "compressed_name": "compress_hyper_2_2_chunk",
        "range_start": _RANGE_START,
        "range_end": _CUTOFF,
        "is_compressed": True,
    }
    values.update(overrides)
    return CatalogChunk(**values)  # type: ignore[arg-type]


def _rel(
    oid: int,
    schema: str,
    name: str,
    relkind: str,
    tablespace: str,
    nbytes: int,
    *,
    toast_oid: int | None = None,
    heap_oid: int | None = None,
) -> CatalogRelation:
    return CatalogRelation(oid, schema, name, relkind, tablespace, nbytes, toast_oid, heap_oid)


def _complete_relations(
    *,
    origin_space: str = "pg_default",
    other_space: str | None = None,
    compressed_index_space: str | None = None,
) -> tuple[CatalogRelation, ...]:
    compressed_space = other_space or origin_space
    compressed_idx_space = compressed_index_space or compressed_space
    internal = "_timescaledb_internal"
    return (
        _rel(10, internal, "_hyper_1_1_chunk", "r", origin_space, 8192, toast_oid=30),
        _rel(15, internal, "10_23_river_timeseries_pkey", "i", origin_space, 16384, heap_oid=10),
        _rel(16, internal, "10_probe_extra_idx", "i", origin_space, 8192, heap_oid=10),
        _rel(20, internal, "compress_hyper_2_2_chunk", "r", compressed_space, 4096, toast_oid=40),
        _rel(25, internal, "compress_hyper_2_2_chunk_idx", "i", compressed_idx_space, 8192, heap_oid=20),
        _rel(30, "pg_toast", "pg_toast_10", "t", origin_space, 32768),
        _rel(31, "pg_toast", "pg_toast_10_index", "i", origin_space, 8192, heap_oid=30),
        _rel(40, "pg_toast", "pg_toast_20", "t", compressed_space, 16384),
        _rel(41, "pg_toast", "pg_toast_20_index", "i", compressed_space, 8192, heap_oid=40),
    )


def test_pinned_image_identity_matches_committed_snapshot_literals() -> None:
    identity = snapshot_image_identity()
    assert identity["image_id"] == "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e"
    assert identity["image_ref"] == "timescale/timescaledb-ha:pg15-latest"
    assert identity["pg_version_prefix"] == "15.2"
    assert identity["timescaledb_version"] == "2.10.2"
    assert PINNED_IMAGE_ID == identity["image_id"]
    assert PINNED_TIMESCALEDB_VERSION == "2.10.2"


def test_accepted_sequence_is_shell_first_not_direct_alter() -> None:
    assert ACCEPTED_SEQUENCE_NAME == "shell_first_decompress_recompress_atomic"
    assert "alter_tablespace_oid_order" in REJECTED_SEQUENCE_NAMES
    assert "timescaledb_experimental.move_chunk" in REJECTED_SEQUENCE_NAMES
    assert "direct_compressed_heap_alter" in REJECTED_SEQUENCE_NAMES
    assert "decompress_first" in REJECTED_SEQUENCE_NAMES
    assert "two_transaction" in REJECTED_SEQUENCE_NAMES
    assert ACCEPTED_SEQUENCE_NAME not in REJECTED_SEQUENCE_NAMES


def test_exact_cutoff_compressed_chunk_is_eligible() -> None:
    assert compute_cutoff(_WATERMARK, _LAG) == _CUTOFF
    assert (
        classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=_CUTOFF,
            watermark=_WATERMARK,
            lag_seconds=_LAG,
        )
        == "eligible"
    )


def test_hot_uncompressed_other_hypertable_and_missing_watermark_are_ineligible() -> None:
    newer = _CUTOFF + timedelta(seconds=1)
    assert (
        classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=newer,
            watermark=_WATERMARK,
            lag_seconds=_LAG,
        )
        == "ineligible_newer"
    )
    assert (
        classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=False,
            range_end=_CUTOFF,
            watermark=_WATERMARK,
            lag_seconds=_LAG,
        )
        == "ineligible_uncompressed"
    )
    assert (
        classify_eligibility(
            hypertable_schema="public",
            hypertable_name="other",
            is_compressed=True,
            range_end=_CUTOFF,
            watermark=_WATERMARK,
            lag_seconds=_LAG,
        )
        == "ineligible_hypertable"
    )
    assert (
        classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=_CUTOFF,
            watermark=None,
            lag_seconds=_LAG,
        )
        == "refused_watermark"
    )
    assert ALLOWED_HYPERTABLES == {
        ("hydro", "river_timeseries"),
        ("met", "forcing_station_timeseries"),
    }


def test_complete_group_includes_origin_compressed_indexes_and_toast() -> None:
    group = resolve_residency_group(_chunk(), _complete_relations())
    assert group.blocker is None
    kinds = tuple(member.kind for member in group.members)
    names = tuple(member.name for member in group.members)
    assert kinds == (
        "origin_heap",
        "index",
        "index",
        "compressed_heap",
        "index",
        "toast_heap",
        "toast_index",
        "toast_heap",
        "toast_index",
    )
    assert "10_23_river_timeseries_pkey" in names
    assert "10_probe_extra_idx" in names
    assert quote_ident("10_23_river_timeseries_pkey") == '"10_23_river_timeseries_pkey"'


def test_durable_identity_is_origin_and_range_not_sibling() -> None:
    group = resolve_residency_group(_chunk(), _complete_relations())
    identity = group.durable_identity
    assert identity == DurableIdentity(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=10,
        origin_schema="_timescaledb_internal",
        origin_name="_hyper_1_1_chunk",
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    replaced = resolve_residency_group(
        _chunk(compressed_oid=99, compressed_name="compress_hyper_2_9_chunk"),
        _complete_relations()[:3]
        + (_rel(99, "_timescaledb_internal", "compress_hyper_2_9_chunk", "r", "pg_default", 4096),),
    )
    assert replaced.durable_identity == identity
    assert replaced.compressed_oid != group.compressed_oid


def test_origin_shell_alone_cannot_prove_cold_residency() -> None:
    relations = _complete_relations(origin_space=COLD_TABLESPACE_NAME, other_space="pg_default")
    group = resolve_residency_group(_chunk(), relations)
    assert classify_residency(group.members) == "mixed"
    assert origin_shell_is_not_complete(group) is True
    plan = build_shell_first_plan(group)
    assert plan.kind == "blocked"
    assert plan.reason == "group residency is mixed"
    assert plan.shell_move_sql == ()


def test_transient_mixed_compressed_index_is_not_a_terminal_success() -> None:
    relations = _complete_relations(
        origin_space=COLD_TABLESPACE_NAME,
        other_space=COLD_TABLESPACE_NAME,
        compressed_index_space="pg_default",
    )
    group = resolve_residency_group(_chunk(), relations)
    assert classify_residency(group.members) == "mixed"
    plan = build_shell_first_plan(group)
    assert plan.kind == "blocked"
    assert all("compress_hyper_2_2_chunk" not in sql or "ALTER TABLE" not in sql for sql in plan.shell_move_sql)


def test_missing_compressed_relation_blocks_the_group() -> None:
    chunk = _chunk(compressed_oid=None, compressed_schema=None, compressed_name=None)
    relations = (
        _rel(10, "_timescaledb_internal", "_hyper_1_1_chunk", "r", "pg_default", 8192),
        _rel(15, "_timescaledb_internal", "10_23_river_timeseries_pkey", "i", "pg_default", 8192, heap_oid=10),
    )
    group = resolve_residency_group(chunk, relations)
    assert group.blocker == "compressed relation is missing"
    assert build_shell_first_plan(group).kind == "blocked"


def test_cross_group_index_is_a_blocker() -> None:
    relations = _complete_relations() + (
        CatalogRelation(99, "_timescaledb_internal", "foreign_idx", "i", "pg_default", 8192, heap_oid=1000),
    )
    group = resolve_residency_group(_chunk(), relations)
    assert group.blocker == "cross-group index mapping"


def test_empty_and_no_user_index_groups_still_enumerate_existing_members() -> None:
    empty = resolve_residency_group(
        _chunk(compressed_oid=21, compressed_name="compress_hyper_2_3_chunk"),
        (
            _rel(10, "_timescaledb_internal", "_hyper_1_1_chunk", "r", "pg_default", 0),
            _rel(21, "_timescaledb_internal", "compress_hyper_2_3_chunk", "r", "pg_default", 8192),
            _rel(22, "_timescaledb_internal", "compress_hyper_2_3_chunk_idx", "i", "pg_default", 8192, heap_oid=21),
        ),
    )
    assert empty.blocker is None
    assert [member.kind for member in empty.members] == ["origin_heap", "compressed_heap", "index"]
    assert empty.members[0].bytes == 0

    no_user_index = resolve_residency_group(
        _chunk(hypertable_schema="met", hypertable_name="forcing_station_timeseries"),
        (
            _rel(10, "_timescaledb_internal", "_hyper_1_4_chunk", "r", "pg_default", 8192),
            _rel(11, "_timescaledb_internal", "_hyper_1_4_chunk_pkey", "i", "pg_default", 8192, heap_oid=10),
            _rel(20, "_timescaledb_internal", "compress_hyper_2_5_chunk", "r", "pg_default", 4096),
        ),
    )
    assert no_user_index.blocker is None
    assert {member.kind for member in no_user_index.members} == {"origin_heap", "index", "compressed_heap"}


def test_same_window_chunks_keep_separate_identities() -> None:
    hydro = resolve_residency_group(_chunk(), _complete_relations())
    met = resolve_residency_group(
        _chunk(
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            origin_oid=110,
            origin_name="_hyper_2_1_chunk",
            compressed_oid=120,
            compressed_name="compress_hyper_3_2_chunk",
        ),
        (
            _rel(110, "_timescaledb_internal", "_hyper_2_1_chunk", "r", "pg_default", 8192),
            _rel(111, "_timescaledb_internal", "_hyper_2_1_chunk_pkey", "i", "pg_default", 8192, heap_oid=110),
            _rel(120, "_timescaledb_internal", "compress_hyper_3_2_chunk", "r", "pg_default", 4096),
        ),
    )
    assert same_window_groups_are_separate(hydro, met) is True
    assert hydro.durable_identity.origin_oid != met.durable_identity.origin_oid


def test_shell_first_plan_locks_heaps_and_moves_only_origin_shell_and_indexes() -> None:
    group = resolve_residency_group(_chunk(), _complete_relations())
    plan = build_shell_first_plan(group)
    assert plan.kind == "migrate"
    assert plan.phases == (
        "begin_timeouts",
        "lock_heaps",
        "move_origin_shell_and_indexes",
        "decompress",
        "prove_expanded_cold",
        "recompress",
        "prove_complete_cold",
        "commit",
    )
    assert plan.prefix_sql == (
        "BEGIN",
        "SET LOCAL lock_timeout = '2s'",
        "SET LOCAL statement_timeout = '30s'",
    )
    assert plan.lock_oids == (10, 20)
    assert plan.shell_move_oids == (10, 15, 16)
    assert plan.lock_sql == (
        'LOCK TABLE "_timescaledb_internal"."_hyper_1_1_chunk" IN ACCESS EXCLUSIVE MODE',
        'LOCK TABLE "_timescaledb_internal"."compress_hyper_2_2_chunk" IN ACCESS EXCLUSIVE MODE',
    )
    assert plan.shell_move_sql == (
        'ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "nhms_cold"',
        'ALTER INDEX "_timescaledb_internal"."10_23_river_timeseries_pkey" SET TABLESPACE "nhms_cold"',
        'ALTER INDEX "_timescaledb_internal"."10_probe_extra_idx" SET TABLESPACE "nhms_cold"',
    )
    assert plan.decompress_sql == ("SELECT decompress_chunk('_timescaledb_internal._hyper_1_1_chunk'::regclass)::text")
    assert plan.compress_sql == ("SELECT compress_chunk('_timescaledb_internal._hyper_1_1_chunk'::regclass)::text")
    joined = "\n".join(plan.shell_move_sql)
    assert "compress_hyper_2_2_chunk" not in joined
    assert "pg_toast" not in joined


def test_already_target_is_a_no_write_noop() -> None:
    group = resolve_residency_group(_chunk(), _complete_relations(origin_space=COLD_TABLESPACE_NAME))
    plan = build_shell_first_plan(group)
    assert classify_residency(group.members) == "already_target"
    assert plan.kind == "already_cold"
    assert plan.shell_move_sql == ()
    assert plan.decompress_sql is None
    assert plan.compress_sql is None
    assert plan.lock_oids == (10, 20)


def test_reconciliation_allows_new_sibling_only_for_complete_target() -> None:
    source = resolve_residency_group(_chunk(), _complete_relations())
    target = resolve_residency_group(
        _chunk(compressed_oid=99, compressed_name="compress_hyper_2_9_chunk"),
        (
            _rel(10, "_timescaledb_internal", "_hyper_1_1_chunk", "r", COLD_TABLESPACE_NAME, 0, toast_oid=30),
            _rel(
                15, "_timescaledb_internal", "10_23_river_timeseries_pkey", "i", COLD_TABLESPACE_NAME, 8192, heap_oid=10
            ),
            _rel(16, "_timescaledb_internal", "10_probe_extra_idx", "i", COLD_TABLESPACE_NAME, 8192, heap_oid=10),
            _rel(
                99, "_timescaledb_internal", "compress_hyper_2_9_chunk", "r", COLD_TABLESPACE_NAME, 8192, toast_oid=40
            ),
            _rel(
                100,
                "_timescaledb_internal",
                "compress_hyper_2_9_chunk_idx",
                "i",
                COLD_TABLESPACE_NAME,
                8192,
                heap_oid=99,
            ),
            _rel(30, "pg_toast", "pg_toast_10", "t", COLD_TABLESPACE_NAME, 0),
            _rel(31, "pg_toast", "pg_toast_10_index", "i", COLD_TABLESPACE_NAME, 8192, heap_oid=30),
            _rel(40, "pg_toast", "pg_toast_20", "t", COLD_TABLESPACE_NAME, 8192),
            _rel(41, "pg_toast", "pg_toast_20_index", "i", COLD_TABLESPACE_NAME, 8192, heap_oid=40),
        ),
    )
    parity = {
        "count": 24,
        "value_sum": 138.0,
        "checksum": "44e88875287a81d598d28044dc7e605e",
        "range_start": "2026-06-27T12:00:00Z",
        "range_end": "2026-07-04T12:00:00Z",
    }
    assert classify_reconciliation(source, target, before_parity=parity, after_parity=parity) == "complete_target"
    rolled = resolve_residency_group(_chunk(), _complete_relations())
    assert classify_reconciliation(source, rolled, before_parity=parity, after_parity=parity) == "complete_source"
    assert classify_reconciliation(source, None) == "unknown"
    mixed = resolve_residency_group(
        _chunk(),
        _complete_relations(origin_space=COLD_TABLESPACE_NAME, compressed_index_space="pg_default"),
    )
    assert classify_reconciliation(source, mixed, before_parity=parity, after_parity=parity) == "mixed"
    assert classify_reconciliation(source, rolled) == "unknown"
    assert classify_reconciliation(source, rolled, before_parity=parity) == "unknown"
    assert classify_reconciliation(source, target, after_parity=parity) == "unknown"
    assert classify_reconciliation(source, mixed) == "unknown"
    new_sibling_on_source = resolve_residency_group(
        _chunk(compressed_oid=99, compressed_name="compress_hyper_2_9_chunk"),
        (
            _rel(10, "_timescaledb_internal", "_hyper_1_1_chunk", "r", "pg_default", 0, toast_oid=30),
            _rel(15, "_timescaledb_internal", "10_23_river_timeseries_pkey", "i", "pg_default", 8192, heap_oid=10),
            _rel(16, "_timescaledb_internal", "10_probe_extra_idx", "i", "pg_default", 8192, heap_oid=10),
            _rel(99, "_timescaledb_internal", "compress_hyper_2_9_chunk", "r", "pg_default", 8192, toast_oid=40),
            _rel(100, "_timescaledb_internal", "compress_hyper_2_9_chunk_idx", "i", "pg_default", 8192, heap_oid=99),
            _rel(30, "pg_toast", "pg_toast_10", "t", "pg_default", 0),
            _rel(31, "pg_toast", "pg_toast_10_index", "i", "pg_default", 8192, heap_oid=30),
            _rel(40, "pg_toast", "pg_toast_20", "t", "pg_default", 8192),
            _rel(41, "pg_toast", "pg_toast_20_index", "i", "pg_default", 8192, heap_oid=40),
        ),
    )
    assert (
        classify_reconciliation(source, new_sibling_on_source, before_parity=parity, after_parity=parity)
        == "unknown"
    )
    assert (
        classify_reconciliation(source, target, before_parity=parity, after_parity={"count": 0, "checksum": "nope"})
        == "unknown"
    )


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        (
            {
                "container_name": LIVE_CONTAINER_NAME,
                "host_port": 55492,
                "pgdata": "/tmp/nhms-1892-probe-abc",
            },
            "live container",
        ),
        (
            {
                "container_name": "nhms-1892-probe-abc",
                "host_port": LIVE_PORT,
                "pgdata": "/tmp/nhms-1892-probe-abc",
            },
            "55432",
        ),
        (
            {
                "container_name": "nhms-1892-probe-abc",
                "host_port": 55492,
                "pgdata": LIVE_PGDATA,
            },
            "live/production path",
        ),
        (
            {
                "container_name": "nhms-1892-probe-abc",
                "host_port": 55492,
                "pgdata": "/tmp/nhms-1892-probe-abc",
                "extra_paths": ["/home/nwm/NWM/tmp"],
            },
            "live/production path",
        ),
        (
            {
                "container_name": "nhms-1892-probe-abc",
                "host_port": 55492,
                "pgdata": "/tmp/nhms-1892-probe-abc",
                "extra_paths": ["/data/GHDC/nhms-cold-tablespace"],
            },
            "live/production path",
        ),
    ],
)
def test_live_identity_is_refused_before_connection(kwargs: dict[str, object], fragment: str) -> None:
    with pytest.raises(ColdResidencyError, match=fragment):
        refuse_live_identity(**kwargs)  # type: ignore[arg-type]


def test_naive_watermark_is_refused_without_wall_clock_fallback() -> None:
    with pytest.raises(ColdResidencyError, match="timezone-aware"):
        compute_cutoff(datetime(2026, 7, 11, 12), _LAG)
    assert (
        classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=_CUTOFF,
            watermark=datetime(2026, 7, 11, 12),
            lag_seconds=_LAG,
        )
        == "refused_watermark"
    )


def test_complete_source_requires_original_sibling_and_window_parity() -> None:
    source = resolve_residency_group(_chunk(), _complete_relations())
    original = resolve_residency_group(_chunk(), _complete_relations())
    window_parity = {
        "count": 2,
        "value_sum": 1.5,
        "checksum": "2b0c0e6c0d6a0f1c8a7d4e3b2a190877",
        "range_start": "2026-06-27T12:00:00Z",
        "range_end": "2026-07-04T12:00:00Z",
    }
    assert (
        classify_reconciliation(
            source,
            original,
            before_parity=window_parity,
            after_parity=window_parity,
        )
        == "complete_source"
    )
    assert (
        classify_reconciliation(
            source,
            original,
            before_parity=window_parity,
            after_parity={"count": 1, "value_sum": 0.5, "checksum": "deadbeef"},
        )
        == "unknown"
    )


def test_shared_residency_module_does_not_export_fixture_four_column_parity() -> None:
    import packages.common.compressed_chunk_cold_residency as residency

    assert not hasattr(residency, "window_parity_sql")
    assert not hasattr(residency, "canonical_parity_token")
    assert not hasattr(residency, "window_parity_from_rows")


def test_production_hypertables_are_not_the_probe_four_column_fixture() -> None:
    from pathlib import Path

    from packages.common.compressed_chunk_cold_probe.fixture_parity import (
        PROBE_FIXTURE_PARITY_COLUMNS,
    )

    hydro = Path("db/migrations/000006_hydro.sql").read_text(encoding="utf-8")
    met = Path("db/migrations/000005_met.sql").read_text(encoding="utf-8")
    assert PROBE_FIXTURE_PARITY_COLUMNS == ("id", "valid_time", "value", "payload")
    assert "run_id TEXT NOT NULL" in hydro
    assert "river_segment_id TEXT NOT NULL" in hydro
    assert "forcing_version_id TEXT NOT NULL" in met
    assert "station_id TEXT NOT NULL" in met
    assert "id integer NOT NULL" not in hydro
    assert "payload text" not in hydro
    assert "id integer NOT NULL" not in met
    assert "payload text" not in met


def test_probe_fixture_parity_hashes_all_four_fixture_columns() -> None:
    from packages.common.compressed_chunk_cold_probe.fixture_parity import (
        fixture_window_parity_sql,
    )

    sql = " ".join(fixture_window_parity_sql("hydro", "river_timeseries").split())
    assert '"hydro"."river_timeseries"' in sql
    assert "valid_time >= %s AND valid_time < %s" in sql
    assert "id::text" in sql
    assert "timezone('UTC', valid_time)" in sql
    assert 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"' in sql
    assert "value::text" in sql
    assert "CASE WHEN payload IS NULL THEN 'N' ELSE 'P' || payload END" in sql
    assert "string_agg" in sql
    assert "md5" in sql
    assert "WHERE" in sql


def test_window_parity_from_rows_ignores_sibling_and_distinguishes_null_payload() -> None:
    from packages.common.compressed_chunk_cold_probe.fixture_parity import (
        fixture_canonical_parity_token as canonical_parity_token,
    )
    from packages.common.compressed_chunk_cold_probe.fixture_parity import (
        fixture_window_parity_from_rows as window_parity_from_rows,
    )

    in_a = {
        "id": 1,
        "valid_time": datetime(2026, 6, 27, 12, tzinfo=UTC),
        "value": 0.5,
        "payload": None,
    }
    in_b = {
        "id": 1,
        "valid_time": datetime(2026, 6, 27, 13, tzinfo=UTC),
        "value": 1.5,
        "payload": "x",
    }
    sibling = {
        "id": 9,
        "valid_time": datetime(2026, 7, 4, 12, tzinfo=UTC),
        "value": 99.0,
        "payload": "hide",
    }
    assert canonical_parity_token(1, in_a["valid_time"], 0.5, None) == (
        "1\x1f2026-06-27T12:00:00.000000Z\x1f0.5\x1fN"
    )
    assert canonical_parity_token(1, in_b["valid_time"], 1.5, "x") == (
        "1\x1f2026-06-27T13:00:00.000000Z\x1f1.5\x1fPx"
    )
    joined = (
        "1\x1f2026-06-27T12:00:00.000000Z\x1f0.5\x1fN"
        "\x1e"
        "1\x1f2026-06-27T13:00:00.000000Z\x1f1.5\x1fPx"
    )
    expected = hashlib.md5(joined.encode("utf-8"), usedforsecurity=False).hexdigest()
    actual = window_parity_from_rows(
        [in_a, in_b, sibling],
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    assert actual == {"count": 2, "value_sum": 2.0, "checksum": expected}

    mutated = dict(in_a)
    mutated["value"] = 0.25
    mutated_parity = window_parity_from_rows(
        [mutated, in_b, sibling],
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    assert mutated_parity["checksum"] != expected
    assert mutated_parity["count"] == 2

    compensating_sibling = dict(sibling)
    compensating_sibling["value"] = sibling["value"] - 0.25
    hidden = window_parity_from_rows(
        [mutated, in_b, compensating_sibling],
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    assert hidden["checksum"] == mutated_parity["checksum"]
    assert hidden["checksum"] != expected

    null_vs_empty = window_parity_from_rows(
        [{**in_a, "payload": ""}],
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    none_payload = window_parity_from_rows(
        [in_a],
        range_start=_RANGE_START,
        range_end=_CUTOFF,
    )
    assert null_vs_empty["checksum"] != none_payload["checksum"]


def test_engine_identity_drift_refuses_before_mutation_callback() -> None:
    from packages.common.compressed_chunk_cold_residency import check_engine_identity

    calls: list[str] = []

    def mutate() -> None:
        calls.append("mutated")

    with pytest.raises(ColdResidencyError, match="engine identity drift"):
        check_engine_identity(
            live_image_id="sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            live_image_ref="timescale/timescaledb-ha:pg15-latest",
            requested_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            requested_image_ref="timescale/timescaledb-ha:pg15-latest",
        )
        mutate()
    assert calls == []

    with pytest.raises(ColdResidencyError, match="engine identity drift"):
        check_engine_identity(
            live_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            live_image_ref="timescale/timescaledb-ha:pg15-latest",
            requested_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            requested_image_ref="timescale/timescaledb-ha:pg15-latest",
            used_image_id="sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            used_image_ref="timescale/timescaledb-ha:pg15-latest",
        )
        mutate()
    assert calls == []

    with pytest.raises(ColdResidencyError, match="engine identity drift"):
        check_engine_identity(
            live_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            live_image_ref="timescale/timescaledb-ha:pg15-latest",
            requested_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            requested_image_ref="timescale/timescaledb-ha:pg15-latest",
            server_version="16.0",
            timescaledb_version="2.10.2",
        )
        mutate()
    assert calls == []

    flags = check_engine_identity(
        live_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
        live_image_ref="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
        requested_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
        requested_image_ref="timescale/timescaledb-ha:pg15-latest",
        used_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
        used_image_ref="timescale/timescaledb-ha:pg15-latest",
        server_version="15.2 (Ubuntu 15.2-1.pgdg22.04+1)",
        timescaledb_version="2.10.2",
    )
    assert flags["image_pin_ok"] is True
    assert flags["pg_matches_pin"] is True
    assert flags["ts_matches_pin"] is True


def test_capacity_preflight_uses_explicit_inputs_and_does_not_reclaim_source() -> None:
    from packages.common.compressed_chunk_cold_residency import evaluate_capacity_preflight

    equality = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1100,
        cold_reserve_bytes=100,
        hot_free_bytes=50,
        wal_reserve_bytes=50,
        retained_source_bytes=8000,
    )
    assert equality["approved"] is True
    assert equality["required_cold_bytes"] == 1100
    assert equality["required_hot_bytes"] == 50
    assert equality["cold_headroom_bytes"] == 0
    assert equality["hot_headroom_bytes"] == 0
    assert equality["retained_source_bytes"] == 8000
    assert equality["blockers"] == ()

    positive = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1101,
        cold_reserve_bytes=100,
        hot_free_bytes=51,
        wal_reserve_bytes=50,
        retained_source_bytes=8000,
    )
    assert positive["approved"] is True
    assert positive["cold_headroom_bytes"] == 1
    assert positive["hot_headroom_bytes"] == 1

    cold_short = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=1099,
        cold_reserve_bytes=100,
        hot_free_bytes=10_000,
        wal_reserve_bytes=50,
        retained_source_bytes=8000,
    )
    assert cold_short["approved"] is False
    assert "cold" in " ".join(cold_short["blockers"])
    assert cold_short["required_cold_bytes"] == 1100

    hot_short = evaluate_capacity_preflight(
        before_compression_total_bytes=1000,
        cold_free_bytes=10_000,
        cold_reserve_bytes=100,
        hot_free_bytes=49,
        wal_reserve_bytes=50,
        retained_source_bytes=8000,
    )
    assert hot_short["approved"] is False
    assert "hot" in " ".join(hot_short["blockers"]) or "wal" in " ".join(hot_short["blockers"])
    assert hot_short["required_hot_bytes"] == 50


def test_capacity_preflight_refuses_invalid_inputs_without_mutation_defaults() -> None:
    from packages.common.compressed_chunk_cold_residency import evaluate_capacity_preflight

    with pytest.raises(ColdResidencyError, match="capacity"):
        evaluate_capacity_preflight(
            before_compression_total_bytes=-1,
            cold_free_bytes=1,
            cold_reserve_bytes=0,
            hot_free_bytes=1,
            wal_reserve_bytes=0,
            retained_source_bytes=0,
        )
    with pytest.raises(ColdResidencyError, match="capacity"):
        evaluate_capacity_preflight(
            before_compression_total_bytes=1,
            cold_free_bytes=1,
            cold_reserve_bytes=0,
            hot_free_bytes=1,
            wal_reserve_bytes=0,
            retained_source_bytes=None,  # type: ignore[arg-type]
        )


def test_catalog_path_mismatch_is_refused_before_shell_sql() -> None:
    from packages.common.compressed_chunk_cold_residency import validate_catalog_path

    validate_catalog_path(
        catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
        expected_location="/home/postgres/pgdata/tablespaces/nhms_cold",
    )
    with pytest.raises(ColdResidencyError, match="catalog/path identity mismatch"):
        validate_catalog_path(
            catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
            expected_location="/wrong/tablespace/path",
        )


def test_mixed_members_never_report_success() -> None:
    mixed = (
        ResidencyMember("origin_heap", 1, "s", "a", "r", "pg_default", 1),
        ResidencyMember("compressed_heap", 2, "s", "b", "r", COLD_TABLESPACE_NAME, 1),
    )
    assert classify_residency(mixed) == "mixed"
    mixed_group = resolve_residency_group(
        _chunk(),
        _complete_relations(origin_space=COLD_TABLESPACE_NAME, other_space="pg_default"),
    )
    assert origin_shell_is_not_complete(mixed_group)
    assert build_shell_first_plan(mixed_group).kind == "blocked"
