"""Accepted shell-first sequence execution and disposable-schema bootstrap."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.compressed_chunk_cold_probe.catalog import (
    _as_capacity_decision,
    _aware,
    _relation_oid,
    collect_group,
    fresh_observer,
    parity,
    snapshot_group,
    wal_lsn,
)
from packages.common.compressed_chunk_cold_probe.cluster import (
    connect,
    execute,
    scalar,
    validate_catalog_path_preflight,
)
from packages.common.compressed_chunk_cold_probe.types import (
    COMPRESSED_SIBLING_SQL,
    CONTAINER_COLD,
    CONTAINER_FULL,
    WAL_LIMITATION,
    WINDOW_STARTS,
    CommitAckLost,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    ALLOWED_HYPERTABLES,
    COLD_TABLESPACE_NAME,
    SOURCE_TABLESPACE_NAME,
    CatalogChunk,
    build_shell_first_plan,
    quote_ident,
    quote_literal,
)


class _AckLossConnection:
    """Moving-connection wrapper that loses the commit acknowledgement after COMMIT."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._unusable = False
        self.commit_ack_lost = False

    def __getattr__(self, name: str) -> Any:
        if self._unusable:
            raise ProbeError("moving connection is unusable after lost commit acknowledgement")
        return getattr(self._connection, name)

    def commit(self) -> None:
        if self._unusable:
            raise ProbeError("moving connection is unusable after lost commit acknowledgement")
        self._connection.commit()
        self.commit_ack_lost = True
        self._unusable = True
        try:
            self._connection.close()
        except Exception:
            pass
        raise CommitAckLost("commit acknowledgement lost after server commit")

    def rollback(self) -> None:
        if self._unusable:
            raise ProbeError("moving connection is unusable after lost commit acknowledgement")
        self._connection.rollback()

    def close(self) -> None:
        if self._unusable:
            return
        self._connection.close()


def _expanded_member_proof(snapshot: Mapping[str, Any] | None, *, target: str) -> dict[str, Any]:
    members = list((snapshot or {}).get("members") or [])
    if not members:
        return {
            "target": target,
            "all_requested_target": False,
            "pg_default_bytes": None,
            "member_count": 0,
        }
    pg_default_bytes = 0
    all_target = True
    for member in members:
        tablespace = member.get("tablespace")
        if tablespace != target:
            all_target = False
        if tablespace == SOURCE_TABLESPACE_NAME:
            pg_default_bytes += int(member.get("bytes") or 0)
    return {
        "target": target,
        "all_requested_target": all_target,
        "pg_default_bytes": pg_default_bytes,
        "member_count": len(members),
    }


