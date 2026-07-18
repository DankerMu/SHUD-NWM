#!/usr/bin/env python
"""Clone warm states for an explicit baseline -> direct-grid cutover.

The command is intentionally bounded to one cutover time and an explicit
warm/cold basin partition.  Warm candidates pass the platform's hydrologic
core fingerprint gate before their clone row is written to PostgreSQL and the
DB-free scheduler state index.  Cold candidates are verified to have no target
state at the cutover time; no state is fabricated for them.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore
from packages.common.source_identity import normalize_source_id
from packages.common.state_clone import fingerprint_gated_state_clone
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
)
from scripts.provision_direct_grid_scheduler_registry import _category_files, _required_single
from workers.data_adapters.base import parse_cycle_time
from workers.mapping_builder.rewrite import verify_hydrologic_core_fingerprint_equal


class CutoverCloneError(RuntimeError):
    pass


class _RefusalRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CutoverCloneError(f"JSON object required: {path}")
    return payload


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _source_for_variant(model: Mapping[str, Any]) -> str:
    profile = model.get("resource_profile")
    if not isinstance(profile, Mapping):
        raise CutoverCloneError(f"resource_profile missing for {model.get('model_id')}")
    source_id = profile.get("direct_grid_source_id")
    if not source_id:
        raise CutoverCloneError(f"direct_grid_source_id missing for {model.get('model_id')}")
    return normalize_source_id(str(source_id))


def _baseline_for_variant(model: Mapping[str, Any]) -> str:
    profile = model.get("resource_profile")
    if not isinstance(profile, Mapping) or not profile.get("baseline_model_id"):
        raise CutoverCloneError(f"baseline_model_id missing for {model.get('model_id')}")
    return str(profile["baseline_model_id"])


def _model_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    models = payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes):
        raise CutoverCloneError("registry models must be an array")
    return {str(model["model_id"]): dict(model) for model in models if isinstance(model, Mapping)}


def _variant_map(payload: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for model in _model_map(payload).values():
        key = (_baseline_for_variant(model), _source_for_variant(model))
        if key in result:
            raise CutoverCloneError(f"duplicate direct-grid variant for {key}")
        result[key] = model
    return result


def _package_root(store: LocalObjectStore, model: Mapping[str, Any]) -> Path:
    uri = str(model.get("model_package_uri") or "")
    if not uri:
        raise CutoverCloneError(f"model_package_uri missing for {model.get('model_id')}")
    root = store.resolve_path(uri)
    if not root.is_dir():
        raise CutoverCloneError(f"model package missing: {root}")
    return root


def _package_root_from_uri(store: LocalObjectStore, uri: str | None, *, identity: str) -> Path:
    if not uri:
        raise CutoverCloneError(f"model_package_version missing for {identity}")
    root = store.resolve_path(uri)
    if not root.is_dir():
        raise CutoverCloneError(f"state-producing model package missing for {identity}: {root}")
    return root


def run(args: argparse.Namespace) -> dict[str, Any]:
    cutover_time = parse_cycle_time(args.cutover_time)
    warm_basins = frozenset(_csv(args.warm_basins))
    cold_basins = frozenset(_csv(args.cold_basins))
    if warm_basins & cold_basins:
        raise CutoverCloneError("warm/cold basin partitions overlap")
    if len(warm_basins) != args.expected_warm_count or len(cold_basins) != args.expected_cold_count:
        raise CutoverCloneError("warm/cold basin counts do not match the declared authority")

    baseline_models = _model_map(_read_json(Path(args.baseline_registry)))
    variants = _variant_map(_read_json(Path(args.variant_registry)))
    registry_basins = frozenset(baseline_id for baseline_id, _source_id in variants)
    if warm_basins | cold_basins != registry_basins:
        missing = sorted(registry_basins - warm_basins - cold_basins)
        extra = sorted((warm_basins | cold_basins) - registry_basins)
        raise CutoverCloneError(f"warm/cold partition mismatch: missing={missing}, extra={extra}")
    for basin_id in registry_basins:
        sources = {source_id for model_id, source_id in variants if model_id == basin_id}
        if sources != {"gfs", "IFS"}:
            raise CutoverCloneError(f"expected GFS/IFS variants for {basin_id}, got {sorted(sources)}")

    store = LocalObjectStore(
        root=Path(args.object_store_root),
        object_store_prefix=args.object_store_prefix,
    )
    db_repo = PsycopgStateSnapshotRepository(args.database_url)
    file_repo = FileStateSnapshotIndexRepository(
        index_uri=args.state_index,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        create_missing=False,
    )
    decisions: list[dict[str, Any]] = []

    for baseline_id in sorted(registry_basins):
        baseline = baseline_models.get(baseline_id)
        if baseline is None:
            raise CutoverCloneError(f"baseline registry entry missing: {baseline_id}")
        for source_id in ("gfs", "IFS"):
            variant = variants[(baseline_id, source_id)]
            target_id = str(variant["model_id"])
            existing_db = db_repo.get_state_snapshot_by_model_time(
                model_id=target_id,
                source_id=source_id,
                valid_time=cutover_time,
            )
            existing_file = file_repo.get_state_snapshot_by_model_time(
                model_id=target_id,
                source_id=source_id,
                valid_time=cutover_time,
            )
            if baseline_id in cold_basins:
                if existing_db is not None or existing_file is not None:
                    raise CutoverCloneError(
                        f"cold basin already has target state: {baseline_id}/{source_id}/{target_id}"
                    )
                decisions.append(
                    {
                        "basin_model_id": baseline_id,
                        "source_id": source_id,
                        "target_model_id": target_id,
                        "decision": "cold_new_basin",
                        "state_id": None,
                    }
                )
                continue

            source_state = db_repo.get_state_snapshot_by_model_time(
                model_id=baseline_id,
                source_id=source_id,
                valid_time=cutover_time,
            )
            if source_state is None or not source_state.usable_flag or source_state.lead_hours != 12:
                raise CutoverCloneError(f"qualified source state missing: {baseline_id}/{source_id}")
            baseline_root = _package_root_from_uri(
                store,
                source_state.model_package_version,
                identity=f"{baseline_id}/{source_id}/{source_state.state_id}",
            )
            variant_root = _package_root(store, variant)
            baseline_sp_att = _required_single(baseline_root, "*.sp.att")
            variant_sp_att = _required_single(variant_root, "*.sp.att")
            categories = _category_files(variant_root)
            state_schema_bytes = _required_single(baseline_root, "*.cfg.ic").read_bytes()
            solver_config_bytes = _required_single(baseline_root, "*.cfg.para").read_bytes()
            fingerprint = verify_hydrologic_core_fingerprint_equal(
                baseline_root,
                variant_root,
                baseline_sp_att_path=baseline_sp_att,
                variant_sp_att_path=variant_sp_att,
                category_files=categories,
                baseline_state_schema_bytes=state_schema_bytes,
                variant_state_schema_bytes=state_schema_bytes,
                baseline_solver_config_bytes=solver_config_bytes,
                variant_solver_config_bytes=solver_config_bytes,
            )
            if args.dry_run:
                state_id = None
            else:
                manifest = _read_json(variant_root / "manifest.json")
                recorder = _RefusalRecorder()
                result = fingerprint_gated_state_clone(
                    m0_model_id=baseline_id,
                    m1_model_id=target_id,
                    m1_model_package_version=str(variant["model_package_uri"]),
                    m1_model_package_checksum=str(variant["package_checksum"]),
                    source_id=source_id,
                    cutover_valid_time=cutover_time,
                    m0_package_root=baseline_root,
                    m1_package_root=variant_root,
                    m0_sp_att_path=baseline_sp_att,
                    m1_sp_att_path=variant_sp_att,
                    m1_category_files=categories,
                    m1_recorded_hydrologic_core_fingerprint=fingerprint.hash,
                    state_schema_bytes=state_schema_bytes,
                    solver_config_bytes=solver_config_bytes,
                    m1_forcing_mapping_manifest=dict(manifest.get("direct_grid_forcing") or {}),
                    repository=db_repo,
                    audit_recorder=recorder,
                )
                if result.refused or result.cloned_row is None:
                    raise CutoverCloneError(
                        f"state clone refused: {baseline_id}/{source_id}: "
                        f"{result.refusal_scope}; audit={recorder.records}"
                    )
                file_repo.upsert_state_snapshot(result.cloned_row)
                state_id = result.cloned_row.state_id
            decisions.append(
                {
                    "basin_model_id": baseline_id,
                    "source_id": source_id,
                    "target_model_id": target_id,
                    "decision": "warm_clone",
                    "state_id": state_id,
                    "hydrologic_core_fingerprint": fingerprint.hash,
                }
            )

    receipt = {
        "schema_version": "nhms.direct_grid_cutover_state_clone.v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "cutover_time": cutover_time.isoformat().replace("+00:00", "Z"),
        "dry_run": bool(args.dry_run),
        "warm_basin_count": len(warm_basins),
        "cold_basin_count": len(cold_basins),
        "warm_candidate_count": sum(item["decision"] == "warm_clone" for item in decisions),
        "cold_candidate_count": sum(item["decision"] == "cold_new_basin" for item in decisions),
        "decisions": decisions,
    }
    if args.receipt:
        path = Path(args.receipt)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), required=False)
    parser.add_argument("--object-store-root", required=True)
    parser.add_argument("--object-store-prefix", default="s3://nhms")
    parser.add_argument("--state-index", required=True)
    parser.add_argument("--baseline-registry", required=True)
    parser.add_argument("--variant-registry", required=True)
    parser.add_argument("--cutover-time", required=True)
    parser.add_argument("--warm-basins", required=True)
    parser.add_argument("--cold-basins", required=True)
    parser.add_argument("--expected-warm-count", type=int, default=12)
    parser.add_argument("--expected-cold-count", type=int, default=6)
    parser.add_argument("--receipt")
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    args.dry_run = not args.apply
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
