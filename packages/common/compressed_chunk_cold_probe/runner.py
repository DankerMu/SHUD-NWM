"""Isolated disposable-cluster probe runner."""

from __future__ import annotations

from typing import Any

from packages.common.compressed_chunk_cold_probe.catalog import (
    collect_group,
    load_chunks,
    parity,
    snapshot_group,
)
from packages.common.compressed_chunk_cold_probe.cluster import (
    _container_logs,
    _run,
    assert_engine_identity,
    connect,
    docker_run_argv,
    inspect_live_image,
    scalar,
    wait_for_port,
    wait_for_sql,
)
from packages.common.compressed_chunk_cold_probe.scenarios import (
    probe_rejected_candidates,
    restore_source_compressed,
    run_boundaries,
    run_capacity_preflight,
    run_failures,
    run_lifecycle,
    run_parity_sentinel,
    select_sequence,
)
from packages.common.compressed_chunk_cold_probe.shell import (
    _chunk_by,
    _load_named_chunks,
    bootstrap_extension,
    bootstrap_schema,
    compress_named,
    run_shell_first,
)
from packages.common.compressed_chunk_cold_probe.types import (
    CUTOFF,
    LAG_SECONDS,
    WAL_LIMITATION,
    WATERMARK,
    OwnedResources,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    classify_residency,
    evaluate_capacity_preflight,
)