def run_shell_first(
    config: ProbeConfig,
    chunk: CatalogChunk,
    *,
    commit: bool,
    target: str = COLD_TABLESPACE_NAME,
    inject_after: str | None = None,
    inject_sql: str | None = None,
    capacity_decision: Any = None,
) -> dict[str, Any]:
    observer = connect(config)
    before = collect_group(observer, chunk)
    before_parity = parity(observer, chunk)
    before_wal = wal_lsn(observer)
    observer.close()
    source = SOURCE_TABLESPACE_NAME if target == COLD_TABLESPACE_NAME else COLD_TABLESPACE_NAME
    plan = build_shell_first_plan(
        before,
        target=target,
        source=source,
        lock_timeout=config.lock_timeout,
        statement_timeout=config.statement_timeout,
    )
    if plan.kind != "migrate":
        observed = fresh_observer(config, before, chunk, before_parity, source=source, target=target)
        return {
            "outcome": plan.kind,
            "reason": plan.reason,
            "plan": {"kind": plan.kind},
            "before": snapshot_group(before),
            "before_parity": before_parity,
            "after": observed["after_snapshot"],
            "after_parity": observed["after_parity"],
            "reconciliation": observed["reconciliation"],
            "complete": False,
            "rolled_back_fresh": observed["reconciliation"] == "complete_source"
            and observed["original_sibling"] is True,
            "shell_sql_executed": False,
        }
    if target == COLD_TABLESPACE_NAME:
        locator = connect(config)
        try:
            catalog_location = scalar(
                locator,
                "SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = %s",
                (target,),
            )
        finally:
            locator.close()
        preflight = validate_catalog_path_preflight(
            catalog_location=catalog_location,
            expected_location=CONTAINER_COLD,
        )
        if not preflight["ok"]:
            observed = fresh_observer(config, before, chunk, before_parity, source=source, target=target)
            return {
                "outcome": "refused",
                "reason": preflight.get("error"),
                "catalog_path_preflight": preflight,
                "before": snapshot_group(before),
                "before_parity": before_parity,
                "after": observed["after_snapshot"],
                "after_parity": observed["after_parity"],
                "reconciliation": observed["reconciliation"],
                "original_sibling": observed["original_sibling"],
                "complete": False,
                "rolled_back_fresh": observed["reconciliation"] == "complete_source",
                "replayed": False,
                "shell_sql_executed": False,
            }
    decision = _as_capacity_decision(capacity_decision if capacity_decision is not None else config.capacity_decision)
    if decision is None or not decision.approved:
        observed = fresh_observer(config, before, chunk, before_parity, source=source, target=target)
        blockers = () if decision is None else decision.blockers
        return {
            "outcome": "refused",
            "reason": "capacity preflight refused" if decision is not None else "capacity preflight missing",
            "capacity_decision": None if decision is None else decision.as_dict(),
            "capacity_blockers": list(blockers),
            "shell_sql_executed": False,
            "before": snapshot_group(before),
            "before_parity": before_parity,
            "after": observed["after_snapshot"],
            "after_parity": observed["after_parity"],
            "reconciliation": observed["reconciliation"],
            "original_sibling": observed["original_sibling"],
            "complete": False,
            "rolled_back_fresh": observed["reconciliation"] == "complete_source",
            "replayed": False,
        }
    raw_tx = connect(config, autocommit=False)
    tx: Any = _AckLossConnection(raw_tx) if inject_after == "lost_ack" else raw_tx
    steps: list[dict[str, Any]] = []
    phases: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    commit_ack_lost = False
    try:
        for statement in (*plan.prefix_sql[1:], *plan.lock_sql, *plan.shell_move_sql):
            execute(tx, statement)
            steps.append({"ok": True, "sql": statement})
            if inject_after == "shell" and statement == plan.shell_move_sql[-1]:
                execute(tx, inject_sql or "SELECT 1 FROM no_such_table_1892")
        phases["after_shell"] = snapshot_group(collect_group(tx, chunk))
        if plan.decompress_sql:
            execute(tx, plan.decompress_sql)
            steps.append({"ok": True, "sql": plan.decompress_sql})
        phases["after_decompress"] = snapshot_group(collect_group(tx, chunk))
        if inject_after == "decompress":
            execute(tx, inject_sql or "SELECT 1 FROM no_such_table_1892")
        if inject_after == "pre_commit_kill":
            pid = scalar(tx, "SELECT pg_backend_pid()")
            killer = connect(config)
            try:
                scalar(killer, "SELECT pg_terminate_backend(%s)", (pid,))
            finally:
                killer.close()
            execute(tx, "SELECT 1")
        if plan.compress_sql:
            execute(tx, plan.compress_sql)
            steps.append({"ok": True, "sql": plan.compress_sql})
        phases["after_recompress"] = snapshot_group(collect_group(tx, chunk))
        if inject_after == "recompress":
            execute(tx, inject_sql or "SELECT 1 FROM no_such_table_1892")
        if commit:
            tx.commit()
            outcome = "committed"
        else:
            tx.rollback()
            outcome = "rolled_back"
    except CommitAckLost as exc:
        commit_ack_lost = True
        outcome = "unknown"
        error = {"error_type": type(exc).__name__, "error": str(exc).split("\n")[0]}
    except Exception as exc:
        try:
            tx.rollback()
        except Exception:
            pass
        outcome = "rolled_back"
        error = {"error_type": type(exc).__name__, "error": str(exc).split("\n")[0]}
    finally:
        try:
            tx.close()
        except Exception:
            pass
    observed = fresh_observer(config, before, chunk, before_parity, source=source, target=target)
    after = observed["after"]
    after_parity = observed["after_parity"]
    recon = observed["reconciliation"]
    observer = connect(config)
    after_wal = wal_lsn(observer)
    observer.close()
    if commit_ack_lost:
        if recon == "complete_target" and observed["original_sibling"] is False:
            outcome = "committed_ack_lost"
        else:
            outcome = "unknown"
    after_decompress_proof = _expanded_member_proof(phases.get("after_decompress"), target=target)
    return {
        "outcome": outcome,
        "error": error,
        "reconciliation": recon,
        "before": snapshot_group(before),
        "before_parity": before_parity,
        "after": snapshot_group(after),
        "after_parity": after_parity,
        "phases": phases,
        "steps": steps,
        "wal": {"before": before_wal, "after": after_wal, "limitation": WAL_LIMITATION},
        "after_decompress_proof": after_decompress_proof,
        "expanded_bytes_in_pg_default_after_decompress": (
            int(after_decompress_proof["pg_default_bytes"] or 0) > 0
        ),
        "original_sibling": observed["original_sibling"],
        "complete": (
            recon == "complete_target"
            if commit and not commit_ack_lost and error is None
            else recon == "complete_source" and observed["original_sibling"] is True
        )
        and (error is None if commit and not commit_ack_lost else True),
        "rolled_back_fresh": recon == "complete_source"
        and observed["original_sibling"] is True
        and not (commit and error is None and not commit_ack_lost),
        "replayed": False,
        "commit_ack_lost": commit_ack_lost,
        "shell_sql_executed": bool(steps),
        "capacity_decision": decision.as_dict(),
        "target": target,
    }


