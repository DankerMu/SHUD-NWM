"""Production owner for TimescaleDB 2.10.2 compressed-chunk cold residency.

Consumes the #1892 pure contract and executes exactly
``shell_first_decompress_recompress_atomic``. Production code never imports
``compressed_chunk_cold_probe``.

The target-preflight identity contract (#1929) is owned by
``compressed_chunk_cold_runtime_target``; the names it exports are re-exported
here unchanged so every existing ``from ...compressed_chunk_cold_runtime import``
site keeps working with no wrapper and no duplicated logic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    PINNED_PG_VERSION_PREFIX,
    PINNED_TIMESCALEDB_VERSION,
    CatalogChunk,
    ResidencyGroup,
    ShellFirstPlan,
    build_shell_first_plan,
    classify_eligibility,
    classify_reconciliation,
    classify_residency,
    evaluate_capacity_preflight,
    expanded_uncompressed_group_is_complete,
    recompressed_group_is_complete,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    ColdRuntimeError,
    WindowParity,
    collect_residency_group,
    compression_before_bytes,
    compute_window_parity,
    derive_bound_inventories,
    engine_versions,
    load_catalog_chunk,
    lock_allowlisted_parents,
    ranked_candidates_from_execute,
    retained_source_bytes,
    snapshot_group,
)

# Compatibility surface: the target-preflight owner below is the single
# definition site, and these names stay importable from this module for the
# existing consumers (scripts/node27_cold_residency.py imports the fixed
# identity constants; the runtime/CLI/test suites import the four preflight
# names). Re-exports only — no wrapper functions, no duplicated logic.
from packages.common.compressed_chunk_cold_runtime_target import (  # noqa: F401
    CONTAINER_COLD_PATH,
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_STATEMENT_TIMEOUT,
    HOST_COLD_PATH,
    LIVE_CONTAINER_NAME,
    RuntimeConfig,
    TargetIdentity,
    preflight_target_identity,
    require_runtime_exec_identity,
)
from packages.common.compressed_chunk_cold_runtime_timing import (
    MoveObservation,
    StageTimer,
    build_move_observation,
    inspect_timing_payload,
)

Connect = Callable[..., Any]


class CommitAckLost(ColdRuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "commit acknowledgement lost after server commit",
            error_class="commit_ack_lost",
            stage="commit",
        )


class _AckLossConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._unusable = False
        self.commit_ack_lost = False

    def __getattr__(self, name: str) -> Any:
        self._require_usable()
        return getattr(self._connection, name)

    def _require_usable(self) -> None:
        if self._unusable:
            raise ColdRuntimeError("moving connection is unusable after lost commit acknowledgement")

    def commit(self) -> None:
        self._require_usable()
        self._connection.commit()
        self.commit_ack_lost = True
        self._unusable = True
        try:
            self._connection.close()
        except Exception:
            pass
        raise CommitAckLost()

    def rollback(self) -> None:
        self._require_usable()
        self._connection.rollback()

    def close(self) -> None:
        if not self._unusable:
            self._connection.close()


def _mapping_rows(cursor: Any) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    names = [item[0] for item in cursor.description]
    converted: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        converted.append(dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False)))
    return converted


def execute_on(connection: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _mapping_rows(cursor)


def bind_execute(connection: Any) -> Callable[..., list[Mapping[str, Any]]]:
    return lambda sql, params=None: execute_on(connection, sql, params)


def _binder(connection: Any) -> Callable[..., list[Mapping[str, Any]]]:
    return bind_execute(connection)


def _close(connection: Any | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        pass


def assert_engine_versions(server_version: str, timescaledb_version: str) -> None:
    if not str(server_version).startswith(PINNED_PG_VERSION_PREFIX):
        raise ColdRuntimeError(
            f"PostgreSQL version {server_version} is not {PINNED_PG_VERSION_PREFIX}",
            error_class="engine_identity",
            stage="preflight",
        )
    if str(timescaledb_version) != PINNED_TIMESCALEDB_VERSION:
        raise ColdRuntimeError(
            f"TimescaleDB version {timescaledb_version} is not {PINNED_TIMESCALEDB_VERSION}",
            error_class="engine_identity",
            stage="preflight",
        )


def load_inventories(connection: Any) -> BoundInventories:
    return derive_bound_inventories(_binder(connection))


def _reload_chunk(execute: Callable[..., list[Mapping[str, Any]]], chunk: CatalogChunk) -> CatalogChunk:
    return load_catalog_chunk(
        execute,
        hypertable_schema=chunk.hypertable_schema,
        hypertable_name=chunk.hypertable_name,
        origin_schema=chunk.origin_schema,
        origin_name=chunk.origin_name,
    )


def _parity_for(
    execute: Callable[..., list[Mapping[str, Any]]],
    inventories: BoundInventories,
    chunk: CatalogChunk,
) -> WindowParity:
    inventory = inventories.for_hypertable(chunk.hypertable_schema, chunk.hypertable_name)
    return compute_window_parity(
        execute,
        inventory,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
    )


def _member_identity(group: ResidencyGroup) -> tuple[tuple[int, str, str, str, str, int | None, int | None, str], ...]:
    return tuple(
        (
            member.oid,
            member.kind,
            member.schema,
            member.name,
            member.relkind,
            member.heap_oid,
            member.toast_oid,
            member.tablespace,
        )
        for member in group.members
    )


def _require_same_group(expected: ResidencyGroup, actual: ResidencyGroup) -> None:
    if actual.durable_identity != expected.durable_identity:
        raise ColdRuntimeError("durable identity drifted under lock", error_class="selection_race", stage="revalidate")
    if actual.compressed_oid != expected.compressed_oid or actual.compressed_name != expected.compressed_name:
        raise ColdRuntimeError(
            "compressed sibling drifted under lock",
            error_class="selection_race",
            stage="revalidate",
        )
    if _member_identity(actual) != _member_identity(expected):
        raise ColdRuntimeError("group member map drifted under lock", error_class="selection_race", stage="revalidate")


def _require_complete_group(group: ResidencyGroup, *, max_members: int) -> None:
    if group.blocker:
        raise ColdRuntimeError(group.blocker, error_class="inventory_drift", stage="group")
    if not group.members:
        raise ColdRuntimeError("residency group is empty", error_class="inventory_drift", stage="group")
    if len(group.members) > max_members:
        raise ColdRuntimeError("group exceeds maximum member count", error_class="max_members", stage="group")


def _capacity_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(
        expected.get(key) == actual.get(key)
        for key in (
            "approved",
            "before_compression_total_bytes",
            "retained_source_bytes",
            "cold_free_bytes",
            "cold_reserve_bytes",
            "required_cold_bytes",
            "hot_free_bytes",
            "wal_reserve_bytes",
            "required_hot_bytes",
        )
    )


def _revalidate_locked(
    execute: Callable[..., list[Mapping[str, Any]]],
    *,
    selected: CatalogChunk,
    inventories: BoundInventories,
    before: ResidencyGroup,
    before_parity: WindowParity,
    watermark: datetime,
    lag_seconds: int,
    max_members: int,
) -> tuple[CatalogChunk, ResidencyGroup, WindowParity]:
    current = _reload_chunk(execute, selected)
    if any(
        getattr(current, field) != getattr(selected, field)
        for field in ("origin_oid", "range_start", "range_end", "hypertable_schema", "hypertable_name")
    ):
        raise ColdRuntimeError("durable identity drifted under lock", error_class="selection_race", stage="revalidate")
    eligibility = classify_eligibility(
        hypertable_schema=current.hypertable_schema,
        hypertable_name=current.hypertable_name,
        is_compressed=current.is_compressed,
        range_end=current.range_end,
        watermark=watermark,
        lag_seconds=lag_seconds,
    )
    if eligibility != "eligible":
        raise ColdRuntimeError(
            f"locked eligibility is {eligibility}",
            error_class="selection_race",
            stage="revalidate",
        )
    lock_allowlisted_parents(execute)
    locked_inventories = derive_bound_inventories(execute)
    if locked_inventories.as_payload() != inventories.as_payload():
        raise ColdRuntimeError("inventory drifted under lock", error_class="inventory_drift", stage="revalidate")
    group = collect_residency_group(execute, current)
    _require_complete_group(group, max_members=max_members)
    _require_same_group(before, group)
    locked_parity = _parity_for(execute, locked_inventories, current)
    if locked_parity.as_dict() != before_parity.as_dict():
        raise ColdRuntimeError("window parity drifted under lock", error_class="parity", stage="revalidate")
    return current, group, locked_parity


def _fresh_observer(
    connect: Connect,
    before: ResidencyGroup,
    chunk: CatalogChunk,
    before_parity: WindowParity | None,
    inventories: BoundInventories,
    config: RuntimeConfig,
) -> dict[str, Any]:
    del config
    fresh = connect()
    try:
        execute = _binder(fresh)
        try:
            after_chunk = _reload_chunk(execute, chunk)
            after = collect_residency_group(execute, after_chunk)
        except ColdRuntimeError:
            return {"after": None, "after_parity": None, "reconciliation": "unknown"}
        try:
            live_inventories = derive_bound_inventories(execute)
        except ColdRuntimeError:
            live_inventories = None
        if live_inventories is None or live_inventories.as_payload() != inventories.as_payload():
            return {"after": after, "after_parity": None, "reconciliation": "unknown"}
        after_parity = _parity_for(execute, live_inventories, after_chunk)
        recon = classify_reconciliation(
            before,
            after,
            before_parity=None if before_parity is None else before_parity.as_dict(),
            after_parity=after_parity.as_dict(),
        )
        return {"after": after, "after_parity": after_parity, "reconciliation": recon}
    finally:
        _close(fresh)


def _observation(**kwargs: Any) -> MoveObservation:
    return build_move_observation(**kwargs)


def _sqlstate(error: BaseException) -> str | None:
    value = getattr(error, "pgcode", None)
    return str(value) if value else None


def _classify_sql_error(error: BaseException, *, stage: str) -> tuple[str, str]:
    state = _sqlstate(error)
    name = type(error).__name__
    if state == "55P03" or "lock timeout" in str(error).lower():
        return "lock_timeout", stage
    if state == "57014" or "statement timeout" in str(error).lower() or "querycanceled" in name.lower():
        return "statement_timeout", stage
    if state == "42P01" or "undefinedtable" in name.lower():
        return "relation_disappeared", stage
    if "undefinedobject" in name.lower():
        return "target_identity", stage
    return "runtime", stage


def inspect_residency_group(
    *,
    connect: Connect,
    chunk: CatalogChunk,
    inventories: BoundInventories,
    config: RuntimeConfig | None = None,
) -> MoveObservation:
    """Read-only group classification. Never issues movement SQL."""

    runtime = config or RuntimeConfig()
    started = runtime.clock()
    observer = connect()
    try:
        execute = _binder(observer)
        current = _reload_chunk(execute, chunk)
        before = collect_residency_group(execute, current)
        _require_complete_group(before, max_members=runtime.max_members)
        before_parity = _parity_for(execute, inventories, current)
        plan = build_shell_first_plan(
            before,
            lock_timeout=runtime.lock_timeout,
            statement_timeout=runtime.statement_timeout,
        )
        inspect_timing = inspect_timing_payload(started, runtime.clock())
        if plan.kind == "already_cold":
            return _observation(
                outcome="already_cold",
                reconciliation="complete_target",
                plan_kind=plan.kind,
                shell_sql_executed=False,
                before=before,
                after=before,
                before_parity=before_parity,
                after_parity=before_parity,
                timing=inspect_timing,
            )
        if plan.kind != "migrate":
            recon = "mixed" if classify_residency(before.members) == "mixed" else "unknown"
            return _observation(
                outcome="blocked",
                reconciliation=recon,
                plan_kind=plan.kind,
                shell_sql_executed=False,
                before=before,
                after=before,
                before_parity=before_parity,
                after_parity=before_parity,
                error_class="mixed" if recon == "mixed" else "unknown",
                stage="plan",
                reason=plan.reason,
                timing=inspect_timing,
            )
        return _observation(
            outcome="planned",
            reconciliation="complete_source",
            plan_kind="migrate",
            shell_sql_executed=False,
            before=before,
            after=before,
            before_parity=before_parity,
            after_parity=before_parity,
            timing=inspect_timing,
        )
    finally:
        _close(observer)


def migrate_residency_group(
    *,
    connect: Connect,
    chunk: CatalogChunk,
    inventories: BoundInventories,
    watermark: datetime,
    lag_seconds: int,
    cold_free_bytes: int,
    hot_free_bytes: int,
    cold_reserve_bytes: int,
    wal_reserve_bytes: int,
    config: RuntimeConfig | None = None,
    lose_commit_ack: bool = False,
    expected_before: ResidencyGroup | None = None,
    expected_before_parity: WindowParity | None = None,
    expected_capacity: Mapping[str, Any] | None = None,
) -> MoveObservation:
    """Execute the sole accepted shell-first sequence, or refuse before movement SQL."""

    if ACCEPTED_SEQUENCE_NAME != "shell_first_decompress_recompress_atomic":
        raise ColdRuntimeError("accepted sequence drifted", error_class="sequence", stage="plan")
    runtime = config or RuntimeConfig()
    # #1929: an invalid expected principal is a config refusal, not a mid-flight
    # movement failure — validate before the observer connection opens.
    require_runtime_exec_identity(runtime)
    started = runtime.clock()
    before: ResidencyGroup | None = None
    before_parity: WindowParity | None = None
    current: CatalogChunk | None = None
    capacity = None
    observer = connect()
    try:
        execute = _binder(observer)
        server, timescale = engine_versions(execute)
        assert_engine_versions(server, timescale)
        current = _reload_chunk(execute, chunk)
        before = collect_residency_group(execute, current)
        _require_complete_group(before, max_members=runtime.max_members)
        before_parity = _parity_for(execute, inventories, current)
        if expected_before is not None:
            _require_same_group(expected_before, before)
        if expected_before_parity is not None and before_parity.as_dict() != expected_before_parity.as_dict():
            raise ColdRuntimeError(
                "observer preflight parity drifted from persisted intent",
                error_class="selection_race",
                stage="preflight",
            )
        plan = build_shell_first_plan(
            before,
            lock_timeout=runtime.lock_timeout,
            statement_timeout=runtime.statement_timeout,
        )
        inspect_timing = inspect_timing_payload(started, runtime.clock())
        if plan.kind == "already_cold":
            return _observation(
                outcome="already_cold",
                reconciliation="complete_target",
                plan_kind=plan.kind,
                shell_sql_executed=False,
                before=before,
                after=before,
                before_parity=before_parity,
                after_parity=before_parity,
                timing=inspect_timing,
            )
        if plan.kind != "migrate":
            recon = "mixed" if classify_residency(before.members) == "mixed" else "unknown"
            return _observation(
                outcome="blocked",
                reconciliation=recon,
                plan_kind=plan.kind,
                shell_sql_executed=False,
                before=before,
                after=before,
                before_parity=before_parity,
                after_parity=before_parity,
                error_class="mixed" if recon == "mixed" else "unknown",
                stage="plan",
                reason=plan.reason,
                timing=inspect_timing,
            )
        preflight_target_identity(execute, runtime)
        before_bytes = compression_before_bytes(execute, current)
        capacity = evaluate_capacity_preflight(
            before_compression_total_bytes=before_bytes,
            cold_free_bytes=cold_free_bytes,
            cold_reserve_bytes=cold_reserve_bytes,
            hot_free_bytes=hot_free_bytes,
            wal_reserve_bytes=wal_reserve_bytes,
            retained_source_bytes=retained_source_bytes(before),
        )
        if expected_capacity is not None and not _capacity_matches(expected_capacity, capacity.as_dict()):
            raise ColdRuntimeError(
                "observer preflight capacity drifted from persisted intent",
                error_class="capacity",
                stage="preflight",
            )
        if not capacity.approved:
            return _observation(
                outcome="refused",
                reconciliation="complete_source",
                plan_kind=plan.kind,
                shell_sql_executed=False,
                before=before,
                after=before,
                before_parity=before_parity,
                after_parity=before_parity,
                capacity=capacity.as_dict(),
                error_class="capacity",
                stage="capacity",
                reason="; ".join(capacity.blockers),
                timing=inspect_timing,
            )
    except ColdRuntimeError as error:
        if expected_before is None:
            raise
        dummy = expected_before
        dummy_parity = expected_before_parity
        return _observation(
            outcome="blocked",
            reconciliation="unknown" if error.error_class != "capacity" else "complete_source",
            plan_kind="blocked",
            shell_sql_executed=False,
            before=dummy,
            after=dummy,
            before_parity=dummy_parity,
            after_parity=dummy_parity,
            capacity=None if expected_capacity is None else dict(expected_capacity),
            error_class=error.error_class,
            stage=error.stage,
            reason=str(error),
            timing=inspect_timing_payload(started, runtime.clock()),
        )
    finally:
        _close(observer)
    if current is None or before is None or before_parity is None or capacity is None:
        raise ColdRuntimeError("preflight did not bind a source preimage", error_class="runtime", stage="preflight")

    return _run_shell_first(
        connect=connect,
        chunk=current,
        before=before,
        before_parity=before_parity,
        plan=plan,
        inventories=inventories,
        watermark=watermark,
        lag_seconds=lag_seconds,
        capacity=capacity.as_dict(),
        runtime=runtime,
        lose_commit_ack=lose_commit_ack,
    )


def _run_shell_first(
    *,
    connect: Connect,
    chunk: CatalogChunk,
    before: ResidencyGroup,
    before_parity: WindowParity,
    plan: ShellFirstPlan,
    inventories: BoundInventories,
    watermark: datetime,
    lag_seconds: int,
    capacity: Mapping[str, Any],
    runtime: RuntimeConfig,
    lose_commit_ack: bool,
) -> MoveObservation:
    raw = connect()
    try:
        raw.autocommit = False
    except Exception:
        pass
    tx: Any = _AckLossConnection(raw) if lose_commit_ack else raw
    intermediate: dict[str, Any] = {}
    commit_ack_lost = False
    error_class: str | None = None
    stage = "begin_timeouts"
    shell_sql_executed = False
    timer = StageTimer(runtime.clock)
    try:
        execute = _binder(tx)
        for statement in plan.prefix_sql[1:]:
            execute(statement)
        stage = "lock_heaps"
        timer.start("heap_lock_wait_ms")
        for statement in plan.lock_sql:
            execute(statement)
        timer.stop()
        stage = "revalidate"
        timer.start("revalidation_ms")
        locked_chunk, locked_group, _locked_parity = _revalidate_locked(
            execute,
            selected=chunk,
            inventories=inventories,
            before=before,
            before_parity=before_parity,
            watermark=watermark,
            lag_seconds=lag_seconds,
            max_members=runtime.max_members,
        )
        timer.stop()
        if classify_residency(locked_group.members) != "all_source":
            raise ColdRuntimeError(
                f"locked group residency is {classify_residency(locked_group.members)}",
                error_class="selection_race",
                stage="revalidate",
            )
        stage = "move_origin_shell_and_indexes"
        timer.start("shell_move_ms")
        for statement in plan.shell_move_sql:
            shell_sql_executed = True
            execute(statement)
        timer.stop()
        intermediate["after_shell"] = snapshot_group(collect_residency_group(execute, locked_chunk))
        stage = "decompress"
        timer.start("decompress_ms")
        if plan.decompress_sql:
            execute(plan.decompress_sql)
        expanded_chunk = _reload_chunk(execute, locked_chunk)
        expanded = collect_residency_group(execute, expanded_chunk)
        timer.stop()
        intermediate["after_decompress"] = snapshot_group(expanded)
        _require_complete_group(expanded, max_members=runtime.max_members)
        if not expanded_uncompressed_group_is_complete(expanded):
            raise ColdRuntimeError(
                "expanded origin group is incomplete, blocked, or not fully cold",
                error_class="mixed",
                stage="prove_expanded_cold",
            )
        stage = "recompress"
        timer.start("recompress_ms")
        if plan.compress_sql:
            execute(plan.compress_sql)
        complete_chunk = _reload_chunk(execute, locked_chunk)
        complete = collect_residency_group(execute, complete_chunk)
        _require_complete_group(complete, max_members=runtime.max_members)
        if not recompressed_group_is_complete(complete):
            raise ColdRuntimeError(
                "recompressed group is missing a complete cold compressed sibling",
                error_class="mixed",
                stage="prove_complete_cold",
            )
        after_parity = _parity_for(execute, inventories, complete_chunk)
        if after_parity.as_dict() != before_parity.as_dict():
            raise ColdRuntimeError("window parity drifted", error_class="parity", stage="prove_complete_cold")
        timer.stop()
        intermediate["after_recompress"] = snapshot_group(complete)
        stage = "commit"
        timer.start("commit_ms")
        tx.commit()
        timer.stop()
    except CommitAckLost:
        timer.stop()
        commit_ack_lost = True
        error_class = "commit_ack_lost"
    except ColdRuntimeError as error:
        timer.stop()
        error_class = error.error_class
        stage = error.stage
        try:
            tx.rollback()
        except Exception:
            pass
        timer.start("fresh_reconciliation_ms")
        observed = _fresh_observer(connect, before, chunk, before_parity, inventories, runtime)
        timer.stop()
        return _observation(
            outcome="rolled_back" if observed["reconciliation"] == "complete_source" else observed["reconciliation"],
            reconciliation=str(observed["reconciliation"]),
            plan_kind=plan.kind,
            shell_sql_executed=shell_sql_executed,
            before=before,
            after=observed["after"],
            before_parity=before_parity,
            after_parity=observed["after_parity"],
            intermediate=intermediate,
            capacity=capacity,
            error_class=error_class,
            stage=stage,
            reason=str(error),
            timing=timer.as_dict(),
        )
    except Exception as error:
        timer.stop()
        error_class, stage = _classify_sql_error(error, stage=stage)
        try:
            tx.rollback()
        except Exception:
            pass
        timer.start("fresh_reconciliation_ms")
        observed = _fresh_observer(connect, before, chunk, before_parity, inventories, runtime)
        timer.stop()
        return _observation(
            outcome="rolled_back" if observed["reconciliation"] == "complete_source" else observed["reconciliation"],
            reconciliation=str(observed["reconciliation"]),
            plan_kind=plan.kind,
            shell_sql_executed=shell_sql_executed,
            before=before,
            after=observed["after"],
            before_parity=before_parity,
            after_parity=observed["after_parity"],
            intermediate=intermediate,
            capacity=capacity,
            error_class=error_class,
            stage=stage,
            reason=type(error).__name__,
            timing=timer.as_dict(),
        )
    finally:
        _close(tx)

    timer.start("fresh_reconciliation_ms")
    observed = _fresh_observer(connect, before, chunk, before_parity, inventories, runtime)
    timer.stop()
    recon = str(observed["reconciliation"])
    if commit_ack_lost:
        outcome = "committed_ack_lost" if recon == "complete_target" else recon
        return _observation(
            outcome=outcome,
            reconciliation=recon,
            plan_kind=plan.kind,
            shell_sql_executed=shell_sql_executed,
            before=before,
            after=observed["after"],
            before_parity=before_parity,
            after_parity=observed["after_parity"],
            intermediate=intermediate,
            capacity=capacity,
            error_class="commit_ack_lost" if recon != "complete_target" else None,
            stage="commit",
            commit_ack_lost=True,
            timing=timer.as_dict(),
        )
    if recon != "complete_target":
        return _observation(
            outcome=recon,
            reconciliation=recon,
            plan_kind=plan.kind,
            shell_sql_executed=shell_sql_executed,
            before=before,
            after=observed["after"],
            before_parity=before_parity,
            after_parity=observed["after_parity"],
            intermediate=intermediate,
            capacity=capacity,
            error_class=recon,
            stage="fresh_observer",
            timing=timer.as_dict(),
        )
    return _observation(
        outcome="migrated",
        reconciliation="complete_target",
        plan_kind=plan.kind,
        shell_sql_executed=shell_sql_executed,
        before=before,
        after=observed["after"],
        before_parity=before_parity,
        after_parity=observed["after_parity"],
        intermediate=intermediate,
        capacity=capacity,
        timing=timer.as_dict(),
    )


def reconcile_named_group(
    *,
    connect: Connect,
    inventories: BoundInventories,
    hypertable_schema: str,
    hypertable_name: str,
    origin_schema: str,
    origin_name: str,
    range_start: datetime,
    range_end: datetime,
    origin_oid: int,
    before: ResidencyGroup | None = None,
    before_parity: WindowParity | None = None,
    config: RuntimeConfig | None = None,
) -> MoveObservation:
    """Fresh-connection reconciliation keyed by durable origin identity/window."""

    if config is None:
        config = RuntimeConfig()
    started = config.clock()
    fresh = connect()
    try:
        execute = _binder(fresh)
        try:
            chunk = load_catalog_chunk(
                execute,
                hypertable_schema=hypertable_schema,
                hypertable_name=hypertable_name,
                origin_schema=origin_schema,
                origin_name=origin_name,
            )
            after = collect_residency_group(execute, chunk)
            _require_complete_group(after, max_members=config.max_members)
            after_parity = _parity_for(execute, inventories, chunk)
        except ColdRuntimeError as error:
            dummy = before or ResidencyGroup(
                hypertable_schema=hypertable_schema,
                hypertable_name=hypertable_name,
                origin_oid=origin_oid,
                origin_schema=origin_schema,
                origin_name=origin_name,
                compressed_oid=None,
                compressed_schema=None,
                compressed_name=None,
                range_start=range_start,
                range_end=range_end,
                is_compressed=False,
                members=(),
                blocker=str(error),
            )
            return _observation(
                outcome="unknown",
                reconciliation="unknown",
                plan_kind="blocked",
                shell_sql_executed=False,
                before=dummy,
                after=None,
                before_parity=before_parity,
                after_parity=None,
                error_class=error.error_class,
                stage="reconcile",
                reason=str(error),
                timing=inspect_timing_payload(started, config.clock()),
            )
        if before is None or before_parity is None:
            return _observation(
                outcome="unknown",
                reconciliation="unknown",
                plan_kind="blocked",
                shell_sql_executed=False,
                before=after,
                after=after,
                before_parity=before_parity,
                after_parity=after_parity,
                error_class="unknown",
                stage="reconcile",
                reason="complete-source recovery requires original before evidence",
                timing=inspect_timing_payload(started, config.clock()),
            )
        recon = classify_reconciliation(
            before,
            after,
            before_parity=before_parity.as_dict(),
            after_parity=after_parity.as_dict(),
        )
        if (
            after.origin_oid != origin_oid
            or after.range_start != range_start
            or after.range_end != range_end
        ):
            recon = "unknown"
        if recon == "complete_source" and _member_identity(after) != _member_identity(before):
            recon = "unknown"
        return _observation(
            outcome=recon,
            reconciliation=recon,
            plan_kind="already_cold" if recon == "complete_target" else "blocked",
            shell_sql_executed=False,
            before=before,
            after=after,
            before_parity=before_parity,
            after_parity=after_parity,
            error_class=None if recon in {"complete_source", "complete_target"} else recon,
            stage="reconcile",
            timing=inspect_timing_payload(started, config.clock()),
        )
    finally:
        _close(fresh)


def ranked_candidates(
    connect: Connect,
    *,
    cutoff: datetime,
    per_table_limit: int,
    max_catalog_bytes: int,
) -> list[tuple[int, datetime, str, str, int, CatalogChunk]]:
    """Oldest-first per hypertable, then fair merge by rank/range/identity/oid."""

    connection = connect()
    try:
        return ranked_candidates_from_execute(
            _binder(connection),
            cutoff=cutoff,
            per_table_limit=per_table_limit,
            max_catalog_bytes=max_catalog_bytes,
        )
    finally:
        _close(connection)