def run_isolated_cluster(config: ProbeConfig, owned: OwnedResources) -> dict[str, Any]:
    live_image = inspect_live_image(config.docker_bin)
    engine_gate = assert_engine_identity(
        requested_image_id=config.image_id,
        requested_image_ref=config.image_ref,
        live_image_id=live_image["image_id"],
        live_image_ref=live_image["image_ref"],
        used_image_id=config.image_id,
        used_image_ref=config.image_ref,
    )
    run = _run(docker_run_argv(config), timeout=60)
    if run.returncode != 0:
        raise ProbeError(f"docker run failed: {run.stderr.strip()[:300]}")
    owned.created_container = True
    try:
        wait_for_port("127.0.0.1", config.host_port)
        wait_for_sql(config)
    except ProbeError as error:
        raise ProbeError(f"{error}; logs={_container_logs(config.docker_bin, config.container_name)}") from error
    connection = connect(config)
    report: dict[str, Any] = {
        "mode": "isolated-cluster",
        "container_name": config.container_name,
        "host_port": config.host_port,
        "work_root": str(config.work_root),
        "image_requested": {"image_id": config.image_id, "image_ref": config.image_ref},
        "image_live_readonly": live_image,
        "image_used": {"image_id": config.image_id, "image_ref": config.image_ref},
        "engine_gate": engine_gate,
        "image_pin_ok": engine_gate["image_pin_ok"],
        "live_ref_alias": live_image.get("live_ref_alias"),
        "live_repo_digest": next(
            (
                item
                for item in (live_image.get("repo_digests") or [])
                if str(item).startswith("timescale/timescaledb-ha@")
            ),
            None,
        ),
        "cutoff": CUTOFF.isoformat().replace("+00:00", "Z"),
        "watermark": WATERMARK.isoformat().replace("+00:00", "Z"),
        "lag_seconds": LAG_SECONDS,
        "accepted_sequence": ACCEPTED_SEQUENCE_NAME,
        "wal": {"limitation": WAL_LIMITATION},
    }
    try:
        bootstrap_extension(connection)
        server_version = str(scalar(connection, "SHOW server_version"))
        timescaledb_version = str(
            scalar(connection, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        )
        engine_gate = assert_engine_identity(
            requested_image_id=config.image_id,
            requested_image_ref=config.image_ref,
            live_image_id=live_image["image_id"],
            live_image_ref=live_image["image_ref"],
            used_image_id=config.image_id,
            used_image_ref=config.image_ref,
            server_version=server_version,
            timescaledb_version=timescaledb_version,
        )
        report["engine_gate"] = engine_gate
        report["image_pin_ok"] = engine_gate["image_pin_ok"]
        report["pg_matches_pin"] = engine_gate["pg_matches_pin"]
        report["ts_matches_pin"] = engine_gate["ts_matches_pin"]
        report["server"] = {
            "server_version": server_version,
            "timescaledb_version": timescaledb_version,
            "pg_matches_pin": engine_gate["pg_matches_pin"],
            "ts_matches_pin": engine_gate["ts_matches_pin"],
        }
        bootstrap_schema(connection)
        chunks = load_chunks(connection)
        hydro = _chunk_by(chunks=chunks, table="river_timeseries", end=CUTOFF)
        report["parity_sentinel"] = run_parity_sentinel(connection, hydro)
        for chunk in chunks:
            if chunk.range_end <= CUTOFF:
                compress_named(connection, chunk.origin_schema, chunk.origin_name)
        chunks = load_chunks(connection)
        hydro = _chunk_by(chunks=chunks, table="river_timeseries", end=CUTOFF)
        fail_chunk = None
        fail_parity = None
        for item in chunks:
            if item.hypertable_name != "river_timeseries" or not item.is_compressed:
                continue
            if item.origin_oid == hydro.origin_oid or item.range_end > CUTOFF:
                continue
            candidate_parity = parity(connection, item)
            if int(candidate_parity["count"]) > 0:
                fail_chunk = item
                fail_parity = candidate_parity
                break
        if fail_chunk is None or fail_parity is None:
            raise ProbeError("no nonempty compressed failure chunk is available")
        report["failure_chunk_parity"] = fail_parity
        report["group_before"] = snapshot_group(collect_group(connection, hydro))
        capacity_preflight = run_capacity_preflight(connection, config, fail_chunk)
        report["capacity_preflight"] = capacity_preflight
        if not capacity_preflight["positive"]["approved"]:
            raise ProbeError("capacity preflight refused the measured disposable cluster")
        config.capacity_decision = evaluate_capacity_preflight(
            before_compression_total_bytes=capacity_preflight["before_compression_total_bytes"],
            cold_free_bytes=capacity_preflight["measured_cold_free_bytes"],
            cold_reserve_bytes=capacity_preflight["cold_reserve_bytes"],
            hot_free_bytes=capacity_preflight["measured_hot_free_bytes"],
            wal_reserve_bytes=capacity_preflight["wal_reserve_bytes"],
            retained_source_bytes=capacity_preflight["retained_source_bytes"],
        )
        no_index_chunks = _load_named_chunks(connection, "hydro", "no_index_timeseries")
        if no_index_chunks and not no_index_chunks[0].is_compressed:
            compress_named(connection, no_index_chunks[0].origin_schema, no_index_chunks[0].origin_name)
        rejected = probe_rejected_candidates(connection, config, fail_chunk)
        fail_now = collect_group(connection, fail_chunk)
        if classify_residency(fail_now.members) != "all_source" or not fail_now.is_compressed:
            restore_source_compressed(config, fail_chunk)
        shell_rollback = run_shell_first(config, hydro, commit=False)
        rejected["shell_first"] = {
            "complete": shell_rollback.get("reconciliation") == "complete_source"
            and shell_rollback.get("original_sibling") is True
            and shell_rollback.get("phases", {}).get("after_recompress", {}).get("residency") == "already_target",
            "rolled_back_fresh": shell_rollback.get("reconciliation") == "complete_source"
            and shell_rollback.get("original_sibling") is True,
            "result": shell_rollback,
            **{
                k: shell_rollback.get(k)
                for k in (
                    "reconciliation",
                    "original_sibling",
                    "before_parity",
                    "after_parity",
                    "before",
                    "after",
                    "phases",
                )
            },
        }
        report["candidates"] = {key: value for key, value in rejected.items() if key != "shell_first"}
        report["candidates"]["shell_first_rollback"] = shell_rollback
        report["sequence"] = select_sequence(
            {
                "shell_first": rejected["shell_first"],
                **{k: {"complete": False} for k in ("move_chunk", "direct_compressed_alter")},
            }
        )
        if report["sequence"].get("accepted") != ACCEPTED_SEQUENCE_NAME:
            report["status"] = "blocked"
            return report
        report["boundaries"] = run_boundaries(connection, config)
        report["failures"] = run_failures(connection, config, fail_chunk)
        report["lifecycle"] = run_lifecycle(connection, config, hydro)
        report["false_success"] = bool((report.get("failures") or {}).get("false_success"))
        report["status"] = "pending_cleanup"
        return report
    finally:
        connection.close()