def bootstrap_extension(connection: Any) -> None:
    execute(connection, "CREATE EXTENSION IF NOT EXISTS timescaledb")


def bootstrap_schema(connection: Any) -> None:
    execute(
        connection, f"CREATE TABLESPACE {quote_ident(COLD_TABLESPACE_NAME)} LOCATION {quote_literal(CONTAINER_COLD)}"
    )
    execute(connection, "CREATE TABLESPACE probe_full LOCATION " + quote_literal(CONTAINER_FULL))
    execute(connection, "CREATE SCHEMA hydro")
    execute(connection, "CREATE SCHEMA met")
    for schema, table, extra_index in (
        ("hydro", "river_timeseries", True),
        ("met", "forcing_station_timeseries", False),
    ):
        qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
        execute(
            connection,
            f"""
            CREATE TABLE {qualified} (
                id integer NOT NULL,
                valid_time timestamptz NOT NULL,
                value double precision NOT NULL,
                payload text,
                PRIMARY KEY (id, valid_time)
            )
            """,
        )
        execute(
            connection,
            "SELECT create_hypertable(%s, 'valid_time', chunk_time_interval => interval '7 days')",
            (f"{schema}.{table}",),
        )
        execute(
            connection,
            f"""
            ALTER TABLE {qualified} SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'id',
                timescaledb.compress_orderby = 'valid_time'
            )
            """,
        )
        if extra_index:
            execute(connection, f"CREATE INDEX {quote_ident('10_probe_extra_idx')} ON {qualified} (value, valid_time)")
    execute(
        connection,
        """
        CREATE TABLE hydro.no_index_timeseries (
            id integer NOT NULL,
            valid_time timestamptz NOT NULL,
            value double precision NOT NULL,
            payload text
        )
        """,
    )
    execute(
        connection,
        """
        SELECT create_hypertable(
            'hydro.no_index_timeseries',
            'valid_time',
            chunk_time_interval => interval '7 days',
            create_default_indexes => false
        )
        """,
    )
    execute(
        connection,
        """
        ALTER TABLE hydro.no_index_timeseries SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'id',
            timescaledb.compress_orderby = 'valid_time'
        )
        """,
    )
    seed_sql = """
    INSERT INTO {table} (id, valid_time, value, payload)
    SELECT g, %s::timestamptz + (g * interval '1 hour'), g * 0.5,
           CASE WHEN %s THEN repeat('T', 8000) ELSE NULL END
    FROM generate_series(0, 23) AS g
    """
    for schema, table in ALLOWED_HYPERTABLES:
        qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
        for start in WINDOW_STARTS:
            execute(
                connection,
                seed_sql.format(table=qualified),
                (start, schema == "hydro" and start in {WINDOW_STARTS[0], WINDOW_STARTS[1]}),
            )
    execute(
        connection,
        seed_sql.format(table="hydro.no_index_timeseries"),
        (WINDOW_STARTS[1], False),
    )
    empty_start = datetime(2026, 6, 11, tzinfo=UTC)
    execute(
        connection,
        "INSERT INTO hydro.river_timeseries (id, valid_time, value, payload) VALUES (999, %s, 1.0, NULL)",
        (empty_start,),
    )
    execute(
        connection,
        "DELETE FROM hydro.river_timeseries WHERE valid_time >= %s AND valid_time < %s",
        (empty_start, empty_start + timedelta(days=7)),
    )


