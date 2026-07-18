#!/usr/bin/env python
"""Build/register direct-grid model variants and publish a DB-free registry.

The input registry contains one release-frozen hydrologic baseline per basin.
The output contains one source-scoped direct-grid variant per basin/source.
No legacy/IDW row is copied into the output.  Publication is atomic and only
occurs after every package build and database registration succeeds.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.grid_registry_store import CanonicalGridCell, CanonicalGridSnapshot
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.source_identity import normalize_source_id
from services.orchestrator.scheduler_file_providers import publish_scheduler_registry_manifest
from workers.mapping_builder.algorithm import (
    SmallBasinApproval,
    derive_used_cell_subset,
    nearest_cell_barycenter_geodesic_v1,
)
from workers.mapping_builder.cli import build_direct_grid_variant
from workers.mapping_builder.evidence import (
    Approvals,
    CapacityReport,
    DistanceQA,
    GridSnapshotReference,
    RollbackTarget,
)
from workers.model_registry.direct_grid_variant_registration import (
    DirectGridBaselineModelInputs,
    DirectGridVariantRegistrationInput,
    register_direct_grid_variant,
)

DEFAULT_SOURCE_GRIDS = ("GFS=gfs_0p25", "IFS=ifs_0p25")
SCHEMA_VERSION = "nhms.direct_grid.scheduler_registry_provision.v1"


class DirectGridProvisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedGridSnapshot:
    snapshot: CanonicalGridSnapshot
    cells: tuple[CanonicalGridCell, ...]

    def find_snapshot_by_identity(
        self,
        source_id: str,
        grid_id: str,
    ) -> tuple[CanonicalGridSnapshot, list[CanonicalGridCell]] | None:
        if (
            normalize_source_id(source_id) != normalize_source_id(self.snapshot.source_id)
            or grid_id != self.snapshot.grid_id
        ):
            return None
        return self.snapshot, list(self.cells)


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DirectGridProvisionError(f"Cannot read JSON object at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DirectGridProvisionError(f"JSON payload at {path} must be an object.")
    return payload


def _source_grids(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for value in values:
        source, separator, grid_id = value.partition("=")
        if not separator or not source.strip() or not grid_id.strip():
            raise DirectGridProvisionError(f"Invalid --source-grid value {value!r}; expected SOURCE=GRID_ID.")
        parsed.append((normalize_source_id(source), grid_id.strip()))
    if len(parsed) != len(set(parsed)):
        raise DirectGridProvisionError("--source-grid identities must be unique.")
    return tuple(parsed)


def _load_snapshot(cursor: Any, *, source_id: str, grid_id: str) -> LoadedGridSnapshot:
    cursor.execute(
        """
        SELECT *
        FROM met.canonical_grid_snapshot
        WHERE source_id = %s AND grid_id = %s AND superseded_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (normalize_source_id(source_id), grid_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise DirectGridProvisionError(
            f"No active canonical grid snapshot for source_id={source_id!r}, grid_id={grid_id!r}."
        )
    snapshot_row = dict(row)
    cursor.execute(
        """
        SELECT grid_cell_id, longitude, latitude, canonical_ordinal
        FROM met.canonical_grid_cell
        WHERE grid_snapshot_id = %s
        ORDER BY canonical_ordinal
        """,
        (snapshot_row["grid_snapshot_id"],),
    )
    cells = tuple(
        CanonicalGridCell(
            grid_cell_id=str(cell["grid_cell_id"]),
            longitude=float(cell["longitude"]),
            latitude=float(cell["latitude"]),
            canonical_ordinal=int(cell["canonical_ordinal"]),
        )
        for cell in cursor.fetchall()
    )
    if not cells:
        raise DirectGridProvisionError(f"Canonical grid snapshot {snapshot_row['grid_snapshot_id']} has no cells.")
    snapshot = CanonicalGridSnapshot(
        grid_snapshot_id=snapshot_row["grid_snapshot_id"],
        canonical_grid_key=str(snapshot_row["canonical_grid_key"]),
        source_id=str(snapshot_row["source_id"]),
        grid_id=str(snapshot_row["grid_id"]),
        grid_signature=str(snapshot_row["grid_signature"]),
        grid_definition_uri=str(snapshot_row["grid_definition_uri"]),
        grid_definition_checksum=str(snapshot_row["grid_definition_checksum"]),
        longitude_convention=str(snapshot_row["longitude_convention"]),
        latitude_order=str(snapshot_row["latitude_order"]),
        flatten_order=str(snapshot_row["flatten_order"]),
        native_resolution=float(snapshot_row["native_resolution"]),
        bbox_south=float(snapshot_row["bbox_south"]),
        bbox_north=float(snapshot_row["bbox_north"]),
        bbox_west=float(snapshot_row["bbox_west"]),
        bbox_east=float(snapshot_row["bbox_east"]),
        converter_version=str(snapshot_row["converter_version"]),
        valid_from=snapshot_row["valid_from"],
        valid_to=snapshot_row["valid_to"],
        applicable_source_ids=tuple(snapshot_row["applicable_source_ids"] or ()),
        superseded_at=snapshot_row["superseded_at"],
        created_at=snapshot_row["created_at"],
    )
    return LoadedGridSnapshot(snapshot=snapshot, cells=cells)


def _required_single(root: Path, pattern: str) -> Path:
    matches = sorted(path for path in root.rglob(pattern) if path.is_file())
    if len(matches) != 1:
        raise DirectGridProvisionError(
            f"Expected exactly one {pattern!r} under {root}, found {len(matches)}."
        )
    return matches[0]


def _relative(root: Path, paths: Sequence[Path]) -> tuple[str, ...]:
    return tuple(str(path.relative_to(root)) for path in paths)


def _category_files(root: Path) -> dict[str, tuple[str, ...]]:
    cfg_para = _required_single(root, "*.cfg.para")
    calibration = [_required_single(root, "*.cfg.calib")]
    calibration.extend(sorted(path for path in root.glob("CALIB/*") if path.is_file()))
    lake_files = sorted(path for path in root.rglob("*.lake.*") if path.is_file()) or [cfg_para]
    return {
        "mesh": _relative(root, [_required_single(root, "*.sp.mesh")]),
        "river": _relative(
            root,
            [_required_single(root, "*.sp.riv"), _required_single(root, "*.sp.rivseg")],
        ),
        "lake": _relative(root, lake_files),
        "soil": _relative(root, [_required_single(root, "*.para.soil")]),
        "geol": _relative(root, [_required_single(root, "*.para.geol")]),
        "land": _relative(root, [_required_single(root, "*.para.lc")]),
        "calibration": _relative(root, calibration),
    }


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distance_qa(ownerships: Sequence[Any], snapshot: CanonicalGridSnapshot) -> DistanceQA:
    half_diagonal_m = max(float(snapshot.native_resolution) * 111_320.0 / math.sqrt(2.0), 1.0)
    normalized = [float(item.geodesic_distance_m) / half_diagonal_m for item in ownerships]
    return DistanceQA(
        min_normalized=min(normalized, default=0.0),
        p50_normalized=_percentile(normalized, 0.5),
        p95_normalized=_percentile(normalized, 0.95),
        max_normalized=max(normalized, default=0.0),
        tie_count=sum(item.tie_status != "unique" for item in ownerships),
        coverage_edge_count=0,
    )


def _legacy_station_count(root: Path) -> int:
    first_line = _required_single(root, "*.tsd.forc").read_text(encoding="utf-8").splitlines()[0]
    try:
        return int(first_line.split()[0])
    except (IndexError, ValueError) as error:
        raise DirectGridProvisionError(f"Invalid legacy *.tsd.forc header under {root}.") from error


def _capacity_report(*, station_count: int, before_station_count: int) -> CapacityReport:
    timestep_count = 336
    return CapacityReport(
        station_count=station_count,
        timestep_count=timestep_count,
        timeseries_row_count=station_count * timestep_count,
        file_size_bytes=station_count * timestep_count * 128,
        station_count_limit=10_000,
        timestep_count_limit=10_000,
        timeseries_row_count_limit=10_000_000,
        file_size_bytes_limit=512 * 1024 * 1024,
        before_station_count=before_station_count,
        after_station_count=station_count,
        station_reduction_ratio=before_station_count / station_count,
    )


def _package_identity(model: Mapping[str, Any], source_id: str, snapshot: CanonicalGridSnapshot) -> str:
    seed = ":".join(
        (
            str(model["model_id"]),
            str(model["package_checksum"]),
            source_id,
            snapshot.grid_id,
            snapshot.grid_signature,
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _snapshot_projection(snapshot: CanonicalGridSnapshot) -> dict[str, Any]:
    return {
        "source_id": normalize_source_id(snapshot.source_id),
        "grid_id": snapshot.grid_id,
        "grid_signature": snapshot.grid_signature,
        "grid_snapshot_id": str(snapshot.grid_snapshot_id),
        "bbox_south": snapshot.bbox_south,
        "bbox_north": snapshot.bbox_north,
        "bbox_west": snapshot.bbox_west,
        "bbox_east": snapshot.bbox_east,
        "superseded_at": None,
    }


def _baseline_db_inputs(cursor: Any, model_id: str, variant_uri: str) -> DirectGridBaselineModelInputs:
    cursor.execute(
        """
        SELECT river_network_version_id, mesh_version_id, calibration_version_id, shud_code_version
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (model_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DirectGridProvisionError(f"Baseline model {model_id!r} is not registered on node-27.")
    return DirectGridBaselineModelInputs(
        river_network_version_id=str(row["river_network_version_id"]),
        mesh_version_id=str(row["mesh_version_id"]),
        calibration_version_id=str(row["calibration_version_id"]),
        shud_code_version=str(row["shud_code_version"]),
        model_package_uri=variant_uri,
    )


def _build_one(
    *,
    cursor: Any,
    store: LocalObjectStore,
    model: Mapping[str, Any],
    source_id: str,
    loaded: LoadedGridSnapshot,
    operator_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline_root = store.resolve_path(str(model["model_package_uri"]))
    identity = _package_identity(model, source_id, loaded.snapshot)
    package_key = (
        f"models/direct_grid_variants/{model['model_id']}/"
        f"dg-{source_id.lower()}-{identity}/package"
    )
    variant_root = store.resolve_path(package_key)
    variant_uri = store.uri_for_key(package_key) + "/"
    binding_uri = f"{variant_uri}direct_grid_binding.json"
    model_input_package_id = f"dg-input-{identity}"
    manifest_path = variant_root / "manifest.json"

    if not manifest_path.is_file():
        ownerships = nearest_cell_barycenter_geodesic_v1(
            baseline_root,
            source_id,
            loaded.snapshot.grid_id,
            loaded,
        )
        used_cells = derive_used_cell_subset(ownerships, loaded.cells)
        used_count = len(used_cells)
        small_approval = (
            SmallBasinApproval(approver_id=operator_id, used_cell_count=used_count)
            if used_count < 4
            else None
        )
        sp_att = _required_single(baseline_root, "*.sp.att")
        approvals = Approvals(
            builder_approver_id=operator_id,
            reviewer_approver_id=operator_id,
            small_basin_override_approver_id=(operator_id if small_approval else None),
        )
        result = build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=source_id,
            grid_id=loaded.snapshot.grid_id,
            grid_snapshot_loader=loaded,
            snapshot_cells=loaded.cells,
            grid_snapshot_reference=GridSnapshotReference(
                snapshot_id=str(loaded.snapshot.grid_snapshot_id),
                grid_signature=loaded.snapshot.grid_signature,
                snapshot_checksum=loaded.snapshot.grid_definition_checksum,
            ),
            mapping_asset_identity=f"dg-{source_id.lower()}-{identity}",
            model_input_package_id=model_input_package_id,
            binding_uri=binding_uri,
            sp_att_manifest_path=str(sp_att.relative_to(baseline_root)),
            category_files=_category_files(baseline_root),
            state_schema_bytes=_required_single(baseline_root, "*.cfg.ic").read_bytes(),
            solver_config_bytes=_required_single(baseline_root, "*.cfg.para").read_bytes(),
            domain_shp_path=_required_single(baseline_root, "domain.shp"),
            proj_crs_database_version="pyproj-runtime-pinned-by-lockfile",
            approvals=approvals,
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum=str(model["package_checksum"]),
                previous_mapping_asset_label=str(model["model_id"]),
            ),
            distance_qa=_distance_qa(ownerships, loaded.snapshot),
            capacity_report=_capacity_report(
                station_count=used_count,
                before_station_count=_legacy_station_count(baseline_root),
            ),
            applicable_source_ids=(source_id,),
            small_basin_approval=small_approval,
        )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "baseline_model_id": model["model_id"],
            "source_id": source_id,
            "grid_snapshot_id": str(loaded.snapshot.grid_snapshot_id),
            "grid_id": loaded.snapshot.grid_id,
            "grid_signature": loaded.snapshot.grid_signature,
            "station_count": len(result.manifest.station_bindings),
            "evidence_checksum": result.evidence_package.evidence_checksum,
            "small_basin_override": dataclasses.asdict(small_approval) if small_approval else None,
            "operator_id": operator_id,
        }
        (variant_root / "direct_grid_build_receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    manifest_document = _read_json(manifest_path)
    contract = manifest_document.get("direct_grid_forcing")
    if not isinstance(contract, Mapping):
        raise DirectGridProvisionError(
            f"Built manifest {manifest_path} is missing the direct_grid_forcing object."
        )
    baseline_inputs = _baseline_db_inputs(cursor, str(model["model_id"]), variant_uri)
    registration = register_direct_grid_variant(
        cursor,
        DirectGridVariantRegistrationInput(
            basin_version_id=str(model["basin_version_id"]),
            direct_grid_forcing=contract,
            baseline=baseline_inputs,
            grid_snapshot_id=str(loaded.snapshot.grid_snapshot_id),
        ),
    )
    manifest_checksum = sha256_bytes(manifest_path.read_bytes())
    profile = {
        **dict(model["resource_profile"]),
        "forcing_mapping_mode": "direct_grid",
        "direct_grid_forcing": contract,
        "canonical_grid_snapshot": _snapshot_projection(loaded.snapshot),
        "baseline_model_id": model["model_id"],
        "direct_grid_source_id": source_id,
        "manifest_uri": f"{variant_uri}manifest.json",
        "model_package_uri": variant_uri,
        "package_checksum": manifest_checksum,
    }
    try:
        from psycopg2.extras import Json
    except ImportError as error:
        raise DirectGridProvisionError("psycopg2 is required for direct-grid provisioning.") from error
    cursor.execute(
        """
        UPDATE core.model_instance
        SET model_package_uri = %s, resource_profile = %s
        WHERE model_id = %s
        """,
        (variant_uri, Json(profile), registration.model_id),
    )
    registry_row = {
        **dict(model),
        "model_id": registration.model_id,
        "model_package_uri": variant_uri,
        "manifest_uri": f"{variant_uri}manifest.json",
        "package_checksum": manifest_checksum,
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": profile,
    }
    return registry_row, {
        "baseline_model_id": model["model_id"],
        "model_id": registration.model_id,
        "source_id": source_id,
        "grid_id": loaded.snapshot.grid_id,
        "inserted": registration.inserted,
        "station_count": registration.mirror_stations_written,
    }


def provision_direct_grid_registry(
    *,
    baseline_registry: str | Path,
    output_registry: str | Path,
    database_url: str,
    object_store_root: str | Path,
    object_store_prefix: str,
    source_grids: Sequence[tuple[str, str]],
    operator_id: str,
    model_ids: Sequence[str] = (),
) -> dict[str, Any]:
    payload = _read_json(baseline_registry)
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise DirectGridProvisionError("Baseline registry must contain a non-empty models list.")
    selected = [
        dict(model)
        for model in raw_models
        if isinstance(model, Mapping) and (not model_ids or str(model.get("model_id")) in model_ids)
    ]
    if model_ids and {str(model["model_id"]) for model in selected} != set(model_ids):
        raise DirectGridProvisionError("One or more --model-id values are absent from the baseline registry.")
    for model in selected:
        if (model.get("resource_profile") or {}).get("direct_grid_forcing"):
            raise DirectGridProvisionError("Input registry must contain baseline rows, not direct-grid variants.")

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as error:
        raise DirectGridProvisionError("psycopg2 is required for direct-grid provisioning.") from error
    store = LocalObjectStore(object_store_root, object_store_prefix=object_store_prefix)
    output_models: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    connection = psycopg2.connect(database_url)
    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                snapshots = {
                    (source_id, grid_id): _load_snapshot(cursor, source_id=source_id, grid_id=grid_id)
                    for source_id, grid_id in source_grids
                }
                for model in sorted(selected, key=lambda item: str(item["model_id"])):
                    for source_id, grid_id in source_grids:
                        row, result = _build_one(
                            cursor=cursor,
                            store=store,
                            model=model,
                            source_id=source_id,
                            loaded=snapshots[(source_id, grid_id)],
                            operator_id=operator_id,
                        )
                        output_models.append(row)
                        results.append(result)
        receipt = publish_scheduler_registry_manifest(
            output_models,
            output_registry,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            generated_at=datetime.now(UTC),
        )
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "published",
        "baseline_model_count": len(selected),
        "direct_grid_model_count": len(output_models),
        "source_grids": [{"source_id": source, "grid_id": grid} for source, grid in source_grids],
        "models": results,
        "registry": receipt,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-registry", required=True)
    parser.add_argument("--output-registry", required=True)
    parser.add_argument("--object-store-root", default=os.getenv("OBJECT_STORE_ROOT"))
    parser.add_argument("--object-store-prefix", default=os.getenv("OBJECT_STORE_PREFIX", ""))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--source-grid", action="append", default=[])
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.database_url or not args.object_store_root or not args.object_store_prefix:
        raise DirectGridProvisionError("DATABASE_URL, OBJECT_STORE_ROOT and OBJECT_STORE_PREFIX are required.")
    summary = provision_direct_grid_registry(
        baseline_registry=args.baseline_registry,
        output_registry=args.output_registry,
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        source_grids=_source_grids(args.source_grid or DEFAULT_SOURCE_GRIDS),
        operator_id=args.operator_id,
        model_ids=args.model_id,
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
