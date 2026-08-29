#!/usr/bin/env python3
"""Isolated TimescaleDB 2.10.2 compressed-chunk cold-residency probe (#1892).

Tablespaces are cluster-scoped. This probe refuses the live ``nhms-db``
container, port 55432, live PGDATA and production trees, then (when requested)
creates a separately named disposable cluster with its own PGDATA and hot/cold
paths. The accepted sequence is shell-first decompress/recompress in one
transaction. Terminal cleanup removes only resources this run created.
"""

from __future__ import annotations

import argparse
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_probe.catalog import (
    _as_capacity_decision,
    _aware,
    _relation_oid,
    _synthetic_relations,
    bytes_by_space,
    collect_group,
    compression_stats,
    fresh_observer,
    load_chunks,
    load_relations,
    members_payload,
    parity,
    reload_chunk,
    retained_source_bytes,
    sibling_identity,
    snapshot_group,
    synthetic_complete_group,
    try_sql,
    unit_plan_report,
    wal_lsn,
)
from packages.common.compressed_chunk_cold_probe.cluster import (
    _container_logs,
    _host_user_spec,
    _run,
    assert_engine_identity,
    cleanup_owned,
    config_from_args,
    connect,
    docker_run_argv,
    execute,
    inspect_live_image,
    prepare_work_root,
    scalar,
    validate_catalog_path_preflight,
    wait_for_port,
    wait_for_sql,
)
from packages.common.compressed_chunk_cold_probe.report import (
    _all_passed,
    _compressed_oid,
    _error_blob,
    _row_parities,
    _source_restored,
    _well_formed_parity,
    parse_probe_report,
    write_report,
)
from packages.common.compressed_chunk_cold_probe.runner import run_isolated_cluster
from packages.common.compressed_chunk_cold_probe.scenarios import (
    _capacity_evidence,
    _full_target,
    _observer_payload,
    probe_rejected_candidates,
    restore_source_compressed,
    run_boundaries,
    run_capacity_preflight,
    run_failures,
    run_lifecycle,
    run_parity_sentinel,
    run_selection_disappearance,
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
    CHUNK_INFO_SQL,
    COMPRESSED_SIBLING_SQL,
    CONTAINER_COLD,
    CONTAINER_FULL,
    CONTAINER_PGDATA,
    CUTOFF,
    DEFAULT_HOST_PORT,
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_STATEMENT_TIMEOUT,
    INDEX_OIDS_SQL,
    LAG_SECONDS,
    OWNED_NAME_RE,
    PROBE_NAME_PREFIX,
    RELATION_OID_SQL,
    RELATION_SQL,
    WAL_LIMITATION,
    WATERMARK,
    WINDOW_STARTS,
    OwnedResources,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    ALLOWED_HYPERTABLES,
    COLD_TABLESPACE_NAME,
    LIVE_CONTAINER_NAME,
    LIVE_PGDATA,
    LIVE_PORT,
    PINNED_IMAGE_ID,
    PINNED_IMAGE_REF,
    REJECTED_SEQUENCE_NAMES,
    SOURCE_TABLESPACE_NAME,
    CatalogChunk,
    CatalogRelation,
    ColdResidencyError,
    ResidencyGroup,
    build_shell_first_plan,
    check_engine_identity,
    classify_eligibility,
    classify_reconciliation,
    classify_residency,
    evaluate_capacity_preflight,
    json_ready,
    move_chunk_candidate_sql,
    origin_shell_is_not_complete,
    qualified_ident,
    quote_ident,
    quote_literal,
    refuse_live_identity,
    resolve_residency_group,
    snapshot_image_identity,
    validate_catalog_path,
    window_parity_sql,
)