def _load_named_chunks(connection: Any, schema: str, table: str) -> list[CatalogChunk]:
    rows = execute(
        connection,
        """
        SELECT hypertable_schema, hypertable_name, chunk_schema, chunk_name,
               range_start, range_end, is_compressed
        FROM timescaledb_information.chunks
        WHERE hypertable_schema = %s AND hypertable_name = %s
        ORDER BY range_end
        """,
        (schema, table),
    )
    chunks: list[CatalogChunk] = []
    for row in rows:
        origin_oid = _relation_oid(connection, row["chunk_schema"], row["chunk_name"])
        if origin_oid is None:
            raise ProbeError(f"origin oid missing for {row['chunk_schema']}.{row['chunk_name']}")
        sibling = execute(connection, COMPRESSED_SIBLING_SQL, (row["chunk_schema"], row["chunk_name"]))
        compressed_schema = sibling[0]["schema_name"] if sibling else None
        compressed_name = sibling[0]["table_name"] if sibling else None
        compressed_oid = None
        if compressed_schema and compressed_name:
            compressed_oid = _relation_oid(connection, compressed_schema, compressed_name)
        chunks.append(
            CatalogChunk(
                hypertable_schema=row["hypertable_schema"],
                hypertable_name=row["hypertable_name"],
                origin_oid=origin_oid,
                origin_schema=row["chunk_schema"],
                origin_name=row["chunk_name"],
                compressed_oid=compressed_oid,
                compressed_schema=compressed_schema,
                compressed_name=compressed_name,
                range_start=_aware(row["range_start"]),
                range_end=_aware(row["range_end"]),
                is_compressed=bool(row["is_compressed"]),
            )
        )
    return chunks


def compress_named(connection: Any, schema: str, name: str) -> str:
    return str(scalar(connection, "SELECT compress_chunk(%s::regclass)::text", (f"{schema}.{name}",)))


def _chunk_by(*, chunks: Sequence[CatalogChunk], table: str, end: datetime) -> CatalogChunk:
    for chunk in chunks:
        if chunk.hypertable_name == table and chunk.range_end == end:
            return chunk
    raise ProbeError(f"missing chunk {table} ending {end.isoformat()}")
