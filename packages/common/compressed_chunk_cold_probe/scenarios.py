"""Rejected-sequence, lifecycle, boundary, capacity, and failure probe rows."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.compressed_chunk_cold_probe.catalog import (
    collect_group,
    compression_stats,
    fresh_observer,
    load_chunks,
    parity,
    require_migrate_plan,
    retained_source_bytes,
    sibling_identity,
    snapshot_group,
    try_sql,
)
from packages.common.compressed_chunk_cold_probe.cluster import (
    connect,
    execute,
    scalar,
    validate_catalog_path_preflight,
)
from packages.common.compressed_chunk_cold_probe.shell import (
    _chunk_by,
    _load_named_chunks,
    compress_named,
    run_shell_first,
)
from packages.common.compressed_chunk_cold_probe.types import (
    CONTAINER_COLD,
    CUTOFF,
    LAG_SECONDS,
    WATERMARK,
    WINDOW_STARTS,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    COLD_TABLESPACE_NAME,
    REJECTED_SEQUENCE_NAMES,
    SOURCE_TABLESPACE_NAME,
    CatalogChunk,
    ResidencyGroup,
    classify_eligibility,
    classify_reconciliation,
    classify_residency,
    evaluate_capacity_preflight,
    move_chunk_candidate_sql,
    qualified_ident,
    quote_ident,
)


def probe_rejected_candidates(connection: Any, config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    group = collect_group(connection, chunk)
    origin = next(member for member in group.members if member.kind == "origin_heap")
    compressed = next(member for member in group.members if member.kind == "compressed_heap")
    toast = next(member for member in group.members if member.kind == "toast_heap")
    results: dict[str, Any] = {}

    auto = connect(config, autocommit=True)
    results["move_chunk"] = try_sql(auto, move_chunk_candidate_sql(f"{chunk.origin_schema}.{chunk.origin_name}"))
    auto.close()

    tx = connect(config, autocommit=False)
    rel = qualified_ident(compressed.schema, compressed.name)
    results["direct_compressed_heap_alter"] = try_sql(
        tx,
        f"ALTER TABLE {rel} SET TABLESPACE {quote_ident(COLD_TABLESPACE_NAME)}",
    )
    tx.close()

    tx = connect(config, autocommit=False)
    results["direct_toast_alter"] = try_sql(
        tx,
        f"ALTER TABLE {qualified_ident(toast.schema, toast.name)} SET TABLESPACE {quote_ident(COLD_TABLESPACE_NAME)}",
    )
    tx.close()

    tx = connect(config, autocommit=False)
    steps = []
    origin_reg = f"{chunk.origin_schema}.{chunk.origin_name}"
    steps.append(try_sql(tx, "SELECT decompress_chunk(%s::regclass)::text", (origin_reg,)))
    if steps[-1]["ok"]:
        origin_rel = qualified_ident(origin.schema, origin.name)
        steps.append(
            try_sql(
                tx,
                f"ALTER TABLE {origin_rel} SET TABLESPACE {quote_ident(COLD_TABLESPACE_NAME)}",
            )
        )
        steps.append(try_sql(tx, "SELECT compress_chunk(%s::regclass)::text", (origin_reg,)))
        mid = snapshot_group(collect_group(tx, chunk)) if steps[-1]["ok"] else None
        tx.rollback()
    else:
        mid = None
        tx.rollback()
    tx.close()
    fresh = connect(config)
    restored = collect_group(fresh, chunk)
    fresh.close()
    results["decompress_first"] = {
        "steps": [{k: v for k, v in step.items() if k != "rows"} for step in steps],
        "before_commit": mid,
        "after_rollback_fresh": snapshot_group(restored),
        "rejected_reason": "atomic but expands origin on pg_default before the move",
        "complete": False,
    }

    auto = connect(config, autocommit=True)
    internal = scalar(
        auto,
        """
        SELECT format('%I.%I', compressed.schema_name, compressed.table_name)
        FROM _timescaledb_catalog.hypertable ht
        JOIN _timescaledb_catalog.hypertable compressed ON compressed.id = ht.compressed_hypertable_id
        WHERE ht.schema_name='hydro' AND ht.table_name='river_timeseries'
        """,
    )
    attach = try_sql(
        auto, "SELECT attach_tablespace(%s, %s::regclass, if_not_attached => true)", (COLD_TABLESPACE_NAME, internal)
    )
    later = datetime(2026, 7, 16, tzinfo=UTC)
    execute(
        auto,
        """
        INSERT INTO hydro.river_timeseries (id, valid_time, value, payload)
        SELECT g, %s + (g * interval '1 hour'), 0.1, NULL
        FROM generate_series(0,2) g
        """,
        (later,),
    )
    newest = [item for item in load_chunks(auto) if item.hypertable_name == "river_timeseries"][-1]
    if not newest.is_compressed:
        try_sql(auto, "SELECT compress_chunk(%s::regclass)::text", (f"{newest.origin_schema}.{newest.origin_name}",))
    new_group = collect_group(auto, newest)
    detach = try_sql(
        auto, "SELECT detach_tablespace(%s, %s::regclass, if_attached => true)", (COLD_TABLESPACE_NAME, internal)
    )
    business = execute(
        auto,
        """
        SELECT ts.tablespace_name FROM _timescaledb_catalog.tablespace ts
        JOIN _timescaledb_catalog.hypertable ht ON ht.id = ts.hypertable_id
        WHERE ht.schema_name='hydro' AND ht.table_name='river_timeseries'
        """,
    )
    auto.close()
    results["internal_attach"] = {
        "attach": {k: attach[k] for k in attach if k != "rows"},
        "new_group_residency": classify_residency(new_group.members),
        "business_attached": business,
        "detach": {k: detach[k] for k in detach if k != "rows"},
        "complete": False,
        "rejected_reason": "does not route new compressed chunks per group",
    }

    tx1 = connect(config, autocommit=False)
    two: dict[str, Any] = {"atomic": False}
    decomp = try_sql(
        tx1, "SELECT decompress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",)
    )
    two["decompress"] = {k: decomp[k] for k in decomp if k != "rows"}
    if decomp["ok"]:
        current = collect_group(tx1, chunk)
        origin_now = next(member for member in current.members if member.kind == "origin_heap")
        try_sql(
            tx1,
            "ALTER TABLE "
            + qualified_ident(origin_now.schema, origin_now.name)
            + " SET TABLESPACE "
            + quote_ident(COLD_TABLESPACE_NAME),
        )
        tx1.commit()
        two["tx1_committed"] = True
    else:
        tx1.rollback()
        two["tx1_committed"] = False
    tx1.close()
    tx2 = connect(config, autocommit=False)
    two["tx2"] = {
        k: v
        for k, v in try_sql(
            tx2, "SELECT compress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",)
        ).items()
        if k != "rows"
    }
    if two["tx2"].get("ok"):
        tx2.commit()
    else:
        tx2.rollback()
    tx2.close()
    fresh = connect(config)
    two["after"] = snapshot_group(collect_group(fresh, chunk))
    # restore via shell-first inverse if needed so later tests have a compressed source
    restored_state = collect_group(fresh, chunk)
    fresh.close()
    if classify_residency(restored_state.members) != "all_source" or not restored_state.is_compressed:
        two["restore_inverse"] = restore_source_compressed(config, chunk)
    results["two_transaction"] = two
    return results


def restore_source_compressed(config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    """Best-effort return to compressed all-source for later probe rows."""
    conn = connect(config)
    group = collect_group(conn, chunk)
    if not group.is_compressed:
        try_sql(conn, "SELECT compress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",))
        group = collect_group(conn, chunk)
    conn.close()
    if classify_residency(group.members) == "all_source" and group.is_compressed:
        return {"outcome": "already_source", "reconciliation": "complete_source"}
    inverse = run_shell_first(config, chunk, commit=True, target=SOURCE_TABLESPACE_NAME)
    if inverse.get("reconciliation") == "complete_source":
        return inverse
    conn = connect(config, autocommit=True)
    try:
        if group.is_compressed:
            try_sql(
                conn,
                "SELECT decompress_chunk(%s::regclass)::text",
                (f"{chunk.origin_schema}.{chunk.origin_name}",),
            )
        current = collect_group(conn, chunk)
        origin_now = next(member for member in current.members if member.kind == "origin_heap")
        try_sql(
            conn,
            "ALTER TABLE "
            + qualified_ident(origin_now.schema, origin_now.name)
            + " SET TABLESPACE "
            + quote_ident(SOURCE_TABLESPACE_NAME),
        )
        for member in current.members:
            if member.kind == "index" and member.heap_oid == origin_now.oid:
                try_sql(
                    conn,
                    "ALTER INDEX "
                    + qualified_ident(member.schema, member.name)
                    + " SET TABLESPACE "
                    + quote_ident(SOURCE_TABLESPACE_NAME),
                )
        try_sql(conn, "SELECT compress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",))
        after = collect_group(conn, chunk)
        return {
            "outcome": "forced_source",
            "reconciliation": classify_reconciliation(group, after),
            "after": snapshot_group(after),
        }
    finally:
        conn.close()


def select_sequence(results: Mapping[str, Any]) -> dict[str, Any]:
    shell = results.get("shell_first") or {}
    rejected = sorted(REJECTED_SEQUENCE_NAMES)
    rollback = shell.get("result") or shell
    original = rollback.get("original_sibling")
    if original is None:
        before = ((rollback.get("before") or {}).get("compressed") or {})
        after = ((rollback.get("after") or {}).get("compressed") or {})
        original = before.get("oid") is not None and before.get("oid") == after.get("oid")
    if (
        shell.get("complete")
        and rollback.get("reconciliation") == "complete_source"
        and original is True
        and rollback.get("before_parity") == rollback.get("after_parity")
    ):
        return {
            "accepted": ACCEPTED_SEQUENCE_NAME,
            "rejected": rejected,
            "reason": "shell-first decompress/recompress is the only complete atomic sequence",
            "candidates": results,
        }
    return {
        "accepted": None,
        "rejected": rejected,
        "reason": "shell-first did not prove complete residency plus original-sibling rollback",
        "blocker": True,
        "candidates": results,
    }


def run_lifecycle(connection: Any, config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    before_parity = parity(connection, chunk)
    committed = run_shell_first(config, chunk, commit=True)
    cold = collect_group(connection, chunk)
    cold_parity = parity(connection, chunk)
    already = run_shell_first(config, chunk, commit=True)
    scalar(connection, "SELECT decompress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",))
    decompressed = collect_group(connection, chunk)
    execute(
        connection,
        "INSERT INTO hydro.river_timeseries (id, valid_time, value, payload) VALUES (1000, %s, 42.0, NULL)",
        (CUTOFF - timedelta(hours=12),),
    )
    replay_parity = parity(connection, chunk)
    scalar(connection, "SELECT compress_chunk(%s::regclass)::text", (f"{chunk.origin_schema}.{chunk.origin_name}",))
    recompressed = collect_group(connection, chunk)
    reconverge = None
    if classify_residency(recompressed.members) != "already_target":
        reconverge = run_shell_first(config, chunk, commit=True)
        recompressed = collect_group(connection, chunk)
    move_back = run_shell_first(config, chunk, commit=True, target=SOURCE_TABLESPACE_NAME)
    back = collect_group(connection, chunk)
    drop_before_oids = [member.oid for member in back.members]
    dropped = scalar(
        connection,
        """
        SELECT drop_chunks(
            'hydro.river_timeseries'::regclass,
            older_than => %s::timestamptz,
            newer_than => %s::timestamptz
        )
        """,
        (chunk.range_end, chunk.range_start),
    )
    remaining = execute(
        connection,
        "SELECT table_name FROM _timescaledb_catalog.chunk WHERE NOT dropped AND table_name = %s",
        (chunk.origin_name,),
    )
    remaining_oids = []
    if drop_before_oids:
        remaining_oids = [
            int(row["oid"])
            for row in execute(connection, "SELECT oid FROM pg_class WHERE oid = ANY(%s)", (drop_before_oids,))
        ]
    return {
        "committed_move": committed,
        "cold": snapshot_group(cold),
        "before_parity": before_parity,
        "cold_parity": cold_parity,
        "parity_unchanged_until_replay": before_parity == cold_parity,
        "already_cold": already,
        "decompressed": snapshot_group(decompressed),
        "replay_parity": replay_parity,
        "recompressed": snapshot_group(recompressed),
        "reconverge": reconverge,
        "move_back": move_back,
        "move_back_residency": classify_residency(back.members),
        "drop_chunks": dropped,
        "drop_remaining": remaining,
        "drop_before_oids": drop_before_oids,
        "drop_oids_absent": remaining_oids == [],
        "drop_remaining_oids": remaining_oids,
    }


def run_parity_sentinel(connection: Any, chunk: CatalogChunk) -> dict[str, Any]:
    before = parity(connection, chunk)
    execute(
        connection,
        """
        UPDATE hydro.river_timeseries
        SET value = value + 0.25
        WHERE id = 0 AND valid_time >= %s AND valid_time < %s
        """,
        (chunk.range_start, chunk.range_end),
    )
    after_target = parity(connection, chunk)
    execute(
        connection,
        """
        UPDATE hydro.river_timeseries
        SET value = value - 0.25
        WHERE id = 0 AND valid_time >= %s AND valid_time < %s
        """,
        (chunk.range_start, chunk.range_end),
    )
    restored = parity(connection, chunk)
    execute(
        connection,
        """
        UPDATE hydro.river_timeseries
        SET value = value + 0.25
        WHERE id = 0 AND valid_time >= %s AND valid_time < %s
        """,
        (chunk.range_end, chunk.range_end + timedelta(days=7)),
    )
    after_sibling = parity(connection, chunk)
    execute(
        connection,
        """
        UPDATE hydro.river_timeseries
        SET value = value - 0.25
        WHERE id = 0 AND valid_time >= %s AND valid_time < %s
        """,
        (chunk.range_end, chunk.range_end + timedelta(days=7)),
    )
    return {
        "before": before,
        "after_target_mutation": after_target,
        "restored": restored,
        "after_sibling_compensation": after_sibling,
        "target_mutation_changes_checksum": after_target["checksum"] != before["checksum"],
        "sibling_compensation_does_not_hide": after_sibling["checksum"] == restored["checksum"]
        and after_target["checksum"] != before["checksum"],
    }


def run_boundaries(connection: Any, config: ProbeConfig) -> dict[str, Any]:
    chunks = load_chunks(connection)
    exact = _chunk_by(chunks=chunks, table="river_timeseries", end=CUTOFF)
    empty_candidates = [
        chunk for chunk in chunks if chunk.hypertable_name == "river_timeseries" and chunk.range_end < WINDOW_STARTS[1]
    ]
    empty = empty_candidates[0] if empty_candidates else None
    met_same = _chunk_by(chunks=chunks, table="forcing_station_timeseries", end=CUTOFF)
    hydro_same = exact
    hydro_group = collect_group(connection, hydro_same)
    met_group = collect_group(connection, met_same)
    no_index_chunks = _load_named_chunks(connection, "hydro", "no_index_timeseries")
    if not no_index_chunks:
        raise ProbeError("no-index group is missing")
    no_index = no_index_chunks[0]
    if not no_index.is_compressed:
        compress_named(connection, no_index.origin_schema, no_index.origin_name)
        no_index_chunks = _load_named_chunks(connection, "hydro", "no_index_timeseries")
        no_index = no_index_chunks[0]
    no_index_group = collect_group(connection, no_index)
    later = datetime(2026, 7, 23, tzinfo=UTC)
    execute(
        connection,
        """
        INSERT INTO hydro.river_timeseries (id, valid_time, value, payload)
        VALUES (2000, %s, 1.0, NULL)
        """,
        (later,),
    )
    newest = [item for item in load_chunks(connection) if item.hypertable_name == "river_timeseries"][-1]
    newest_group = collect_group(connection, newest)
    origin_newest = next(member for member in newest_group.members if member.kind == "origin_heap")
    new_chunk_space = origin_newest.tablespace
    return {
        "exact_cutoff_eligibility": classify_eligibility(
            hypertable_schema=exact.hypertable_schema,
            hypertable_name=exact.hypertable_name,
            is_compressed=exact.is_compressed,
            range_end=exact.range_end,
            watermark=WATERMARK,
            lag_seconds=LAG_SECONDS,
        ),
        "empty_chunk": None if empty is None else snapshot_group(collect_group(connection, empty)),
        "no_index_group": snapshot_group(no_index_group),
        "no_index_origin_index_count": sum(
            1
            for member in no_index_group.members
            if member.kind == "index" and member.heap_oid == no_index_group.origin_oid
        ),
        "quoted_numeric_leading_index": any(
            member.name[:1].isdigit() for member in hydro_group.members if member.relkind == "i"
        ),
        "owned_toast_present": any(member.kind in {"toast_heap", "toast_index"} for member in hydro_group.members),
        "same_window_disjoint": set(member.oid for member in hydro_group.members).isdisjoint(
            member.oid for member in met_group.members
        ),
        "new_chunk_tablespace": new_chunk_space,
        "ineligible_newer": classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=datetime(2026, 7, 16, tzinfo=UTC),
            watermark=WATERMARK,
            lag_seconds=LAG_SECONDS,
        ),
        "ineligible_hypertable": classify_eligibility(
            hypertable_schema="other",
            hypertable_name="table",
            is_compressed=True,
            range_end=datetime(2026, 6, 20, tzinfo=UTC),
            watermark=WATERMARK,
            lag_seconds=LAG_SECONDS,
        ),
        "refused_watermark": classify_eligibility(
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            is_compressed=True,
            range_end=datetime(2026, 6, 20, tzinfo=UTC),
            watermark=None,
            lag_seconds=LAG_SECONDS,
        ),
        "attach_tablespace": execute(
            connection,
            """
            SELECT ht.schema_name, ht.table_name, ts.tablespace_name
            FROM _timescaledb_catalog.tablespace ts
            JOIN _timescaledb_catalog.hypertable ht ON ht.id = ts.hypertable_id
            WHERE ts.tablespace_name = %s
            """,
            (COLD_TABLESPACE_NAME,),
        ),
    }


def _observer_payload(
    before: ResidencyGroup,
    observed: Mapping[str, Any],
    before_parity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    after = observed["after"]
    return {
        "reconciliation": observed["reconciliation"],
        "original_sibling": observed["original_sibling"],
        "after": observed["after_snapshot"],
        "after_parity": observed["after_parity"],
        "before": snapshot_group(before),
        "before_parity": before_parity,
        "residency_after": classify_residency(after.members),
    }


def _capacity_evidence(connection: Any, chunk: CatalogChunk) -> dict[str, Any]:
    group = collect_group(connection, chunk)
    return {
        "oids": sorted(member.oid for member in group.members),
        "residency": classify_residency(group.members),
        "sibling": sibling_identity(group),
        "parity": parity(connection, chunk),
    }


def run_capacity_preflight(connection: Any, config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    group = collect_group(connection, chunk)
    stats = compression_stats(connection, chunk)
    expanded = stats["before_compression_total_bytes"]
    retained = retained_source_bytes(group)
    cold_reserve = 1
    wal_reserve = 1
    required_cold = expanded + cold_reserve
    required_hot = wal_reserve
    measured_cold_free = int(shutil.disk_usage(config.work_root / "cold").free)
    measured_hot_free = int(shutil.disk_usage(config.work_root / "pgdata").free)
    positive = evaluate_capacity_preflight(
        before_compression_total_bytes=expanded,
        cold_free_bytes=measured_cold_free,
        cold_reserve_bytes=cold_reserve,
        hot_free_bytes=measured_hot_free,
        wal_reserve_bytes=wal_reserve,
        retained_source_bytes=retained,
    )
    equality = evaluate_capacity_preflight(
        before_compression_total_bytes=expanded,
        cold_free_bytes=required_cold,
        cold_reserve_bytes=cold_reserve,
        hot_free_bytes=required_hot,
        wal_reserve_bytes=wal_reserve,
        retained_source_bytes=retained,
    )
    cold_short_decision = evaluate_capacity_preflight(
        before_compression_total_bytes=expanded,
        cold_free_bytes=required_cold - 1,
        cold_reserve_bytes=cold_reserve,
        hot_free_bytes=measured_hot_free,
        wal_reserve_bytes=wal_reserve,
        retained_source_bytes=retained,
    )
    hot_short_decision = evaluate_capacity_preflight(
        before_compression_total_bytes=expanded,
        cold_free_bytes=measured_cold_free,
        cold_reserve_bytes=cold_reserve,
        hot_free_bytes=0,
        wal_reserve_bytes=wal_reserve,
        retained_source_bytes=retained,
    )
    before_ev = _capacity_evidence(connection, chunk)

    def _negative(decision: Any) -> dict[str, Any]:
        result = run_shell_first(config, chunk, commit=True, capacity_decision=decision)
        after_ev = _capacity_evidence(connection, chunk)
        return {
            **decision.as_dict(),
            "shell_sql_executed": bool(result.get("shell_sql_executed")),
            "oids_unchanged": after_ev["oids"] == before_ev["oids"],
            "residency_unchanged": after_ev["residency"] == before_ev["residency"],
            "original_sibling": after_ev["sibling"] == before_ev["sibling"],
            "parity_unchanged": after_ev["parity"] == before_ev["parity"],
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
        }

    config.capacity_decision = positive
    row = {
        "before_compression_total_bytes": expanded,
        "retained_source_bytes": retained,
        "cold_reserve_bytes": cold_reserve,
        "wal_reserve_bytes": wal_reserve,
        "measured_cold_free_bytes": measured_cold_free,
        "measured_hot_free_bytes": measured_hot_free,
        "positive": positive.as_dict(),
        "equality": equality.as_dict(),
        "cold_short": _negative(cold_short_decision),
        "hot_short": _negative(hot_short_decision),
    }
    config._capacity_preflight_row = row
    return row


def run_failures(connection: Any, config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    stored = getattr(config, "_capacity_preflight_row", None)
    capacity_preflight = stored if isinstance(stored, dict) else run_capacity_preflight(connection, config, chunk)
    before = collect_group(connection, chunk)
    before_parity = parity(connection, chunk)
    before_oids = sorted(member.oid for member in before.members)

    missing_plan = require_migrate_plan(before, target="nhms_cold_missing")
    missing_sql = missing_plan.shell_move_sql[0]
    missing_target_conn = connect(config, autocommit=False)
    missing_exec = try_sql(missing_target_conn, missing_sql)
    missing_target_conn.close()
    missing_obs = fresh_observer(config, before, chunk, before_parity)
    missing_target = {
        "exec": missing_exec,
        "plan_kind": missing_plan.kind,
        "target": "nhms_cold_missing",
        "sql": missing_sql,
        **_observer_payload(before, missing_obs, before_parity),
    }

    mid_shell = run_shell_first(config, chunk, commit=True, inject_after="shell")
    mid_decomp = run_shell_first(config, chunk, commit=True, inject_after="decompress")
    mid_recomp = run_shell_first(config, chunk, commit=True, inject_after="recompress")

    timeout_conn = connect(config, autocommit=False)
    timeout = try_sql(timeout_conn, "SET LOCAL statement_timeout = '1s'")
    timeout_sleep = try_sql(timeout_conn, "SELECT pg_sleep(3)")
    timeout_conn.close()
    timeout_obs = fresh_observer(config, before, chunk, before_parity)
    statement_timeout = {
        "set": timeout,
        "sleep": timeout_sleep,
        **_observer_payload(before, timeout_obs, before_parity),
    }

    holder = connect(config, autocommit=False)
    locker = connect(config, autocommit=False)
    origin = next(member for member in before.members if member.kind == "origin_heap")
    execute(holder, f"LOCK TABLE {qualified_ident(origin.schema, origin.name)} IN ACCESS SHARE MODE")
    lock = try_sql(locker, "SET LOCAL lock_timeout = '1s'")
    lock_block = try_sql(
        locker, f"LOCK TABLE {qualified_ident(origin.schema, origin.name)} IN ACCESS EXCLUSIVE MODE"
    )
    holder.rollback()
    holder.close()
    locker.close()
    lock_obs = fresh_observer(config, before, chunk, before_parity)
    lock_conflict = {
        "set": lock,
        "block": lock_block,
        **_observer_payload(before, lock_obs, before_parity),
    }

    pre_commit_interrupt = run_shell_first(config, chunk, commit=True, inject_after="pre_commit_kill")
    lost_commit = run_shell_first(config, chunk, commit=True, inject_after="lost_ack")
    lost_commit_ack = {
        **lost_commit,
        "committed": lost_commit.get("reconciliation") == "complete_target"
        and lost_commit.get("outcome") == "committed_ack_lost"
        and lost_commit.get("commit_ack_lost") is True,
        "replayed": False,
    }
    if lost_commit_ack["committed"]:
        restore_source_compressed(config, chunk)
        before = collect_group(connection, chunk)
        before_parity = parity(connection, chunk)
        before_oids = sorted(member.oid for member in before.members)

    permission_plan = require_migrate_plan(before, target=COLD_TABLESPACE_NAME)
    permission_sql = permission_plan.shell_move_sql[0]
    limited_role = scalar(connection, "SELECT 1 FROM pg_roles WHERE rolname = 'probe_limited'")
    if not limited_role:
        execute(connection, "CREATE ROLE probe_limited LOGIN PASSWORD 'probe_limited'")
    permission: dict[str, Any]
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        limited = psycopg2.connect(
            host="127.0.0.1",
            port=config.host_port,
            user="probe_limited",
            password="probe_limited",
            dbname="postgres",
            connect_timeout=5,
            cursor_factory=RealDictCursor,
        )
        limited.autocommit = False
        permission = try_sql(limited, permission_sql)
        limited.close()
    except Exception as error:  # noqa: BLE001
        permission = {"ok": False, "error_type": type(error).__name__, "error": str(error).split("\n")[0]}
    permission_obs = fresh_observer(config, before, chunk, before_parity)
    permission.update(_observer_payload(before, permission_obs, before_parity))
    permission["plan_kind"] = permission_plan.kind
    permission["target"] = COLD_TABLESPACE_NAME
    permission["sql"] = permission_sql

    full = _full_target(connection, config, chunk)

    catalog_location = scalar(
        connection,
        "SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = %s",
        (COLD_TABLESPACE_NAME,),
    )
    match = validate_catalog_path_preflight(
        catalog_location=catalog_location,
        expected_location=CONTAINER_COLD,
    )
    mismatch = validate_catalog_path_preflight(
        catalog_location=catalog_location,
        expected_location="/tmp/wrong-cold-path",
    )
    mismatch_obs = fresh_observer(config, before, chunk, before_parity)
    after_oids = sorted(member.oid for member in mismatch_obs["after"].members)
    catalog_path_mismatch = {
        "match": match,
        "mismatch": mismatch,
        "refused": mismatch["refused"] is True and mismatch["ok"] is False,
        "relation_oids_unchanged": after_oids == before_oids,
        "residency_unchanged": classify_residency(mismatch_obs["after"].members) == classify_residency(before.members),
        "parity_unchanged": mismatch_obs["after_parity"] == before_parity,
        "reconciliation": mismatch_obs["reconciliation"],
        "original_sibling": mismatch_obs["original_sibling"],
        "after": mismatch_obs["after_snapshot"],
        "after_parity": mismatch_obs["after_parity"],
        "before_parity": before_parity,
    }

    injected = run_shell_first(
        config, chunk, commit=True, inject_after="shell", inject_sql="SELECT 1 FROM no_such_relation_1892"
    )
    injected_missing_relation_error = {
        **injected,
        "claim": "injected_missing_relation_error",
        "selected_relation_disappeared": False,
    }

    selection_disappearance = run_selection_disappearance(connection, config, chunk)
    false_success = any(
        item.get("reconciliation") == "complete_target" and item.get("error")
        for item in (mid_shell, mid_decomp, mid_recomp, pre_commit_interrupt)
    )
    return {
        "missing_target": missing_target,
        "mid_shell": mid_shell,
        "mid_decompress": mid_decomp,
        "mid_recompress": mid_recomp,
        "statement_timeout": statement_timeout,
        "lock_conflict": lock_conflict,
        "pre_commit_interrupt": pre_commit_interrupt,
        "lost_commit_ack": lost_commit_ack,
        "permission": permission,
        "full_target": full,
        "catalog_path_mismatch": catalog_path_mismatch,
        "injected_missing_relation_error": injected_missing_relation_error,
        "selection_disappearance": selection_disappearance,
        "capacity_preflight": capacity_preflight,
        "false_success": false_success,
    }


def run_selection_disappearance(connection: Any, config: ProbeConfig, witness: CatalogChunk) -> dict[str, Any]:
    sacrificial_start = datetime(2026, 5, 28, tzinfo=UTC)
    execute(
        connection,
        """
        INSERT INTO hydro.river_timeseries (id, valid_time, value, payload)
        SELECT g, %s::timestamptz + (g * interval '1 hour'), 3.0, NULL
        FROM generate_series(0, 5) AS g
        """,
        (sacrificial_start,),
    )
    chunks = load_chunks(connection)
    sacrificial = next(
        item
        for item in chunks
        if item.hypertable_name == "river_timeseries" and item.range_start == sacrificial_start
    )
    if not sacrificial.is_compressed:
        compress_named(connection, sacrificial.origin_schema, sacrificial.origin_name)
        sacrificial = next(
            item
            for item in load_chunks(connection)
            if item.origin_oid == sacrificial.origin_oid or (
                item.hypertable_name == "river_timeseries" and item.range_start == sacrificial_start
            )
        )
    selected = collect_group(connection, sacrificial)
    selected_oids = [member.oid for member in selected.members]
    witness_before = collect_group(connection, witness)
    witness_oids = sorted(member.oid for member in witness_before.members)
    witness_parity = parity(connection, witness)
    dropper = connect(config)
    try:
        scalar(
            dropper,
            """
            SELECT drop_chunks(
                'hydro.river_timeseries'::regclass,
                older_than => %s::timestamptz,
                newer_than => %s::timestamptz
            )
            """,
            (sacrificial.range_end, sacrificial.range_start),
        )
    finally:
        dropper.close()
    try:
        stale = run_shell_first(config, sacrificial, commit=True)
    except Exception as error:  # noqa: BLE001
        stale = {
            "outcome": "blocked",
            "complete": False,
            "reason": str(error).split("\n")[0],
            "error": {"error_type": type(error).__name__, "error": str(error).split("\n")[0]},
            "reconciliation": "unknown",
        }
    remaining_oids = []
    if selected_oids:
        remaining_oids = [
            int(row["oid"])
            for row in execute(connection, "SELECT oid FROM pg_class WHERE oid = ANY(%s)", (selected_oids,))
        ]
    leftover_catalog = execute(
        connection,
        "SELECT table_name FROM _timescaledb_catalog.chunk WHERE NOT dropped AND table_name = %s",
        (sacrificial.origin_name,),
    )
    witness_after = collect_group(connection, witness)
    return {
        "stale_blocked": stale.get("outcome") in {"blocked", "refused"} or stale.get("complete") is False,
        "stale_outcome": stale.get("outcome"),
        "stale_reason": stale.get("reason") or (stale.get("error") or {}).get("error"),
        "sacrificed_group_gone": remaining_oids == [] and leftover_catalog == [],
        "unrelated_unchanged": sorted(member.oid for member in witness_after.members) == witness_oids
        and parity(connection, witness) == witness_parity,
        "before_oids": selected_oids,
        "after_oids_absent": remaining_oids == [],
        "remaining_oids": remaining_oids,
        "leftover_catalog": leftover_catalog,
        "stale": {k: stale.get(k) for k in ("outcome", "reason", "error", "reconciliation", "complete")},
    }


def _full_target(connection: Any, config: ProbeConfig, chunk: CatalogChunk) -> dict[str, Any]:
    """Attempt a bounded ENOSPC on the dedicated 1MiB tmpfs tablespace."""
    fill_error = None
    fill_error_type = None
    try:
        execute(connection, "CREATE TABLE IF NOT EXISTS public.probe_filler (payload text) TABLESPACE probe_full")
        for _ in range(200):
            execute(
                connection,
                """
                INSERT INTO public.probe_filler
                SELECT md5(g::text) || md5((g + 1)::text) || md5((g + 2)::text)
                       || md5((g + 3)::text) || md5((g + 4)::text)
                FROM generate_series(1, 400) AS g
                """,
            )
    except Exception as error:  # noqa: BLE001
        fill_error = str(error).split("\n")[0]
        fill_error_type = type(error).__name__
        try:
            connection.rollback()
        except Exception:
            pass
    before = collect_group(connection, chunk)
    before_parity = parity(connection, chunk)
    plan = require_migrate_plan(before, target="probe_full")
    move_sql = plan.shell_move_sql[0]
    tx = connect(config, autocommit=False)
    move = try_sql(tx, move_sql)
    tx.close()
    observed = fresh_observer(config, before, chunk, before_parity)
    after = observed["after"]
    move_blob = " ".join(
        str(item)
        for item in (move.get("error"), move.get("error_type"))
        if item
    )
    genuine_full = (
        move.get("ok") is False
        and (
            "DiskFull" in move_blob
            or "No space left on device" in move_blob
            or "no space left on device" in move_blob.lower()
        )
    )
    return {
        "fill_error": fill_error,
        "fill_error_type": fill_error_type,
        "genuine_enospc": genuine_full,
        "plan_kind": plan.kind,
        "target": "probe_full",
        "sql": move_sql,
        "move": {k: move[k] for k in move if k != "rows"},
        "reconciliation": observed["reconciliation"],
        "original_sibling": observed["original_sibling"],
        "residency_after": classify_residency(after.members),
        "after": observed["after_snapshot"],
        "after_parity": observed["after_parity"],
        "limitation": None if genuine_full else "move SQL itself did not surface DiskFull/ENOSPC",
    }
