"""Per-tick selection, intent evidence, and recovery for cold residency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from packages.common.compressed_chunk_cold_receipt import (
    build_receipt,
    iso_now,
    named_groups_from_intent,
    observation_payload,
    publish_intent,
    publish_receipt,
    read_intent,
    remove_intent,
    sidecar_present,
    stable_error,
)
from packages.common.compressed_chunk_cold_residency import (
    CatalogChunk,
    compute_cutoff,
    evaluate_capacity_preflight,
    json_ready,
)
from packages.common.compressed_chunk_cold_runtime import (
    RuntimeConfig,
    TargetIdentity,
    inspect_residency_group,
    migrate_residency_group,
    preflight_target_identity,
    ranked_candidates,
    reconcile_named_group,
)
from packages.common.compressed_chunk_cold_runtime_catalog import (
    BoundInventories,
    ColdRuntimeError,
    WindowParity,
    compression_before_bytes,
    residency_group_from_snapshot,
    retained_source_bytes,
    snapshot_group,
    window_parity_from_dict,
)
from packages.common.compressed_chunk_cold_runtime_timing import default_clock
from packages.common.display_watermark import fetch_display_watermark

Connect = Callable[[], Any]


def cluster_payload(*, server_version: str, timescaledb_version: str, application_name: str) -> dict[str, Any]:
    return {
        "server_version": server_version,
        "timescaledb_version": timescaledb_version,
        "application_name": application_name,
        "observed": True,
    }


def target_payload(identity: TargetIdentity) -> dict[str, Any]:
    return {
        "catalog_name": identity.catalog_name,
        "catalog_location": identity.catalog_location,
        "container_bind": identity.container_bind,
        "host_path": identity.host_path,
        "device_identity": identity.device_identity,
        "observed": True,
    }


def inventory_payload(inventories: BoundInventories) -> dict[str, Any]:
    payload = inventories.as_payload()
    payload["observed"] = True
    return payload


def runtime_config(config: Any) -> RuntimeConfig:
    return RuntimeConfig(
        lock_timeout=config.lock_timeout,
        statement_timeout=f"{-(-config.statement_timeout_ms // 1000)}s",
        max_members=config.max_members,
        expected_catalog_location=config.expected_catalog_location,
        expected_container_bind=config.expected_container_bind,
        expected_host_path=config.expected_host_path,
        expected_device_identity=config.expected_device_identity,
        expected_container_name=config.expected_container_name,
        inspect_target=config.inspect_target,
        clock=getattr(config, "clock", None) or default_clock,
    )


def device_free_bytes(path: str) -> int:
    import os

    usage = os.statvfs(path)
    return int(usage.f_bavail) * int(usage.f_frsize)


def capacity_inputs(config: Any) -> tuple[int, int]:
    cold_free = config.cold_free_bytes
    hot_free = config.hot_free_bytes
    if cold_free is None:
        cold_free = device_free_bytes(config.expected_host_path)
    if hot_free is None:
        hot_free = device_free_bytes("/home")
    return int(cold_free), int(hot_free)


def durable_from_chunk(chunk: CatalogChunk) -> dict[str, Any]:
    return json_ready(
        {
            "hypertable_schema": chunk.hypertable_schema,
            "hypertable_name": chunk.hypertable_name,
            "origin_oid": chunk.origin_oid,
            "origin_schema": chunk.origin_schema,
            "origin_name": chunk.origin_name,
            "range_start": chunk.range_start,
            "range_end": chunk.range_end,
        }
    )


def parse_intent_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def before_from_intent_item(item: Mapping[str, Any]) -> tuple[Any | None, WindowParity | None]:
    before_payload = item.get("before")
    parity_payload = item.get("before_parity")
    if not isinstance(before_payload, Mapping) or not isinstance(parity_payload, Mapping):
        return None, None
    return residency_group_from_snapshot(before_payload), window_parity_from_dict(parity_payload)


def recover_intent(
    config: Any,
    *,
    connect: Connect,
    inventories: BoundInventories,
    generated_at: datetime,
    head_sha: str,
    watermark: datetime,
    cutoff: datetime,
    cluster: Mapping[str, Any],
    target: Mapping[str, Any],
    application_name: str,
    cleanup_margin_seconds: int,
    systemd_margin_seconds: int,
) -> tuple[dict[str, Any], str]:
    del application_name
    intent = read_intent(config.intent_path)
    named = named_groups_from_intent(intent)
    selected_items = intent.get("selected")
    if not isinstance(selected_items, list) or len(selected_items) != len(named):
        raise ColdRuntimeError("intent selected groups are corrupt", error_class="corrupt_intent", stage="startup")
    observations: list[dict[str, Any]] = []
    blocking = False
    terminal_state = "complete_target"
    error: dict[str, str] | None = None
    for durable, item in zip(named, selected_items, strict=True):
        before, before_parity = before_from_intent_item(item)
        observation = reconcile_named_group(
            connect=connect,
            inventories=inventories,
            hypertable_schema=str(durable["hypertable_schema"]),
            hypertable_name=str(durable["hypertable_name"]),
            origin_schema=str(durable["origin_schema"]),
            origin_name=str(durable["origin_name"]),
            range_start=parse_intent_timestamp(str(durable["range_start"])),
            range_end=parse_intent_timestamp(str(durable["range_end"])),
            origin_oid=int(durable["origin_oid"]),
            before=before,
            before_parity=before_parity,
            config=runtime_config(config),
        )
        payload = observation_payload(observation)
        payload["durable"] = durable
        if isinstance(item.get("rank"), int):
            payload["rank"] = item["rank"]
        if isinstance(item.get("capacity"), Mapping):
            payload["capacity"] = dict(item["capacity"])
        observations.append(payload)
        if observation.reconciliation in {"mixed", "unknown"}:
            blocking = True
            terminal_state = observation.reconciliation
            error = stable_error(
                error_class=observation.reconciliation,
                stage="startup",
                reason="unresolved intent blocks new selection",
            )
        elif observation.reconciliation == "complete_source" and terminal_state == "complete_target":
            terminal_state = "complete_source"
    closed_state = terminal_state if terminal_state in {"complete_source", "complete_target"} else "complete_target"
    receipt = build_receipt(
        mode="enforce",
        outcome="failed" if blocking else "clean",
        state=terminal_state if blocking else closed_state,
        head_sha=head_sha,
        generated_at=generated_at,
        watermark=iso_now(watermark),
        lag_seconds=config.lag_seconds,
        cutoff=iso_now(cutoff),
        per_tick_bound=config.per_tick_bound,
        max_members=config.max_members,
        budget={
            "statement_timeout_ms": config.statement_timeout_ms,
            "wrapper_wall_seconds": config.wrapper_wall_seconds,
            "compression_wrapper_wall_seconds": config.compression_wrapper_wall_seconds,
            "systemd_wall_seconds": config.systemd_wall_seconds,
            "cleanup_margin_seconds": cleanup_margin_seconds,
            "systemd_margin_seconds": systemd_margin_seconds,
        },
        cluster=cluster,
        target=target,
        inventory=inventory_payload(inventories),
        capacity=intent.get("capacity") if isinstance(intent.get("capacity"), Mapping) else None,
        selected=observations,
        deferred=[],
        skipped=[],
        error=error,
        recovery={
            "classification": (
                terminal_state
                if blocking
                else ("complete_source" if terminal_state == "complete_source" else "complete_target")
            ),
            "sidecar_present": True,
            "replayed": False,
            "blocked_new_selection": blocking,
        },
    )
    if blocking:
        return receipt, "blocked"
    publish_receipt(config.receipt_path, receipt)
    remove_intent(config.intent_path)
    return receipt, "closed"


def planned_observation(
    chunk: CatalogChunk,
    *,
    rank: int,
    before: Mapping[str, Any] | None = None,
    before_parity: Mapping[str, Any] | None = None,
    capacity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "durable": durable_from_chunk(chunk),
        "outcome": "planned",
        "reconciliation": "complete_source",
        "plan_kind": "migrate",
        "shell_sql_executed": False,
        "rank": rank,
        "replayed": False,
    }
    if before is not None:
        payload["before"] = dict(before)
    if before_parity is not None:
        payload["before_parity"] = dict(before_parity)
    if capacity is not None:
        payload["capacity"] = dict(capacity)
    return payload


def _group_observation_from_inspect(observation: Any, chunk: CatalogChunk, *, rank: int) -> dict[str, Any]:
    payload = observation_payload(observation)
    payload["durable"] = durable_from_chunk(chunk)
    payload["rank"] = rank
    return payload


def _fresh_capacity_for_group(
    *,
    connect: Connect,
    chunk: CatalogChunk,
    config: Any,
) -> tuple[int, int, int]:
    cold_free, hot_free = capacity_inputs(config)
    observer = connect()
    try:
        from packages.common.compressed_chunk_cold_runtime import bind_execute

        before_bytes = compression_before_bytes(bind_execute(observer), chunk)
    finally:
        observer.close()
    return before_bytes, cold_free, hot_free


def run_tick(
    config: Any,
    *,
    now_utc: datetime,
    head_sha: str,
    connect: Connect,
    fetch_watermark: Callable[[], datetime] | None,
    attributed_connect: Callable[..., Any],
    application_name: str,
    cleanup_margin_seconds: int,
    systemd_margin_seconds: int,
    max_catalog_rows: int,
    max_catalog_bytes: int,
) -> dict[str, Any]:
    watermark = (
        fetch_watermark()
        if fetch_watermark is not None
        else fetch_display_watermark(config.database_url, connect=attributed_connect)
    )
    cutoff = compute_cutoff(watermark, config.lag_seconds)
    observer = connect()
    try:
        from packages.common.compressed_chunk_cold_runtime import bind_execute, engine_versions, load_inventories

        execute = bind_execute(observer)
        server_version, timescaledb_version = engine_versions(execute)
        inventories = load_inventories(observer)
        target = preflight_target_identity(
            execute,
            runtime_config(config),
            require_device_identity=bool(config.enforce),
        )
    finally:
        observer.close()
    cluster = cluster_payload(
        server_version=server_version,
        timescaledb_version=timescaledb_version,
        application_name=application_name,
    )
    target_document = target_payload(target)
    budget = {
        "statement_timeout_ms": config.statement_timeout_ms,
        "wrapper_wall_seconds": config.wrapper_wall_seconds,
        "compression_wrapper_wall_seconds": config.compression_wrapper_wall_seconds,
        "systemd_wall_seconds": config.systemd_wall_seconds,
        "cleanup_margin_seconds": cleanup_margin_seconds,
        "systemd_margin_seconds": systemd_margin_seconds,
    }

    if sidecar_present(config.intent_path):
        recovered, status = recover_intent(
            config,
            connect=connect,
            inventories=inventories,
            generated_at=now_utc,
            head_sha=head_sha,
            watermark=watermark,
            cutoff=cutoff,
            cluster=cluster,
            target=target_document,
            application_name=application_name,
            cleanup_margin_seconds=cleanup_margin_seconds,
            systemd_margin_seconds=systemd_margin_seconds,
        )
        if status == "blocked" or config.enforce:
            return recovered

    ranked = ranked_candidates(
        connect,
        cutoff=cutoff,
        per_table_limit=max_catalog_rows,
        max_catalog_bytes=max_catalog_bytes,
    )
    deferred: list[dict[str, Any]] = []
    blocking_error: dict[str, str] | None = None
    blocking_state = "idle"
    inspect_selected: list[dict[str, Any]] = []
    migrate_plan: list[tuple[int, CatalogChunk, Any]] = []
    already_cold: list[dict[str, Any]] = []
    for rank, _range_end, _schema, _name, _oid, chunk in ranked:
        observation = inspect_residency_group(
            connect=connect,
            chunk=chunk,
            inventories=inventories,
            config=runtime_config(config),
        )
        if observation.outcome == "already_cold":
            payload = _group_observation_from_inspect(observation, chunk, rank=rank)
            already_cold.append(payload)
            inspect_selected.append(payload)
            continue
        if observation.plan_kind != "migrate":
            if observation.reconciliation in {"mixed", "unknown"}:
                blocking_error = stable_error(
                    error_class=observation.reconciliation,
                    stage=observation.stage or "plan",
                    reason=observation.reason or observation.reconciliation,
                )
                blocking_state = observation.reconciliation
                inspect_selected.append(_group_observation_from_inspect(observation, chunk, rank=rank))
                break
            deferred.append(
                {
                    "durable": durable_from_chunk(chunk),
                    "reason": observation.reason or observation.outcome,
                    "rank": rank,
                }
            )
            continue
        if len(migrate_plan) >= config.per_tick_bound:
            deferred.append({"durable": durable_from_chunk(chunk), "reason": "per_tick_bound", "rank": rank})
            continue
        migrate_plan.append((rank, chunk, observation))
        planned = planned_observation(
            chunk,
            rank=rank,
            before=snapshot_group(observation.before),
            before_parity=None if observation.before_parity is None else observation.before_parity.as_dict(),
        )
        if observation.timing is not None:
            planned["timing"] = dict(observation.timing)
        inspect_selected.append(planned)

    if not config.enforce:
        outcome = "no_op" if not migrate_plan and not deferred else "clean"
        if already_cold and not migrate_plan and not deferred:
            outcome = "no_op"
        return build_receipt(
            mode="dry-run",
            outcome=outcome,
            state="idle",
            head_sha=head_sha,
            generated_at=now_utc,
            watermark=iso_now(watermark),
            lag_seconds=config.lag_seconds,
            cutoff=iso_now(cutoff),
            per_tick_bound=config.per_tick_bound,
            max_members=config.max_members,
            budget=budget,
            cluster=cluster,
            target=target_document,
            inventory=inventory_payload(inventories),
            capacity=None,
            selected=inspect_selected,
            deferred=deferred,
            skipped=[],
            error=blocking_error,
            recovery=None
            if blocking_error is None
            else {
                "classification": blocking_state if blocking_state in {"mixed", "unknown"} else "unknown",
                "sidecar_present": False,
                "replayed": False,
                "blocked_new_selection": True,
            },
        )

    selected = list(already_cold)
    if blocking_error is not None:
        return build_receipt(
            mode="enforce",
            outcome="failed",
            state=blocking_state,
            head_sha=head_sha,
            generated_at=now_utc,
            watermark=iso_now(watermark),
            lag_seconds=config.lag_seconds,
            cutoff=iso_now(cutoff),
            per_tick_bound=config.per_tick_bound,
            max_members=config.max_members,
            budget=budget,
            cluster=cluster,
            target=target_document,
            inventory=inventory_payload(inventories),
            capacity=None,
            selected=selected,
            deferred=deferred,
            skipped=[],
            error=blocking_error,
            recovery={
                "classification": blocking_state if blocking_state in {"mixed", "unknown"} else "unknown",
                "sidecar_present": False,
                "replayed": False,
                "blocked_new_selection": True,
            },
        )

    mutated: list[dict[str, Any]] = list(already_cold)
    last_capacity: Mapping[str, Any] | None = None
    intent_selected = list(already_cold)
    bound_plan = migrate_plan[: config.per_tick_bound]
    for index, (rank, chunk, inspect) in enumerate(bound_plan):
        before_bytes, cold_free, hot_free = _fresh_capacity_for_group(connect=connect, chunk=chunk, config=config)
        capacity = evaluate_capacity_preflight(
            before_compression_total_bytes=before_bytes,
            cold_free_bytes=cold_free,
            cold_reserve_bytes=config.cold_reserve_bytes,
            hot_free_bytes=hot_free,
            wal_reserve_bytes=config.wal_reserve_bytes,
            retained_source_bytes=retained_source_bytes(inspect.before),
        )
        planned = planned_observation(
            chunk,
            rank=rank,
            before=snapshot_group(inspect.before),
            before_parity=None if inspect.before_parity is None else inspect.before_parity.as_dict(),
            capacity=capacity.as_dict(),
        )
        intent_selected.append(planned)
        intent = build_receipt(
            mode="enforce",
            outcome="in_progress",
            state="in_progress",
            head_sha=head_sha,
            generated_at=now_utc,
            watermark=iso_now(watermark),
            lag_seconds=config.lag_seconds,
            cutoff=iso_now(cutoff),
            per_tick_bound=config.per_tick_bound,
            max_members=config.max_members,
            budget=budget,
            cluster=cluster,
            target=target_document,
            inventory=inventory_payload(inventories),
            capacity=capacity.as_dict(),
            selected=intent_selected,
            deferred=deferred,
            skipped=[],
        )
        publish_intent(config.intent_path, intent)
        publish_receipt(config.receipt_path, intent)
        if not capacity.approved:
            payload = observation_payload(inspect)
            payload["durable"] = durable_from_chunk(chunk)
            payload["rank"] = rank
            payload["outcome"] = "refused"
            payload["capacity"] = capacity.as_dict()
            payload["error"] = stable_error(
                error_class="capacity",
                stage="capacity",
                reason="; ".join(capacity.blockers) or "capacity refused",
            )
            mutated.append(payload)
            last_capacity = capacity.as_dict()
            blocking_error = payload["error"]
            blocking_state = "complete_source"
            break
        observation = migrate_residency_group(
            connect=connect,
            chunk=chunk,
            inventories=inventories,
            watermark=watermark,
            lag_seconds=config.lag_seconds,
            cold_free_bytes=cold_free,
            hot_free_bytes=hot_free,
            cold_reserve_bytes=config.cold_reserve_bytes,
            wal_reserve_bytes=config.wal_reserve_bytes,
            config=runtime_config(config),
        )
        payload = observation_payload(observation)
        payload["durable"] = durable_from_chunk(chunk)
        payload["rank"] = rank
        mutated.append(payload)
        last_capacity = observation.capacity or capacity.as_dict()
        if observation.reconciliation in {"mixed", "unknown"} or observation.outcome not in {
            "migrated",
            "already_cold",
        }:
            blocking_error = stable_error(
                error_class=observation.error_class or observation.reconciliation,
                stage=observation.stage or "migrate",
                reason=observation.reason or observation.outcome,
            )
            blocking_state = observation.reconciliation
            break
        intent_selected[-1] = payload
        pending: list[dict[str, Any]] = []
        next_refused: dict[str, Any] | None = None
        for later_offset, (later_rank, later_chunk, later_inspect) in enumerate(bound_plan[index + 1 :]):
            later_before_bytes, later_cold_free, later_hot_free = _fresh_capacity_for_group(
                connect=connect, chunk=later_chunk, config=config
            )
            later_capacity = evaluate_capacity_preflight(
                before_compression_total_bytes=later_before_bytes,
                cold_free_bytes=later_cold_free,
                cold_reserve_bytes=config.cold_reserve_bytes,
                hot_free_bytes=later_hot_free,
                wal_reserve_bytes=config.wal_reserve_bytes,
                retained_source_bytes=retained_source_bytes(later_inspect.before),
            )
            later_payload = planned_observation(
                later_chunk,
                rank=later_rank,
                before=snapshot_group(later_inspect.before),
                before_parity=(
                    None if later_inspect.before_parity is None else later_inspect.before_parity.as_dict()
                ),
                capacity=later_capacity.as_dict(),
            )
            if later_inspect.timing is not None:
                later_payload["timing"] = dict(later_inspect.timing)
            if later_offset == 0 and not later_capacity.approved:
                later_payload["outcome"] = "refused"
                later_payload["error"] = stable_error(
                    error_class="capacity",
                    stage="capacity",
                    reason="; ".join(later_capacity.blockers) or "capacity refused",
                )
                if later_inspect.timing is not None:
                    later_payload["timing"] = dict(later_inspect.timing)
                next_refused = later_payload
            pending.append(later_payload)
            if next_refused is not None:
                break
        progress = build_receipt(
            mode="enforce",
            outcome="in_progress",
            state="in_progress",
            head_sha=head_sha,
            generated_at=now_utc,
            watermark=iso_now(watermark),
            lag_seconds=config.lag_seconds,
            cutoff=iso_now(cutoff),
            per_tick_bound=config.per_tick_bound,
            max_members=config.max_members,
            budget=budget,
            cluster=cluster,
            target=target_document,
            inventory=inventory_payload(inventories),
            capacity=(pending[0]["capacity"] if pending else last_capacity),
            selected=[*intent_selected, *pending],
            deferred=deferred,
            skipped=[],
        )
        publish_intent(config.intent_path, progress)
        publish_receipt(config.receipt_path, progress)
        if next_refused is not None:
            mutated.append(next_refused)
            last_capacity = (
                next_refused.get("capacity") if isinstance(next_refused.get("capacity"), Mapping) else last_capacity
            )
            blocking_error = next_refused["error"]
            blocking_state = "complete_source"
            break
        hook = getattr(config, "after_group_progress", None)
        if hook is not None:
            hook(rank, payload)

    terminal_state = blocking_state if blocking_error else "complete_target"
    migrated = any(item.get("outcome") == "migrated" for item in mutated)
    if blocking_error and blocking_state in {"mixed", "unknown"}:
        outcome = "failed"
        state = blocking_state
    elif blocking_error:
        outcome = "failed"
        state = terminal_state
    else:
        outcome = "no_op" if not migrated else "clean"
        state = "idle" if outcome == "no_op" else "complete_target"
    receipt = build_receipt(
        mode="enforce",
        outcome=outcome,
        state=state,
        head_sha=head_sha,
        generated_at=now_utc,
        watermark=iso_now(watermark),
        lag_seconds=config.lag_seconds,
        cutoff=iso_now(cutoff),
        per_tick_bound=config.per_tick_bound,
        max_members=config.max_members,
        budget=budget,
        cluster=cluster,
        target=target_document,
        inventory=inventory_payload(inventories),
        capacity=dict(last_capacity) if last_capacity else None,
        selected=mutated,
        deferred=deferred,
        skipped=[],
        error=blocking_error,
        recovery=None
        if blocking_error is None
        else {
            "classification": (
                blocking_state
                if blocking_state in {"mixed", "unknown", "complete_source", "complete_target"}
                else "unknown"
            ),
            "sidecar_present": sidecar_present(config.intent_path),
            "replayed": False,
            "blocked_new_selection": blocking_state in {"mixed", "unknown"},
        },
    )
    publish_receipt(config.receipt_path, receipt)
    if sidecar_present(config.intent_path) and blocking_state not in {"mixed", "unknown"}:
        remove_intent(config.intent_path)
    return receipt