__all__ = [
    "ACCEPTED_SEQUENCE_NAME",
    "ALLOWED_HYPERTABLES",
    "CHUNK_INFO_SQL",
    "COLD_TABLESPACE_NAME",
    "COMPRESSED_SIBLING_SQL",
    "CONTAINER_COLD",
    "CONTAINER_FULL",
    "CONTAINER_PGDATA",
    "CUTOFF",
    "CatalogChunk",
    "CatalogRelation",
    "ColdResidencyError",
    "DEFAULT_HOST_PORT",
    "DEFAULT_LOCK_TIMEOUT",
    "DEFAULT_STATEMENT_TIMEOUT",
    "INDEX_OIDS_SQL",
    "LAG_SECONDS",
    "LIVE_CONTAINER_NAME",
    "LIVE_PGDATA",
    "LIVE_PORT",
    "OWNED_NAME_RE",
    "OwnedResources",
    "PINNED_IMAGE_ID",
    "PINNED_IMAGE_REF",
    "PROBE_NAME_PREFIX",
    "ProbeConfig",
    "ProbeError",
    "REJECTED_SEQUENCE_NAMES",
    "RELATION_OID_SQL",
    "RELATION_SQL",
    "ResidencyGroup",
    "SOURCE_TABLESPACE_NAME",
    "WAL_LIMITATION",
    "WATERMARK",
    "WINDOW_STARTS",
    "_all_passed",
    "_as_capacity_decision",
    "_aware",
    "_capacity_evidence",
    "_chunk_by",
    "_compressed_oid",
    "_container_logs",
    "_error_blob",
    "_full_target",
    "_host_user_spec",
    "_load_named_chunks",
    "_observer_payload",
    "_relation_oid",
    "_row_parities",
    "_run",
    "_source_restored",
    "_synthetic_relations",
    "_well_formed_parity",
    "assert_engine_identity",
    "bootstrap_extension",
    "bootstrap_schema",
    "build_shell_first_plan",
    "bytes_by_space",
    "check_engine_identity",
    "classify_eligibility",
    "classify_reconciliation",
    "classify_residency",
    "cleanup_owned",
    "collect_group",
    "compress_named",
    "compression_stats",
    "config_from_args",
    "connect",
    "docker_run_argv",
    "evaluate_capacity_preflight",
    "execute",
    "fresh_observer",
    "inspect_live_image",
    "json_ready",
    "load_chunks",
    "load_relations",
    "main",
    "members_payload",
    "move_chunk_candidate_sql",
    "origin_shell_is_not_complete",
    "parity",
    "parse_args",
    "parse_probe_report",
    "prepare_work_root",
    "probe_rejected_candidates",
    "qualified_ident",
    "quote_ident",
    "quote_literal",
    "refuse_live_identity",
    "reload_chunk",
    "resolve_residency_group",
    "restore_source_compressed",
    "retained_source_bytes",
    "run_boundaries",
    "run_capacity_preflight",
    "run_failures",
    "run_isolated_cluster",
    "run_lifecycle",
    "run_parity_sentinel",
    "run_selection_disappearance",
    "run_shell_first",
    "scalar",
    "select_sequence",
    "sibling_identity",
    "snapshot_group",
    "snapshot_image_identity",
    "synthetic_complete_group",
    "try_sql",
    "unit_plan_report",
    "validate_catalog_path",
    "validate_catalog_path_preflight",
    "wait_for_port",
    "wait_for_sql",
    "wal_lsn",
    "window_parity_sql",
    "write_report",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("refuse-only", "unit-plan", "isolated-cluster"), default="isolated-cluster")
    parser.add_argument("--container-name", default="")
    parser.add_argument("--host-port", type=int, default=DEFAULT_HOST_PORT)
    parser.add_argument("--work-root", default="")
    parser.add_argument("--image-id", default=PINNED_IMAGE_ID)
    parser.add_argument("--image-ref", default=PINNED_IMAGE_REF)
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--lock-timeout", default=DEFAULT_LOCK_TIMEOUT)
    parser.add_argument("--statement-timeout", default=DEFAULT_STATEMENT_TIMEOUT)
    parser.add_argument("--output", default="")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--live-container-name", default=LIVE_CONTAINER_NAME)
    parser.add_argument("--live-pgdata", default=LIVE_PGDATA)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output).resolve() if str(args.output).strip() else None
    try:
        if args.mode == "refuse-only":
            config = config_from_args(args)
            write_report(output, {"status": "refused_not_triggered", "container": config.container_name})
            return 0
        if args.mode == "unit-plan":
            write_report(output, unit_plan_report())
            return 0
        config = config_from_args(args)
        owned = prepare_work_root(config)
        report: dict[str, Any] = {"status": "failed"}
        try:
            report = run_isolated_cluster(config, owned)
        except Exception as error:
            failed = {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error).split("\n")[0],
                "traceback": traceback.format_exc()[-4000:],
            }
            if isinstance(report, dict):
                failed = {**report, **failed}
            report = failed
        finally:
            report["cleanup"] = cleanup_owned(owned, docker_bin=config.docker_bin, keep=config.keep)
            if report.get("status") in {"passed", "pending_cleanup"}:
                report["status"] = "passed" if _all_passed(report) else "failed"
                report["false_success"] = bool((report.get("failures") or {}).get("false_success"))
        write_report(config.output_path or output, report)
        return 0 if report.get("status") == "passed" else 1
    except ColdResidencyError as error:
        write_report(output, {"status": "refused", "error": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
