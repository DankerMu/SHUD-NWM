#!/usr/bin/env python
"""Build/register direct-grid model variants and publish a DB-free registry.

The input registry contains one release-frozen hydrologic baseline per basin.
The output contains one source-scoped direct-grid variant per basin/source.
No legacy/IDW row is copied into the output.  Publication is atomic and only
occurs after every package build and database registration succeeds.

The run is a dry-run unless ``--apply`` is given: it reads, validates and
predicts each variant's ``model_id`` and writes nothing but its own receipt
under ``--receipt-root`` (when ``--succession-id`` is given).  ``--apply``
requires the dry-run receipt of the same succession id and refuses unless that
receipt predicted exactly the variants it is about to register.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common import provision_succession_receipt as succession
from packages.common.grid_registry_store import CanonicalGridCell, CanonicalGridSnapshot
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import provider_destination_parent_problem
from packages.common.source_identity import normalize_source_id
from packages.common.state_qc import cfg_ic_header_shape
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
    plan_direct_grid_variant,
    register_direct_grid_variant,
)

DEFAULT_SOURCE_GRIDS = ("GFS=gfs_0p25", "IFS=ifs_0p25")
SCHEMA_VERSION = "nhms.direct_grid.scheduler_registry_provision.v1"

# Upper bound (bytes) on the leading line read out of a ``*.sp.mesh`` to recover the
# declared element count for the IC header cross-check.
HEADER_LINE_LIMIT_BYTES = 4 * 1024


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


def _read_json_with_sha256(path: str | Path) -> tuple[dict[str, Any], str]:
    try:
        content = Path(path).read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise DirectGridProvisionError(f"Cannot read JSON object at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DirectGridProvisionError(f"JSON payload at {path} must be an object.")
    return payload, sha256_bytes(content)


def _read_json(path: str | Path) -> dict[str, Any]:
    return _read_json_with_sha256(path)[0]


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


def _first_line(payload: bytes, *, label: str) -> str:
    try:
        return payload.split(b"\n", 1)[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise DirectGridProvisionError(f"Cannot read the {label} header line: not UTF-8 text.") from error


def _mesh_element_count(baseline_root: Path) -> int:
    """Return the mesh element count declared on the baseline ``*.sp.mesh`` first line."""

    mesh_path = _required_single(baseline_root, "*.sp.mesh")
    with mesh_path.open("rb") as handle:
        header = _first_line(handle.read(HEADER_LINE_LIMIT_BYTES), label=str(mesh_path))
    tokens = header.split()
    try:
        value = float(tokens[0]) if tokens else None
    except ValueError:
        value = None
    if value is None or not value.is_integer():
        raise DirectGridProvisionError(
            f"Mesh header line of {mesh_path} declares no leading integer element count."
        )
    return int(value)


def _validated_state_schema_bytes(baseline_root: Path) -> bytes:
    """Return the baseline ``*.cfg.ic`` bytes, refusing a malformed header fail-closed.

    The direct-grid variant package copies these bytes verbatim as the variant's
    state schema, so a malformed baseline header propagates into every provisioned
    variant and only detonates at first runtime consumption (issue #1197: a
    ``23106\\t6`` two-token header made the runtime overwrite the mesh-state COLUMN
    COUNT with an epoch-minute, and SHUD tried to allocate ~183 GB). This is the
    only point in the direct-grid flow that reads IC bytes into a package, so it is
    where the shape is checked -- before the bytes are handed to the builder.
    """

    ic_path = _required_single(baseline_root, "*.cfg.ic")
    payload = ic_path.read_bytes()
    header = _first_line(payload, label=str(ic_path))
    shape = cfg_ic_header_shape(header.split(), expected_mesh_count=_mesh_element_count(baseline_root))
    if not shape.valid:
        raise DirectGridProvisionError(f"Refusing to package {ic_path}: {shape.reason}.")
    return payload


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


def _make_package_readable(root: Path) -> None:
    """Make non-secret model assets readable by the node-22 NFS consumer."""

    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)


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


@dataclass(frozen=True)
class _VariantLayout:
    """Where one baseline/source variant lives on the object store."""

    identity: str
    package_key: str
    variant_root: Path
    variant_uri: str


def _variant_layout(
    store: LocalObjectStore,
    model: Mapping[str, Any],
    source_id: str,
    snapshot: CanonicalGridSnapshot,
) -> _VariantLayout:
    identity = _package_identity(model, source_id, snapshot)
    package_key = (
        f"models/direct_grid_variants/{model['model_id']}/"
        f"dg-{source_id.lower()}-{identity}/package"
    )
    return _VariantLayout(
        identity=identity,
        package_key=package_key,
        variant_root=store.resolve_path(package_key),
        variant_uri=store.uri_for_key(package_key) + "/",
    )


def _build_package(
    *,
    baseline_root: Path,
    variant_root: Path,
    layout: _VariantLayout,
    model: Mapping[str, Any],
    source_id: str,
    loaded: LoadedGridSnapshot,
    operator_id: str,
) -> None:
    """Build the variant package at ``variant_root``.

    Only ``variant_root`` differs between the published build and a dry-run's
    temporary one: every identity the manifest records (binding URI, input
    package id, mapping asset identity) comes from ``layout``, so both builds
    produce the same ``manifest.json`` bytes.
    """

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
        mapping_asset_identity=f"dg-{source_id.lower()}-{layout.identity}",
        model_input_package_id=f"dg-input-{layout.identity}",
        binding_uri=f"{layout.variant_uri}direct_grid_binding.json",
        sp_att_manifest_path=str(sp_att.relative_to(baseline_root)),
        category_files=_category_files(baseline_root),
        state_schema_bytes=_validated_state_schema_bytes(baseline_root),
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


def _read_package_contract(package_root: Path) -> tuple[Mapping[str, Any], str]:
    """Return ``(direct_grid_forcing contract, sha256 of manifest.json)`` of a built package."""

    manifest_path = package_root / "manifest.json"
    contract = _read_json(manifest_path).get("direct_grid_forcing")
    if not isinstance(contract, Mapping):
        raise DirectGridProvisionError(
            f"Built manifest {manifest_path} is missing the direct_grid_forcing object."
        )
    return contract, sha256_bytes(manifest_path.read_bytes())


def _temporary_build_contract(*, build_tmp_dir: Path, **build: Any) -> tuple[Mapping[str, Any], str]:
    """Build one variant outside the object store, read its contract, delete the build."""

    scratch = Path(tempfile.mkdtemp(prefix="nhms-provision-dry-run-", dir=build_tmp_dir))
    try:
        _build_package(variant_root=scratch / "package", **build)
        return _read_package_contract(scratch / "package")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _registration_input(
    cursor: Any,
    model: Mapping[str, Any],
    layout: _VariantLayout,
    contract: Mapping[str, Any],
    loaded: LoadedGridSnapshot,
) -> DirectGridVariantRegistrationInput:
    return DirectGridVariantRegistrationInput(
        basin_version_id=str(model["basin_version_id"]),
        direct_grid_forcing=contract,
        baseline=_baseline_db_inputs(cursor, str(model["model_id"]), layout.variant_uri),
        grid_snapshot_id=str(loaded.snapshot.grid_snapshot_id),
    )


def _variant_record(
    *,
    model: Mapping[str, Any],
    source_id: str,
    loaded: LoadedGridSnapshot,
    layout: _VariantLayout,
    model_id: str,
    inserted: bool,
    package_checksum: str,
    package_prebuilt: bool,
    station_count: int,
) -> dict[str, Any]:
    """Return one ``models[]`` row of the succession receipt."""

    return {
        "baseline_model_id": model["model_id"],
        "model_id": model_id,
        "source_id": source_id,
        "grid_id": loaded.snapshot.grid_id,
        "basin_version_id": str(model["basin_version_id"]),
        "package_key": layout.package_key,
        "model_package_uri": layout.variant_uri,
        "manifest_uri": f"{layout.variant_uri}manifest.json",
        "package_checksum": package_checksum,
        "inserted": inserted,
        "package_prebuilt": package_prebuilt,
        "station_count": station_count,
    }


def _plan_one(
    *,
    cursor: Any,
    store: LocalObjectStore,
    model: Mapping[str, Any],
    source_id: str,
    loaded: LoadedGridSnapshot,
    operator_id: str,
    build_tmp_dir: Path,
) -> dict[str, Any]:
    """Predict one variant without writing the database or the object store.

    ``model_id`` derives from the ``binding_checksum`` of a built package, so a
    package that is not on the object store yet is built into a temporary
    directory and discarded.  A package that is there is read as it is: no
    chmod, unlike the apply path's ``_make_package_readable``.
    """

    layout = _variant_layout(store, model, source_id, loaded.snapshot)
    prebuilt = (layout.variant_root / "manifest.json").is_file()
    if prebuilt:
        contract, manifest_checksum = _read_package_contract(layout.variant_root)
    else:
        contract, manifest_checksum = _temporary_build_contract(
            build_tmp_dir=build_tmp_dir,
            baseline_root=store.resolve_path(str(model["model_package_uri"])),
            layout=layout,
            model=model,
            source_id=source_id,
            loaded=loaded,
            operator_id=operator_id,
        )
    plan = plan_direct_grid_variant(cursor, _registration_input(cursor, model, layout, contract, loaded))
    stations = contract.get("station_bindings", contract.get("stations"))
    return _variant_record(
        model=model,
        source_id=source_id,
        loaded=loaded,
        layout=layout,
        model_id=plan.model_id,
        inserted=plan.would_insert,
        package_checksum=manifest_checksum,
        package_prebuilt=prebuilt,
        # One met.met_station upsert per station on the insert and the reuse path alike.
        station_count=len(stations),
    )


def _build_one(
    *,
    cursor: Any,
    store: LocalObjectStore,
    model: Mapping[str, Any],
    source_id: str,
    loaded: LoadedGridSnapshot,
    operator_id: str,
    dry_run_receipt: Mapping[str, Any],
    dry_run_receipt_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    layout = _variant_layout(store, model, source_id, loaded.snapshot)
    variant_root = layout.variant_root
    variant_uri = layout.variant_uri
    prebuilt = (variant_root / "manifest.json").is_file()

    if not prebuilt:
        _build_package(
            baseline_root=store.resolve_path(str(model["model_package_uri"])),
            variant_root=variant_root,
            layout=layout,
            model=model,
            source_id=source_id,
            loaded=loaded,
            operator_id=operator_id,
        )

    _make_package_readable(variant_root)

    contract, manifest_checksum = _read_package_contract(variant_root)
    registration_input = _registration_input(cursor, model, layout, contract, loaded)
    # Apply requires its dry-run: the id this run is about to register must be
    # the one the dry-run receipt predicted, checked before the first write.
    plan = plan_direct_grid_variant(cursor, registration_input)
    succession.require_predicted_variant(
        dry_run_receipt,
        dry_run_receipt_path,
        {"baseline_model_id": model["model_id"], "source_id": source_id, "model_id": plan.model_id},
    )
    registration = register_direct_grid_variant(cursor, registration_input)
    profile = {
        **dict(model["resource_profile"]),
        "lineage": "direct_grid_variant_registration",
        "canonical_grid_key": registration.canonical_grid_key,
        "grid_snapshot_id": registration.grid_snapshot_id,
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
    return registry_row, _variant_record(
        model=model,
        source_id=source_id,
        loaded=loaded,
        layout=layout,
        model_id=registration.model_id,
        inserted=registration.inserted,
        package_checksum=manifest_checksum,
        package_prebuilt=prebuilt,
        station_count=registration.mirror_stations_written,
    )


def _selected_baseline_models(
    baseline_registry: str | Path,
    model_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(selected baseline rows sorted by model_id, sha256 of the registry file)``."""

    payload, registry_sha256 = _read_json_with_sha256(baseline_registry)
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
    return sorted(selected, key=lambda item: str(item["model_id"])), registry_sha256


