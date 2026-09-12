"""Physical-parent admission contracts, including pre-change assertion oracles."""

import pytest

from packages.common.compressed_chunk_cold_runtime_catalog import (
    ColdRuntimeError,
    derive_hypertable_inventory,
)


def narrow_rows():
    fields = (
        ("run_key", "integer", True, "b"),
        ("basin_version_key", "integer", True, "b"),
        ("river_network_version_key", "integer", True, "b"),
        ("river_segment_key", "integer", True, "b"),
        ("valid_time", "timestamp with time zone", True, "b"),
        ("lead_time_hours", "integer", False, "b"),
        ("variable_e", "hydro.river_variable", True, "e"),
        ("value", "double precision", True, "b"),
        ("unit_e", "hydro.river_unit", True, "e"),
        ("quality_flag_e", "hydro.river_quality_flag", True, "e"),
        ("created_at", "timestamp with time zone", True, "b"),
    )
    return [
        dict(
            attnum=index,
            attname=name,
            type_name=kind,
            attnotnull=required,
            attidentity="",
            attgenerated="",
            typtype=typtype,
            parent_oid=2001,
            hypertable_id=17,
        )
        for index, (name, kind, required, typtype) in enumerate(fields, 1)
    ]


def derive(rows, name="river_timeseries"):
    return derive_hypertable_inventory(lambda _sql, _params=None: rows, "hydro", name)


def test_wide_with_surrogate_keys_refuses():
    rows = narrow_rows()
    rows.append(dict(rows[0], attnum=12, attname="run_id", type_name="text"))
    with pytest.raises(ColdRuntimeError):
        derive(rows)


@pytest.mark.parametrize("field", ["parent_oid", "hypertable_id"])
def test_same_columns_changed_parent_changes_digest(field):
    rows = narrow_rows()
    before = derive(rows)
    after = derive([dict(row, **{field: row[field] + 1}) for row in rows])
    assert before.digest != after.digest


@pytest.mark.parametrize("field", ["parent_oid", "hypertable_id"])
@pytest.mark.parametrize("value", [None, False, True, 0, -1, "2001"])
def test_invalid_parent_identity_refuses(field, value):
    with pytest.raises(ColdRuntimeError):
        derive([dict(row, **{field: value}) for row in narrow_rows()])


def test_mixed_observation_refuses():
    rows = narrow_rows()
    rows[-1]["parent_oid"] += 1
    with pytest.raises(ColdRuntimeError):
        derive(rows)


def test_explicit_legacy_refuses():
    with pytest.raises(ColdRuntimeError):
        derive(narrow_rows(), "river_timeseries_legacy")


def test_narrow_without_legacy_retains_extra_columns():
    rows = narrow_rows()
    rows.append(dict(rows[0], attnum=12, attname="extra_value", type_name="double precision"))
    inventory = derive(rows)
    assert inventory.columns[-1].name == "extra_value"
    assert inventory.parent_oid == 2001
    assert inventory.hypertable_id == 17


@pytest.mark.parametrize("days", [1, 3, 7])
def test_candidate_preserves_actual_range_and_rejects_corrupt_membership(days):
    from datetime import timedelta

    from packages.common.compressed_chunk_cold_runtime_catalog import derive_bound_inventories, load_eligible_chunks
    from tests.cold_residency_fakes import CUTOFF, FakeConnection, chunk, complete_relations

    connection = FakeConnection()
    item = chunk(range_start=CUTOFF - timedelta(days=days))
    connection.load_group(item, complete_relations())

    def execute(sql, params=None):
        return connection.dispatch(sql, params)[0]

    inventories = derive_bound_inventories(execute)
    kwargs = dict(
        inventory=inventories.river, schema="hydro", name="river_timeseries", cutoff=CUTOFF, limit=4, max_bytes=65536
    )
    assert load_eligible_chunks(execute, **kwargs) == [item]

    def corrupt(sql, params=None):
        rows = execute(sql, params)
        if "timescaledb_information.chunks" in sql:
            return [dict(row, hypertable_id=999) for row in rows]
        return rows

    with pytest.raises(ColdRuntimeError, match="parent identity"):
        load_eligible_chunks(corrupt, **kwargs)


