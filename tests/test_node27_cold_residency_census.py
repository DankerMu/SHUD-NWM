"""Requirement-driven tests for the pre-target read-only census CLI (#1895 task 4.0).

The census is a pre-target read-only freeze: it must reuse the production
catalog/inventory/parity owners and never load the target preflight owner, the
production target inspector, a probe-private module, or movement SQL.  These
tests exercise the pure owners through a scriptable fake connection plus the
CLI failure seams (capacity policy, exact-count, group-state, engine/read-only,
head freeze, and the read-only connection setup).  The publication /
no-clobber / secret / import-surface half lives in
``tests/test_node27_cold_residency_census_publication.py`` and imports the
shared fake helpers from this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
)
from scripts import node27_cold_residency_census as census
from tests.cold_residency_fakes import (
    ColumnDescriptor,
    FakeConnection,
    bound_inventories,
    chunk,
    complete_relations,
    inventory_for,
    rel,
)

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
_CUTOFF = datetime(2026, 7, 4, 12, tzinfo=UTC)
_HEAD = "a" * 40
_DSN = "postgresql://user:secretpw@127.0.0.1:55432/nhms"
_WINDOW_COLUMNS = [
    "hypertable_schema",
    "hypertable_name",
    "chunk_schema",
    "chunk_name",
    "range_start",
    "range_end",
    "is_compressed",
    "origin_tablespace",
]


class CensusConnection(FakeConnection):
    """FakeConnection plus the two census-specific read-only queries."""

    def __init__(self) -> None:
        super().__init__()
        self.read_only_setting = "on"
        self.window_rows_override: list[Mapping[str, object]] | None = None
        self.external_targets_override: list[Mapping[str, object]] | None = None

    def dispatch(self, sql: str, params: object) -> tuple[list[Mapping[str, object]], list[str]]:
        text = " ".join(sql.split())
        if "transaction_read_only" in text:
            return [{"read_only": self.read_only_setting}], ["read_only"]
        if "origin_tablespace" in text:
            rows = self.window_rows_override or []
            return [dict(row) for row in rows], _WINDOW_COLUMNS
        if "FROM pg_tablespace" in text and "spcname" in text:
            rows = self.external_targets_override or []
            return [dict(row) for row in rows], ["spcname", "location"]
        return super().dispatch(sql, params)


def _chunk_item(index: int, *, schema: str = "hydro", **overrides: object):
    base = 10 if schema == "hydro" else 100
    name = "river_timeseries" if schema == "hydro" else "forcing_station_timeseries"
    arguments: dict[str, object] = {
        "schema": schema,
        "name": name,
        "origin_oid": base + index,
        "origin_name": f"_hyper_{base}_{index}_chunk",
        "compressed_oid": base + 50 + index,
        "compressed_name": f"compress_{base}_{index}",
    }
    arguments.update(overrides)
    return chunk(**arguments)  # type: ignore[arg-type]


def _load(
    connection: CensusConnection,
    index: int,
    *,
    schema: str = "hydro",
    before: int = 1000,
    relations: Sequence[object] | None = None,
    **overrides: object,
):
    item = _chunk_item(index, schema=schema, **overrides)
    if relations is None:
        relations = complete_relations(
            origin_oid=item.origin_oid,
            compressed_oid=item.compressed_oid,
            origin_name=item.origin_name,
            compressed_name=item.compressed_name,
        )
    connection.load_group(item, relations)  # type: ignore[arg-type]
    connection.compression_bytes[item.origin_name] = before
    return item


def _minimal_relations(item, *, origin_bytes: int, compressed_bytes: int = 0):
    return (
        rel(item.origin_oid, item.origin_schema, item.origin_name, "r", "pg_default", origin_bytes),
        rel(
            item.compressed_oid or 0,
            item.compressed_schema or "_timescaledb_internal",
            item.compressed_name or f"_c_{item.origin_oid}",
            "r",
            "pg_default",
            compressed_bytes,
        ),
    )


def _main(
    module,
    tmp_path: Path,
    connection: CensusConnection,
    *,
    require_count: str = "6",
    env: Mapping[str, str] | None = None,
    head: tuple[str | None, bool, bool] = (_HEAD, True, False),
    watermark: datetime = _NOW,
    fetch: object = None,
    output: str | None = None,
) -> tuple[int, Path]:
    target = tmp_path / "census.json" if output is None else Path(output)
    base = {"DATABASE_URL": _DSN}
    if env:
        base.update(env)
    code = module.main(
        ["--require-count", require_count, "--output", str(target)],
        env=base,
        connect=lambda dsn: connection,
        head_observer=lambda: head,
        watermark_fetcher=fetch if fetch is not None else (lambda dsn, connect=None: watermark),
    )
    return code, target


# --- capacity policy arithmetic -----------------------------------------------


def test_capacity_policy_exact_go_numbers() -> None:
    policy = census.capacity_policy(expansions=[1000, 2000, 3000], retained=[50, 50, 50], group_count=3)
    assert policy["status"] == "resolved"
    assert policy["group_count"] == "3"
    assert policy["E"] == "3000"
    assert policy["S"] == "150"
    assert policy["cold_reserve_bytes"] == "3000"
    assert policy["wal_reserve_bytes"] == "3000"
    assert policy["install_required_bytes"] == "150"
    assert policy["rollback_headroom_bytes"] == "6000"
    assert policy["installer_required_cold_free_bytes"] == "6150"
    assert all(isinstance(value, str) for value in (policy["E"], policy["S"], policy["cold_reserve_bytes"]))
    assert "not a WAL measurement" in policy["wal_reserve_basis"]
    assert "165736" not in json.dumps(policy)


def test_capacity_policy_refuses_count_mismatch_and_non_positive_values() -> None:
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[1], retained=[1], group_count=2)
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[], retained=[], group_count=1)
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[0], retained=[1], group_count=1)
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[-1], retained=[1], group_count=1)
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[1], retained=[0], group_count=1)
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[1], retained=[-1], group_count=1)


def test_capacity_policy_64bit_boundary_and_overflows() -> None:
    # Exact upper edge: E = 2^62 - 1 leaves 2E = 2^63 - 2 and S + 2E = 2^63 - 1,
    # both inside the signed-bigint canonical-decimal ceiling.
    boundary = census.capacity_policy(expansions=[2**62 - 1], retained=[1], group_count=1)
    assert boundary["E"] == str(2**62 - 1)
    assert boundary["rollback_headroom_bytes"] == str(2**63 - 2)
    assert boundary["installer_required_cold_free_bytes"] == str(2**63 - 1)
    # E itself beyond the signed-bigint ceiling refuses.
    with pytest.raises(census.CensusError):
        census.capacity_policy(expansions=[2**63], retained=[1], group_count=1)
    # 2*E overflow at E = 2^62 refuses even though E and S are individually valid.
    with pytest.raises(census.CensusError, match="ROLLBACK_HEADROOM"):
        census.capacity_policy(expansions=[2**62], retained=[1], group_count=1)
    # Sum overflow: S = 2^62 + 2^62 refuses even though each value is valid.
    with pytest.raises(census.CensusError, match="S"):
        census.capacity_policy(expansions=[1, 1], retained=[2**62, 2**62], group_count=2)


# --- pure owner API shape -----------------------------------------------------


def test_observer_owner_return_structures() -> None:
    connection = CensusConnection()
    item = _load(connection, 0)
    observer = census.CensusObserver(connection)
    server, timescale = observer.versions()
    assert isinstance(server, str) and server == "15.2"
    assert isinstance(timescale, str) and timescale == "2.10.2"
    inventories = observer.inventories()
    assert isinstance(inventories, BoundInventories)
    assert inventories.for_hypertable("hydro", "river_timeseries") is not None
    ranked = observer.candidates(cutoff=_CUTOFF, per_table_limit=2)
    assert len(ranked) == 1
    rank, range_end, schema, name, oid, candidate = ranked[0]
    assert rank == 0 and range_end == _CUTOFF and schema == "hydro" and name == "river_timeseries"
    assert oid == item.origin_oid and candidate.origin_name == item.origin_name
    parity = observer.parity(inventories, candidate)
    assert isinstance(parity.as_dict(), dict)
    assert {"row_count", "checksum", "inventory_digest", "range_start", "range_end"} <= set(parity.as_dict())
    assert isinstance(observer.before_bytes(candidate), int)
    assert observer.session_read_only() is True


# --- exact-count / distribution -----------------------------------------------


@pytest.mark.parametrize("hydro_count,met_count", [(6, 0), (3, 3), (4, 2), (0, 6)])
def test_exact_required_count_go_across_distributions(
    tmp_path: Path, hydro_count: int, met_count: int
) -> None:
    connection = CensusConnection()
    for index in range(hydro_count):
        _load(connection, index, schema="hydro", before=1000 + index)
    for index in range(met_count):
        _load(connection, index, schema="met", before=2000 + index)
    code, target = _main(census, tmp_path, connection)
    assert code == 0
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "GO"
    assert artifact["required_group_count"] == 6
    assert artifact["resolved_group_count"] == 6
    assert len(artifact["group_keys"]) == 6
    assert len(set(artifact["group_keys"])) == 6
    policy = artifact["capacity_policy"]
    assert policy["status"] == "resolved"
    expected_e = max([*(1000 + index for index in range(hydro_count)), *(2000 + index for index in range(met_count))])
    assert policy["E"] == str(expected_e)
    assert policy["wal_reserve_bytes"] == policy["E"]
    assert policy["installer_required_cold_free_bytes"] == str(
        int(policy["S"]) + 2 * int(policy["E"])
    )
    assert artifact["blockers"] == []
    assert artifact["config"]["session_read_only"] is True


def test_five_groups_missing_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    for index in range(5):
        _load(connection, index)
    code, target = _main(census, tmp_path, connection)
    assert code == 1
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "NO-GO"
    assert artifact["resolved_group_count"] == 5
    assert any("missing" in blocker for blocker in artifact["blockers"])
    assert artifact["capacity_policy"]["status"] == "unresolved"


def test_seventh_extra_group_is_named_not_truncated(tmp_path: Path) -> None:
    connection = CensusConnection()
    for index in range(7):
        _load(connection, index)
    code, target = _main(census, tmp_path, connection)
    assert code == 1
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "NO-GO"
    assert len(artifact["group_keys"]) == 7
    assert any("extra" in blocker and "_hyper_10_6_chunk" in blocker for blocker in artifact["blockers"])


def test_single_table_overflow_bound_refuses(tmp_path: Path) -> None:
    connection = CensusConnection()
    for index in range(8):
        _load(connection, index)
    code, target = _main(census, tmp_path, connection)
    assert code == 1
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "NO-GO"
    assert any("catalog scan failed" in blocker for blocker in artifact["blockers"])


# --- group state refusals -----------------------------------------------------


def _assert_no_go_with_blocker(target: Path, connection: CensusConnection, fragment: str) -> None:
    code = census.main(
        ["--require-count", "1", "--output", str(target)],
        env={"DATABASE_URL": _DSN},
        connect=lambda dsn: connection,
        head_observer=lambda: (_HEAD, True, False),
        watermark_fetcher=lambda dsn, connect=None: _NOW,
    )
    assert code == 1
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "NO-GO"
    assert any(fragment in blocker for blocker in artifact["blockers"]), artifact["blockers"]


def test_mixed_residency_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _chunk_item(0)
    connection.load_group(
        item,
        complete_relations(
            origin_oid=item.origin_oid,
            compressed_oid=item.compressed_oid,
            origin_name=item.origin_name,
            compressed_name=item.compressed_name,
            other_space="nhms_cold",
        ),
    )
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "residency is mixed")


def test_unknown_residency_is_no_go(tmp_path: Path) -> None:
    # An unresolvable group (only the chunk row, no physical relations) snapshots
    # with no members, so the residency classifier reports unknown.
    connection = CensusConnection()
    item = _chunk_item(0)
    connection.load_group(item, ())
    connection.compression_bytes[item.origin_name] = 1000
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "residency is unknown")


def test_already_target_residency_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _chunk_item(0)
    connection.load_group(
        item,
        complete_relations(
            origin_oid=item.origin_oid,
            compressed_oid=item.compressed_oid,
            origin_name=item.origin_name,
            compressed_name=item.compressed_name,
            origin_space="nhms_cold",
        ),
    )
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "already cold")


def test_uncompressed_candidate_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _chunk_item(0, is_compressed=False, compressed_oid=None)
    connection.load_group(
        item,
        (
            rel(item.origin_oid, item.origin_schema, item.origin_name, "r", "pg_default", 8192),
        ),
    )
    connection.compression_bytes[item.origin_name] = 1000
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "is not compressed")


def test_missing_compressed_sibling_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _chunk_item(0, compressed_oid=None, compressed_name=None)
    connection.load_group(
        item,
        (
            rel(item.origin_oid, item.origin_schema, item.origin_name, "r", "pg_default", 8192),
        ),
    )
    connection.compression_bytes[item.origin_name] = 1000
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "no current compressed sibling")


def test_hot_group_origin_not_pg_default_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    connection.window_rows_override = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_10_9_chunk",
            "range_start": _CUTOFF,
            "range_end": _NOW,
            "is_compressed": True,
            "origin_tablespace": "nhms_cold",
        }
    ]
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "is not pg_default")


def test_hot_group_origin_pg_default_is_not_a_blocker(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    connection.window_rows_override = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_10_9_chunk",
            "range_start": _CUTOFF,
            "range_end": _NOW,
            "is_compressed": True,
            "origin_tablespace": "pg_default",
        }
    ]
    code, target = _main(census, tmp_path, connection, require_count="1")
    assert code == 0
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "GO"
    assert artifact["hot_active_group_count"] == 1
    assert artifact["hot_active_groups"][0]["classification"] == "hot_active_compressed"


# --- owner failure refusals ---------------------------------------------------


def test_inventory_failure_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    good = bound_inventories()
    river_columns = list(good.river.columns)
    river_columns[0] = ColumnDescriptor(
        attnum=river_columns[0].attnum,
        name=river_columns[0].name,
        type_name="json",
        not_null=False,
        identity="",
        generated="",
    )
    connection.inventories = BoundInventories(
        river=inventory_for("hydro", "river_timeseries", river_columns),
        forcing=good.forcing,
        digest=good.digest,
    )
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "business inventory failed")


def test_parity_failure_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    connection.parity_rows = []
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "window parity failed")


def test_compression_stats_failure_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _load(connection, 0)
    connection.compression_bytes[item.origin_name] = None
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "compression statistics failed")


def test_retained_source_bytes_zero_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _chunk_item(0)
    connection.load_group(item, _minimal_relations(item, origin_bytes=0))
    connection.compression_bytes[item.origin_name] = 1000
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "retained_source_bytes is not positive")


def test_zero_or_negative_expansion_is_no_go(tmp_path: Path) -> None:
    for index, bad in enumerate((0, -5)):
        connection = CensusConnection()
        item = _load(connection, 0)
        connection.compression_bytes[item.origin_name] = bad
        _assert_no_go_with_blocker(
            tmp_path / f"census-zero-{index}.json", connection, "before_compression_total_bytes is not positive"
        )


def test_above_64bit_expansion_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    item = _load(connection, 0)
    connection.compression_bytes[item.origin_name] = 2**63
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "canonical decimal byte range")


def test_sum_overflow_is_no_go(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0, relations=_minimal_relations(_chunk_item(0), origin_bytes=2**62))
    _load(connection, 1, relations=_minimal_relations(_chunk_item(1), origin_bytes=2**62))
    target = tmp_path / "census.json"
    code = census.main(
        ["--require-count", "2", "--output", str(target)],
        env={"DATABASE_URL": _DSN},
        connect=lambda dsn: connection,
        head_observer=lambda: (_HEAD, True, False),
        watermark_fetcher=lambda dsn, connect=None: _NOW,
    )
    assert code == 1
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["verdict"] == "NO-GO"
    assert any("overflow" in blocker for blocker in artifact["blockers"]), artifact["blockers"]
    assert artifact["capacity_policy"]["status"] == "overflow"


def test_doubled_rollback_overflow_is_no_go(tmp_path: Path) -> None:
    # E = 2^62: individually a valid positive canonical decimal, but 2*E exceeds
    # the signed-bigint ceiling, so the policy must refuse instead of emitting
    # an unrepresentable rollback headroom.
    connection = CensusConnection()
    item = _load(connection, 0, relations=_minimal_relations(_chunk_item(0), origin_bytes=1))
    connection.compression_bytes[item.origin_name] = 2**62
    _assert_no_go_with_blocker(tmp_path / "census.json", connection, "ROLLBACK_HEADROOM")


# --- config / lag / watermark / engine / read-only ----------------------------


def test_lag_source_key_resolution() -> None:
    assert census.lag_source_key({"NODE27_COLD_RESIDENCY_LAG_SECONDS": "1"}) == "NODE27_COLD_RESIDENCY_LAG_SECONDS"
    assert census.lag_source_key(
        {"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "1"}
    ) == "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS"
    assert census.lag_source_key({"UNRELATED": "1"}) == "configured-compression-contract-default"
    assert census.lag_seconds_from_env({"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "172800"}) == 172800
    assert census.lag_seconds_from_env({}) == 604800


def test_artifact_records_lag_source_key(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    code, target = _main(
        census,
        tmp_path,
        connection,
        require_count="1",
        env={"NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "172800"},
    )
    assert code == 0
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["config"]["lag_source_key"] == "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS"
    assert artifact["lag_seconds"] == 172800


def test_watermark_failure_has_no_wall_clock_fallback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from packages.common.display_watermark import DisplayWatermarkError

    connection = CensusConnection()
    _load(connection, 0)

    def broken(dsn: str, connect: object = None) -> datetime:
        del dsn, connect
        raise DisplayWatermarkError("display watermark is unavailable")

    code, target = _main(census, tmp_path, connection, require_count="1", fetch=broken)
    assert code == 2
    assert not target.exists()
    assert '"class": "watermark"' in capsys.readouterr().err


def test_engine_drift_is_fail_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    connection.server_version = "16.1"
    code, target = _main(census, tmp_path, connection, require_count="1")
    assert code == 2
    assert not target.exists()
    assert '"class": "engine_identity"' in capsys.readouterr().err


def test_read_only_verification_failure_is_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    connection.read_only_setting = "off"
    code, target = _main(census, tmp_path, connection, require_count="1")
    assert code == 2
    assert not target.exists()
    assert '"class": "session"' in capsys.readouterr().err


def test_session_read_only_field_is_observed_not_hardcoded(tmp_path: Path) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    code, target = _main(census, tmp_path, connection, require_count="1")
    assert code == 0
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["config"]["session_read_only"] is True
    source = (_ROOT / "scripts/node27_cold_residency_census.py").read_text(encoding="utf-8")
    assert '"session_read_only": True' not in source


def test_dirty_head_refuses_before_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    code, target = _main(census, tmp_path, connection, head=(_HEAD, True, True))
    assert code == 2
    assert not target.exists()
    assert '"class": "head"' in capsys.readouterr().err


def test_unobservable_head_refuses_before_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = CensusConnection()
    _load(connection, 0)
    for head in ((None, False, False), ("not-a-sha" * 2, False, False)):
        code, target = _main(census, tmp_path, connection, head=head)
        assert code == 2
        assert not target.exists()
        assert '"class": "head"' in capsys.readouterr().err


def test_unclean_head_never_reaches_the_connection(tmp_path: Path) -> None:
    _load(CensusConnection(), 0)

    def explosive(dsn: str):
        raise AssertionError("connection must not open under an unclean head")

    for head in ((_HEAD, True, True), (None, False, False)):
        code = census.main(
            ["--require-count", "6", "--output", str(tmp_path / "census.json")],
            env={"DATABASE_URL": _DSN},
            connect=explosive,
            head_observer=lambda head=head: head,
            watermark_fetcher=lambda dsn, connect=None: _NOW,
        )
        assert code == 2, head
        assert not (tmp_path / "census.json").exists()


def test_head_oserror_is_classified_head_not_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object):
        raise OSError("git vanished")

    monkeypatch.setattr(census.subprocess, "run", boom)
    code = census.main(
        ["--require-count", "6", "--output", str(tmp_path / "census.json")],
        env={"DATABASE_URL": _DSN},
    )
    assert code == 2
    err = capsys.readouterr().err
    assert '"class": "head"' in err
    assert "connection" not in err


def test_missing_database_url_is_config_refusal_without_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = census.main(
        ["--require-count", "6", "--output", str(tmp_path / "census.json")],
        env={},
    )
    assert code == 2
    assert not (tmp_path / "census.json").exists()
    assert '"class": "config"' in capsys.readouterr().err


# --- connection read-only setup ------------------------------------------------


def test_connection_setup_uses_set_session_before_any_cursor_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-only transaction MUST be established by the driver's native
    ``set_session`` before the first cursor SQL, because psycopg2's first
    ``cursor.execute`` implicitly begins a transaction whose read-write/read-only
    character is fixed by the session default at BEGIN time.  A later
    ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` only affects the
    *next* transaction, so the census observation transaction would stay
    read-write and ``transaction_read_only`` would read ``off`` forever.
    """

    events: list[str] = []

    class RecordingCursor:
        def execute(self, sql: str, params: object = None) -> None:
            del params
            events.append(f"cursor:{sql.strip()[:60]}")

        def __enter__(self) -> "RecordingCursor":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class RecordingConnection:
        def __init__(self) -> None:
            self.autocommit = False
            self._closed = False

        def set_session(self, **kwargs: object) -> None:
            events.append(f"set_session:{sorted(kwargs.items())}")

        def cursor(self) -> RecordingCursor:
            return RecordingCursor()

        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

        def commit(self) -> None:
            events.append("commit")

    monkeypatch.setattr(census, "_attributed_connect", lambda *args, **kwargs: RecordingConnection())
    connection = census.open_readonly_connection("postgresql://user:secretpw@127.0.0.1:55432/nhms")
    del connection
    assert events[0].startswith("set_session:")
    assert "'readonly', True" in events[0] and "'autocommit', False" in events[0]
    cursor_index = next(index for index, event in enumerate(events) if event.startswith("cursor:"))
    assert cursor_index >= 1
    assert all(not event.startswith("cursor:SET SESSION CHARACTERISTICS") for event in events)
    # The timeout statements must be issued only after the read-only session is
    # established — i.e. inside a transaction that is already read-only.
    timeout_indices = [index for index, event in enumerate(events) if event.startswith("cursor:SET")]
    assert timeout_indices != []
    assert all(index > 0 for index in timeout_indices)
    assert events[0].startswith("set_session:")


def test_set_session_characteristics_alone_does_not_satisfy_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake that only records ``SET SESSION CHARACTERISTICS`` as a cursor
    statement must not be accepted: the real driver forbids that statement from
    fixing the *current* transaction, so the census must call the native
    ``set_session`` seam instead.
    """

    seen: list[str] = []

    class RecordingCursor:
        def execute(self, sql: str, params: object = None) -> None:
            del params
            seen.append(sql.strip())

        def __enter__(self) -> "RecordingCursor":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class RecordingConnection:
        def __init__(self) -> None:
            self.autocommit = False

        def set_session(self, **kwargs: object) -> None:
            raise AssertionError("native set_session must be called")

        def cursor(self) -> RecordingCursor:
            return RecordingCursor()

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(census, "_attributed_connect", lambda *args, **kwargs: RecordingConnection())
    with pytest.raises(AssertionError, match="native set_session"):
        census.open_readonly_connection("postgresql://user:secretpw@127.0.0.1:55432/nhms")
    assert not any("SET SESSION CHARACTERISTICS" in statement for statement in seen)