def _build_tmp_dir(value: str | Path | None, object_store_root: Path) -> Path:
    """Return the directory temporary builds go under, refusing one inside the object store."""

    directory = Path(value) if value else Path(tempfile.gettempdir())
    if not directory.is_dir():
        raise DirectGridProvisionError(f"--build-tmp-dir {directory} is not an existing directory.")
    if directory.resolve().is_relative_to(object_store_root.resolve()):
        raise DirectGridProvisionError(
            f"--build-tmp-dir {directory} resolves inside the object-store root {object_store_root}; "
            "a dry-run must not create files there. Point --build-tmp-dir (or TMPDIR) elsewhere."
        )
    return directory


def _summary_model(variant: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("baseline_model_id", "model_id", "source_id", "grid_id", "inserted", "station_count")
    return {field: variant[field] for field in fields}


def _provision_variants(
    cursor: Any,
    *,
    store: LocalObjectStore,
    selected: Sequence[Mapping[str, Any]],
    source_grids: Sequence[tuple[str, str]],
    operator_id: str,
    build_tmp_dir: Path | None,
    dry_run_receipt: Mapping[str, Any] | None,
    dry_run_receipt_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(source grid records, registry rows, variant records)``.

    ``build_tmp_dir`` selects the mode: set for a dry-run (no registry rows are
    produced), None for an apply (``dry_run_receipt`` is then required).
    """

    snapshots = {
        (source_id, grid_id): _load_snapshot(cursor, source_id=source_id, grid_id=grid_id)
        for source_id, grid_id in source_grids
    }
    grids = [succession.source_grid_record(snapshots[key].snapshot) for key in source_grids]
    if dry_run_receipt is not None:
        succession.require_same_source_grids(dry_run_receipt, dry_run_receipt_path, grids)
    output_models: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    for model in selected:
        for source_id, grid_id in source_grids:
            common: dict[str, Any] = {
                "cursor": cursor,
                "store": store,
                "model": model,
                "source_id": source_id,
                "loaded": snapshots[(source_id, grid_id)],
                "operator_id": operator_id,
            }
            if build_tmp_dir is not None:
                variants.append(_plan_one(build_tmp_dir=build_tmp_dir, **common))
                continue
            row, variant = _build_one(
                dry_run_receipt=dry_run_receipt or {},
                dry_run_receipt_path=dry_run_receipt_path,
                **common,
            )
            output_models.append(row)
            variants.append(variant)
    if dry_run_receipt is not None:
        succession.require_same_variant_set(dry_run_receipt, dry_run_receipt_path, variants)
    return grids, output_models, variants


def _require_publishable_output_registry_parent(output_registry: str | Path) -> None:
    """Refuse now what the registry publisher would refuse after the apply's commit."""

    scheme = urlparse(str(output_registry)).scheme
    if scheme in {"s3", "published"}:
        return  # Resolved by the publisher under the object-store root; not checked here.
    if scheme:
        raise DirectGridProvisionError(
            f"Refusing --output-registry {output_registry}: the registry publisher does not support the scheme "
            f"{scheme!r} and would refuse it only after the apply's database commit. Name a plain path. Nothing "
            "was written and the database was not opened."
        )
    directory = Path(output_registry).expanduser().parent
    problem = provider_destination_parent_problem(directory)
    if problem is not None:
        raise DirectGridProvisionError(
            f"Refusing --output-registry {output_registry}: {problem}. The registry publisher requires its "
            f"directory to be owned and writable by the effective user (uid {os.geteuid()}) and not group- or "
            "other-writable, and an apply would hit that only after its database commit. Nothing was written "
            f"and the database was not opened. If {directory} exists and is yours: `chmod 755 {directory}` and "
            "rerun the same command. Otherwise name an --output-registry in a directory of your own "
            "(`mkdir -m 755 <dir>`); that needs a new --succession-id with its own dry-run only if a dry-run "
            "receipt of this id already names the old path (a refused dry-run wrote none)."
        )


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
    apply: bool = False,
    succession_id: str | None = None,
    receipt_root: str | Path | None = None,
    build_tmp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Plan (default) or apply the provisioning.

    Without ``apply`` nothing is written to the database, the object store or
    ``output_registry``: the connection is read-only and rolled back, and a
    variant that is not built yet is built under ``build_tmp_dir`` and
    discarded.  With ``apply`` the dry-run receipt of the same succession must
    have predicted exactly the variants this run registers.
    """

    selected, registry_sha256 = _selected_baseline_models(baseline_registry, model_ids)
    if succession_id is not None:
        succession.validate_succession_id(succession_id)
    elif apply:
        raise DirectGridProvisionError("--apply requires --succession-id (and the dry-run receipt of that id).")
    store = LocalObjectStore(object_store_root, object_store_prefix=object_store_prefix)
    store_root = Path(store.root)
    receipt_root = Path(receipt_root) if receipt_root else succession.default_receipt_root(store_root)
    header = succession.receipt_header(
        succession_id=succession_id,
        apply=apply,
        operator_id=operator_id,
        object_store_root=store_root,
        object_store_prefix=object_store_prefix,
        selected_model_ids=[str(model["model_id"]) for model in selected],
        baseline_registry=baseline_registry,
        baseline_registry_sha256=registry_sha256,
        output_registry=output_registry,
    )
    # Everything below up to the connection is a refusal that has done nothing.
    _require_publishable_output_registry_parent(output_registry)
    scratch_root = None if apply else _build_tmp_dir(build_tmp_dir, store_root)
    dry_run_receipt: dict[str, Any] | None = None
    dry_run_record: dict[str, Any] = {"path": ""}
    if apply:
        dry_run_receipt, dry_run_record = succession.load_dry_run_receipt(
            receipt_root, str(succession_id), object_store_root=store_root
        )
        succession.require_same_inputs(dry_run_receipt, dry_run_record["path"], header)
    receipt_target = (
        succession.receipt_path(receipt_root, succession_id, apply=apply) if succession_id is not None else None
    )
    if receipt_target is not None:
        succession.prepare_receipt_target(receipt_target, receipt_root=receipt_root)

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as error:
        raise DirectGridProvisionError("psycopg2 is required for direct-grid provisioning.") from error
    provision: dict[str, Any] = {
        "store": store,
        "selected": selected,
        "source_grids": source_grids,
        "operator_id": operator_id,
        "build_tmp_dir": scratch_root,
        "dry_run_receipt": dry_run_receipt,
        "dry_run_receipt_path": dry_run_record["path"],
    }
    registry: dict[str, Any] | None = None
    connection = psycopg2.connect(database_url)
    try:
        if apply:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    grids, output_models, variants = _provision_variants(cursor, **provision)
            registry = publish_scheduler_registry_manifest(
                output_models,
                output_registry,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
                generated_at=datetime.now(UTC),
            )
        else:
            # Never ``with connection:`` here: psycopg2 commits on a clean exit.
            connection.set_session(readonly=True)
            try:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    grids, output_models, variants = _provision_variants(cursor, **provision)
            finally:
                connection.rollback()
    finally:
        connection.close()

    receipt = {**header, "source_grids": grids, "models": variants}
    if registry is not None:
        receipt["output_registry"] = {**header["output_registry"], "sha256": registry.get("content_sha256")}
        receipt["dry_run_receipt"] = dry_run_record
    if receipt_target is not None:
        succession.write_run_receipt(receipt_target, receipt)
        print(f"Provision receipt written: {receipt_target}", file=sys.stderr)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "published" if apply else "planned",
        "baseline_model_count": len(selected),
        "direct_grid_model_count": len(variants),
        "source_grids": [{"source_id": source, "grid_id": grid} for source, grid in source_grids],
        "models": [_summary_model(variant) for variant in variants],
        "registry": registry,
    }
    if not apply:
        summary["plan"] = receipt
        summary["receipt"] = str(receipt_target) if receipt_target is not None else None
    return summary


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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the database, the variant packages and --output-registry. Without it the run is a "
        "dry-run. Requires --succession-id and that id's dry-run receipt.",
    )
    parser.add_argument(
        "--succession-id",
        help="Succession this run belongs to ([A-Za-z0-9._-]{1,80}). The run writes one receipt, never "
        "overwritten, to <receipt-root>/<succession-id>/provision-dry-run.json or provision-apply.json.",
    )
    parser.add_argument(
        "--receipt-root",
        help="Directory holding succession receipts (default: <object-store-root>/scheduler/succession).",
    )
    parser.add_argument(
        "--build-tmp-dir",
        help="Where a dry-run builds a variant package that is not on the object store yet, before "
        "discarding it (default: the system temporary directory, i.e. TMPDIR). Must be outside the "
        "object-store root.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.database_url or not args.object_store_root or not args.object_store_prefix:
        raise DirectGridProvisionError("DATABASE_URL, OBJECT_STORE_ROOT and OBJECT_STORE_PREFIX are required.")
    if not args.apply:
        print(succession.DRY_RUN_NOTICE, flush=True)
    summary = provision_direct_grid_registry(
        baseline_registry=args.baseline_registry,
        output_registry=args.output_registry,
        database_url=args.database_url,
        object_store_root=args.object_store_root,
        object_store_prefix=args.object_store_prefix,
        source_grids=_source_grids(args.source_grid or DEFAULT_SOURCE_GRIDS),
        operator_id=args.operator_id,
        model_ids=args.model_id,
        apply=args.apply,
        succession_id=args.succession_id,
        receipt_root=args.receipt_root,
        build_tmp_dir=args.build_tmp_dir,
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