@pytest.mark.parametrize("consumer", ["reload", "runtime", "census", "post_target"])
def test_parent_swap_cannot_be_adopted_by_consumers(consumer):
    from packages.common.compressed_chunk_cold_runtime import inspect_residency_group
    from packages.common.compressed_chunk_cold_runtime_catalog import (
        collect_residency_group,
        derive_bound_inventories,
        durable_payload,
        load_catalog_chunk,
    )
    from packages.common.node27_issue1895_post_target import observe_named_group
    from scripts.node27_cold_residency_census import CensusObserver
    from tests.cold_residency_fakes import CUTOFF, FakeConnection, chunk, complete_relations

    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations())

    def execute(sql, params=None):
        return connection.dispatch(sql, params)[0]

    inventories = derive_bound_inventories(execute)
    durable = durable_payload(collect_residency_group(execute, item))
    connection.parent_oids[("hydro", "river_timeseries")] += 1
    with pytest.raises(ColdRuntimeError):
        if consumer == "reload":
            load_catalog_chunk(
                execute,
                inventory=inventories.river,
                hypertable_schema="hydro",
                hypertable_name="river_timeseries",
                origin_schema=item.origin_schema,
                origin_name=item.origin_name,
            )
        elif consumer == "runtime":
            inspect_residency_group(connect=lambda: connection, chunk=item, inventories=inventories)
        elif consumer == "census":
            CensusObserver(connection).candidates(inventories=inventories, cutoff=CUTOFF, per_table_limit=4)
        else:
            observe_named_group(execute, durable=durable, inventories=inventories)


def test_intersecting_observer_refuses_changed_parent_before_discovery():
    from packages.common.compressed_chunk_cold_runtime_catalog import derive_bound_inventories
    from packages.common.node27_issue1895_catalog import observe_intersecting_groups
    from packages.common.node27_issue1895_types import Issue1895ReadinessError
    from tests.cold_residency_fakes import FakeConnection

    connection = FakeConnection()

    def execute(sql, params=None):
        return connection.dispatch(sql, params)[0]

    inventories = derive_bound_inventories(execute)
    connection.parent_oids[("hydro", "river_timeseries")] += 1
    with pytest.raises(Issue1895ReadinessError):
        observe_intersecting_groups(
            execute,
            inventories=inventories,
            window_start="2026-06-27T00:00:00Z",
            window_end="2026-06-28T00:00:00Z",
            kind="cold",
        )


def test_pinned_runtime_invokes_physical_parent_discriminator():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("tests/test_compressed_chunk_cold_runtime_integration.py").read_text())
    harness = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_isolated_cluster_production_runtime_not_probe_executor"
    )
    calls = [
        node
        for node in ast.walk(harness)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_assert_physical_parent_admission"
    ]
    assert len(calls) == 1


def test_cold_gate_requires_both_children_and_fresh_artifacts():
    from pathlib import Path

    text = Path("docs/runbooks/tier-node27-timeseries-storage.md").read_text()
    assert "#2290 and #2291 must both be merged before a fresh G0" in text
    assert "reuse pre-expand or cross-revision artifacts" in text
    assert "production rollout remains HOLD" in text


def test_parent_binding_stays_off_closed_inventory_wire():
    from packages.common.compressed_chunk_cold_runtime_catalog import derive_bound_inventories
    from tests.cold_residency_fakes import FakeConnection

    connection = FakeConnection()
    inventories = derive_bound_inventories(lambda sql, params=None: connection.dispatch(sql, params)[0])
    payload = inventories.as_payload()
    assert set(payload) == {"digest", "hypertables"}
    for inventory in payload["hypertables"].values():
        assert set(inventory) == {"schema", "name", "digest", "columns"}


@pytest.mark.parametrize("phase", ["locked", "fresh"])
def test_parent_drift_never_produces_successful_move_observation(phase):
    from packages.common.compressed_chunk_cold_runtime import RuntimeConfig, migrate_residency_group
    from tests.cold_residency_fakes import (
        LAG,
        WATERMARK,
        FakeConnection,
        chunk,
        complete_relations,
        expected_exec_identity,
        target_observation,
    )

    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations())

    def substitute(current):
        current.parent_oids[("hydro", "river_timeseries")] += 1

    if phase == "locked":
        connection.after_lock_hook = substitute
    else:
        connection.commit_hook = substitute
    observation = migrate_residency_group(
        connect=lambda: connection,
        chunk=item,
        inventories=connection.inventories,
        watermark=WATERMARK,
        lag_seconds=LAG,
        cold_free_bytes=10**12,
        hot_free_bytes=10**12,
        cold_reserve_bytes=1,
        wal_reserve_bytes=1,
        config=RuntimeConfig(
            inspect_target=target_observation,
            expected_device_identity="8:1",
            **expected_exec_identity(),
        ),
    )
    assert observation.outcome not in {"migrated", "already_target"}
    assert observation.reconciliation == "unknown"
    assert observation.before.members
    if phase == "locked":
        assert not observation.shell_sql_executed


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_enum", "nullable_key"])
def test_incomplete_or_ambiguous_narrow_observation_refuses(mutation):
    rows = narrow_rows()
    if mutation == "missing":
        rows = []
    elif mutation == "duplicate":
        rows.insert(1, dict(rows[0]))
    elif mutation == "wrong_enum":
        rows[6]["typtype"] = "b"
    else:
        rows[0]["attnotnull"] = False
    with pytest.raises(ColdRuntimeError):
        derive(rows)
