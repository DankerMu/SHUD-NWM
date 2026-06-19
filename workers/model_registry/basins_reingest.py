from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packages.common.auth_policy import PolicyDecision, cli_policy_decision_from_evidence

from .basins_discovery import (
    BasinsDiscoveryError,
    discover_basins_inventory,
    resolve_basins_root,
    write_inventory,
)
from .basins_package import BasinsPackageError, publish_basins_package
from .basins_registry_import import (
    PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
    BasinsRegistryImportError,
    _transaction,
    import_basins_registry,
)

REINGEST_RECEIPT_SCHEMA_VERSION = "basins.reingest.v1"
_REINGEST_ACTOR_ID = "cli-model-admin"
_REINGEST_ROLE = "model_admin"


class BasinsReingestError(RuntimeError):
    """Raised when a single-basin reingest cannot finish cleanly."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        basin_slug: str | None = None,
        model_id: str | None = None,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.basin_slug = basin_slug
        self.model_id = model_id
        self.path = path
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.basin_slug is not None:
            payload["basin_slug"] = self.basin_slug
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.path is not None:
            payload["path"] = self.path
        payload.update(self.details)
        return payload


def reingest_basin(
    *,
    basin_slug: str,
    model_id: str,
    package_version: str,
    work_dir: str | Path,
    output_path: str | Path,
    basins_root: str | Path | None = None,
    database_url: str | None = None,
    auth_actor_id: str | None = None,
    auth_roles: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run discover -> publish -> import for a single basin and emit a receipt.

    Mirrors the in-process pieces of ``bootstrap_qhh_production`` minus the
    QHH-specific scheduler / station / output-segment wiring. The shared
    ``import_basin_into_registry_core`` sequence (PR 569) covers crosswalk
    write and legacy seg-row purge in-transaction.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    work_dir_path = Path(work_dir).expanduser()
    output_path_obj = Path(output_path).expanduser()
    work_dir_path.mkdir(parents=True, exist_ok=True)
    output_path_obj.parent.mkdir(parents=True, exist_ok=True)

    try:
        root = resolve_basins_root(str(basins_root) if basins_root is not None else None)
        inventory = discover_basins_inventory(root)
    except BasinsDiscoveryError as error:
        raise BasinsReingestError(
            error.error_code,
            str(error),
            basin_slug=basin_slug,
            model_id=model_id,
            path=error.path,
            details=getattr(error, "details", None),
        ) from error

    filtered_inventory = _filter_inventory_to_basin(inventory, basin_slug, model_id)
    inventory_path = work_dir_path / f"{basin_slug}-inventory.json"
    write_inventory(filtered_inventory, inventory_path)

    manifest_path = work_dir_path / f"{basin_slug}-manifest.json"
    try:
        publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version=package_version,
            output_path=manifest_path,
            copy_forcing=False,
        )
    except BasinsPackageError as error:
        raise BasinsReingestError(
            error.error_code,
            str(error),
            basin_slug=basin_slug,
            model_id=model_id,
            path=error.path,
            details=getattr(error, "details", None),
        ) from error

    import_receipt_path = work_dir_path / f"{basin_slug}-import.json"
    # Two decisions: preflight uses the ``unknown`` target_id (the
    # _require_public_import_preflight_policy gate matches on that
    # sentinel before the manifest is read), main pass uses the real
    # model_id. Reusing a single decision fails require_policy_evidence's
    # target_id equality check with "Policy evidence does not authorize".
    preflight_decision = _allow_policy_decision(
        PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
        auth_actor_id=auth_actor_id,
        auth_roles=auth_roles,
    )
    manifest_decision = _allow_policy_decision(
        model_id,
        auth_actor_id=auth_actor_id,
        auth_roles=auth_roles,
    )
    try:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url=database_url,
            output_path=import_receipt_path,
            policy_decision=manifest_decision,
            preflight_policy_decision=preflight_decision,
        )
    except BasinsRegistryImportError as error:
        raise BasinsReingestError(
            error.error_code,
            str(error),
            basin_slug=basin_slug,
            model_id=model_id,
            path=error.path,
            details=error.details,
        ) from error

    model = filtered_inventory["models"][0]
    source_root = Path(model["resolved_source_path"])
    shud_input_name = str(model["shud_input_name"])
    gis_dir = source_root / "input" / shud_input_name / "gis"
    sp_riv_path = source_root / "input" / shud_input_name / f"{shud_input_name}.sp.riv"

    river_count = _count_shapefile_records(gis_dir / "river.shp")
    seg_count = _count_shapefile_records(gis_dir / "seg.shp")
    sp_riv_count = _count_sp_riv_reaches(sp_riv_path)

    db_metrics = _query_post_import_metrics(
        database_url=_resolve_database_url(database_url, basin_slug, model_id),
        basin_slug=basin_slug,
        model_id=model_id,
    )

    finished_at = datetime.now(timezone.utc).isoformat()
    receipt: dict[str, Any] = {
        "schema_version": REINGEST_RECEIPT_SCHEMA_VERSION,
        "basin_slug": basin_slug,
        "model_id": model_id,
        "package_version": package_version,
        "started_at": started_at,
        "finished_at": finished_at,
        "river_shp_record_count": river_count,
        "seg_shp_record_count": seg_count,
        "sp_riv_reach_count": sp_riv_count,
        # TODO(PR 4 verification, issue #565): compute max_edge_meters_observed
        # on real basins via PostGIS ST_MaxDistance / per-edge ST_Length once
        # the verifier owns the threshold. Left NULL here so PR 3 stays
        # functionally focused.
        "max_edge_meters_observed": None,
        # TODO(PR 6, issue #566): wire MVT tile cache purge audit here when
        # the MVT cache invalidation lands.
        "tile_cache_purged_count": 0,
        **db_metrics,
    }
    _write_receipt(output_path_obj, receipt)
    return receipt


def _filter_inventory_to_basin(
    inventory: dict[str, Any],
    basin_slug: str,
    model_id: str,
) -> dict[str, Any]:
    matches = [
        model
        for model in inventory.get("models", [])
        if isinstance(model, dict)
        and (model.get("basin_slug") == basin_slug or model.get("model_id") == model_id)
    ]
    if not matches:
        raise BasinsReingestError(
            "BASINS_REINGEST_BASIN_NOT_FOUND",
            f"Basin slug not found under basins root: {basin_slug}",
            basin_slug=basin_slug,
            model_id=model_id,
            details={"available_basin_slugs": sorted(
                str(model.get("basin_slug") or "")
                for model in inventory.get("models", [])
                if isinstance(model, dict)
            )},
        )
    if len(matches) > 1:
        raise BasinsReingestError(
            "BASINS_REINGEST_BASIN_AMBIGUOUS",
            f"More than one basin matched slug={basin_slug} / model_id={model_id}",
            basin_slug=basin_slug,
            model_id=model_id,
            details={"match_count": len(matches)},
        )
    # Preserve discovery-layer envelope (importable, warnings, root) so the
    # downstream import preflight sees the same single-basin shape as a fresh
    # discovery run.
    filtered = dict(inventory)
    filtered["models"] = matches
    filtered["model_count"] = 1
    return filtered


def _allow_policy_decision(
    target_id: str,
    *,
    auth_actor_id: str | None,
    auth_roles: Sequence[str] | None,
) -> PolicyDecision | None:
    actor = auth_actor_id or _REINGEST_ACTOR_ID
    roles = list(auth_roles) if auth_roles else [_REINGEST_ROLE]
    return cli_policy_decision_from_evidence(
        "models.switch_version",
        target_type="model_registry",
        target_id=target_id,
        actor_id=actor,
        roles=roles,
    )


def _resolve_database_url(database_url: str | None, basin_slug: str, model_id: str) -> str:
    resolved = (database_url or os.getenv("DATABASE_URL", "")).strip()
    if not resolved:
        raise BasinsReingestError(
            "BASINS_REINGEST_DATABASE_URL_MISSING",
            "DATABASE_URL or --database-url is required for basin reingest.",
            basin_slug=basin_slug,
            model_id=model_id,
        )
    return resolved


def _count_shapefile_records(path: Path) -> int:
    if not path.is_file():
        raise BasinsReingestError(
            "BASINS_REINGEST_SHAPEFILE_MISSING",
            f"Required shapefile is missing: {path}",
            path=str(path),
        )
    try:
        import shapefile
    except ImportError as error:
        raise BasinsReingestError(
            "BASINS_REINGEST_SHAPEFILE_DEPENDENCY_MISSING",
            "pyshp is required for basin reingest record counting.",
            path=str(path),
        ) from error
    reader = shapefile.Reader(str(path))
    try:
        # Reader exposes len() (number of records / shapes).
        return len(reader)
    finally:
        reader.close()


def _count_sp_riv_reaches(path: Path) -> int:
    """Read the .sp.riv reach count from the file's count header.

    SHUD .sp.riv files have a first-line count header followed by reach
    rows; the header's first token is the declared reach count. We trust
    that token (matches ``_shud_count_header`` in basins_geometry) so the
    receipt records the same value the parser validated against.
    """
    if not path.is_file():
        raise BasinsReingestError(
            "BASINS_REINGEST_SP_RIV_MISSING",
            f"Required .sp.riv is missing: {path}",
            path=str(path),
        )
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//", "%")):
                continue
            tokens = stripped.split()
            try:
                return int(tokens[0])
            except (IndexError, ValueError) as error:
                raise BasinsReingestError(
                    "BASINS_REINGEST_SP_RIV_MALFORMED",
                    f".sp.riv first non-comment line is not a numeric count: {path}",
                    path=str(path),
                ) from error
    raise BasinsReingestError(
        "BASINS_REINGEST_SP_RIV_MALFORMED",
        f".sp.riv contained no count header: {path}",
        path=str(path),
    )


def _query_post_import_metrics(
    *,
    database_url: str,
    basin_slug: str,
    model_id: str,
) -> dict[str, Any]:
    try:
        with _transaction(database_url) as cursor:
            # The basin_slug recorded in core.basin.basin_id matches the
            # discovery slug via the deterministic ``basins_<slug>`` rule.
            cursor.execute(
                "SELECT basin_id FROM core.basin WHERE basin_id = %s",
                (f"basins_{_slug_id(basin_slug)}",),
            )
            basin_row = cursor.fetchone()
            basin_id = basin_row["basin_id"] if basin_row else None

            # ``core.river_segment`` holds two row classes under one rnv:
            #   * reach rows (from gis/river.shp) — id = "<model>_reach_<iRiv:06d>",
            #     geom always populated, no ``shud_output_river`` property.
            #   * output rows (from .sp.riv, seeded by _ensure_output_river_segments)
            #     — id = "<model>_shud_riv_<N:06d>", ``shud_output_river=true``,
            #     geom backfilled from the matching reach row's iRiv only when
            #     ``shud_riv_index`` (1..N) appears in the reach iRiv set;
            #     production .sp.riv is 1..N contiguous so all match, but the
            #     qhh-sample fixture uses non-contiguous Indices (1,2,3,9,180)
            #     and 2 output rows legitimately keep NULL geom.
            # The receipt counters below all gate on the reach-row predicate so
            # they reflect river.shp ingestion quality, not the seg-output
            # backfill mismatch (which is a known fixture quirk, not a bug).
            reach_predicate = (
                "COALESCE(properties_json->>'shud_output_river', 'false') = 'false'"
            )
            cursor.execute(
                f"""
                SELECT COUNT(*) AS reach_count
                FROM core.river_segment rs
                JOIN core.model_instance mi
                  ON mi.river_network_version_id = rs.river_network_version_id
                WHERE mi.model_id = %s
                  AND {reach_predicate}
                """,
                (model_id,),
            )
            imported_reach_count = int((cursor.fetchone() or {}).get("reach_count") or 0)

            cursor.execute(
                """
                SELECT COUNT(*) AS row_count
                FROM core.river_segment_crosswalk
                WHERE river_network_version_id IN (
                    SELECT river_network_version_id
                    FROM core.model_instance
                    WHERE model_id = %s
                )
                """,
                (model_id,),
            )
            crosswalk_row_count = int((cursor.fetchone() or {}).get("row_count") or 0)

            cursor.execute(
                f"""
                SELECT COUNT(*) AS null_count
                FROM core.river_segment
                WHERE river_network_version_id IN (
                    SELECT river_network_version_id
                    FROM core.model_instance
                    WHERE model_id = %s
                )
                  AND geom IS NULL
                  AND {reach_predicate}
                """,
                (model_id,),
            )
            geom_null_count = int((cursor.fetchone() or {}).get("null_count") or 0)

            # Always 0 by construction: PR #569's
            # _validate_river_shp_single_part_invariant rejects multi-part
            # river.shp at parse time. This counter is a downstream-facing
            # belt-and-suspenders check, not a primary defense. Filtered to
            # reach rows (output rows inherit reach geom via backfill).
            cursor.execute(
                f"""
                SELECT COUNT(*) AS violation_count
                FROM core.river_segment
                WHERE river_network_version_id IN (
                    SELECT river_network_version_id
                    FROM core.model_instance
                    WHERE model_id = %s
                )
                  AND geom IS NOT NULL
                  AND ST_NumGeometries(geom) > 1
                  AND {reach_predicate}
                """,
                (model_id,),
            )
            multi_part_violation_count = int(
                (cursor.fetchone() or {}).get("violation_count") or 0
            )
    except BasinsRegistryImportError as error:
        raise BasinsReingestError(
            error.error_code,
            str(error),
            basin_slug=basin_slug,
            model_id=model_id,
            details=error.details,
        ) from error
    except Exception as error:
        raise BasinsReingestError(
            "BASINS_REINGEST_POST_IMPORT_QUERY_FAILED",
            f"Post-import receipt query failed: {error.__class__.__name__}",
            basin_slug=basin_slug,
            model_id=model_id,
        ) from error

    return {
        "basin_id": basin_id,
        "imported_reach_count": imported_reach_count,
        "crosswalk_row_count": crosswalk_row_count,
        "geom_null_count": geom_null_count,
        "multi_part_violation_count": multi_part_violation_count,
    }


def _write_receipt(output_path: Path, receipt: dict[str, Any]) -> None:
    output_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _slug_id(value: str) -> str:
    # Mirror basins_discovery._slug_id and basins_registry_import._slug_id so
    # the receipt's basin_id lookup matches the discovery suggested_ids.
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"
