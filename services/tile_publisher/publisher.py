from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    rmtree_no_follow,
    verify_directory_no_follow,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CYCLE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_\d{10}")
_FLOOD_TILE_API_PATH = "/api/v1/tiles/flood-return-period"
_DELIVERY_REFERENCE_FIELDS = ("uri", "tile_uri", "tile_uri_template")
_COPYBACK_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class PublishError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class PublishResult:
    cycle_id: str
    status: str
    layers: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    lineage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": list(self.artifacts),
            "cycle_id": self.cycle_id,
            "layers": list(self.layers),
            "lineage": self.lineage,
            "status": self.status,
        }


@dataclass(frozen=True)
class _CopybackSourceTree:
    directories: tuple[str, ...]
    files: tuple[str, ...]


@dataclass(frozen=True)
class _ForcingPackageRef:
    run_id: str
    forcing_version_id: str
    object_key: str
    checksum: str
    lineage_manifest_checksum: str | None
    output_files: tuple[Any, ...]


class TilePublisher:
    """Register existing flood return-period products as map delivery evidence."""

    def __init__(
        self,
        *,
        workspace_root: Path | str,
        object_store_root: Path | str,
        object_store_prefix: str = "",
        database_url: str | None = None,
        published_artifact_root: Path | str | None = None,
        published_artifact_uri_prefix: str | None = None,
        object_store_copyback_root: Path | str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.object_store = LocalObjectStore(object_store_root, object_store_prefix=object_store_prefix)
        self.database_url = (database_url or "").strip()
        self.published_artifact_root = (
            Path(published_artifact_root).expanduser().resolve()
            if published_artifact_root is not None and str(published_artifact_root).strip()
            else None
        )
        self.published_artifact_uri_prefix = (
            (published_artifact_uri_prefix or os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://")).strip()
            or "published://"
        )
        self.object_store_copyback_root = (
            _configured_path_no_resolve(object_store_copyback_root)
            if object_store_copyback_root is not None and str(object_store_copyback_root).strip()
            else None
        )

    @classmethod
    def from_env(cls) -> TilePublisher:
        workspace_root = os.getenv("WORKSPACE_ROOT", "").strip()
        object_store_root = os.getenv("OBJECT_STORE_ROOT", "").strip()
        if not workspace_root:
            raise PublishError("WORKSPACE_ROOT_MISSING", "WORKSPACE_ROOT is required for tile publication.")
        if not object_store_root:
            raise PublishError("OBJECT_STORE_ROOT_MISSING", "OBJECT_STORE_ROOT is required for tile publication.")
        return cls(
            workspace_root=workspace_root,
            object_store_root=object_store_root,
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            database_url=os.getenv("DATABASE_URL", ""),
            published_artifact_root=os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", ""),
            published_artifact_uri_prefix=os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://"),
            object_store_copyback_root=os.getenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", ""),
        )

    def publish_cycle(self, cycle_id: str) -> PublishResult:
        cycle_id = _validate_cycle_id(cycle_id)
        if self.database_url:
            try:
                engine = create_engine(self.database_url, future=True)
                with Session(engine) as session:
                    return self._publish_from_database(session, cycle_id)
            except PublishError:
                raise
            except (SQLAlchemyError, OSError, ValueError) as error:
                raise PublishError("DATABASE_PUBLISH_FAILED", f"Database publish failed: {error}") from error
        try:
            return self._publish_from_object_store(cycle_id)
        except PublishError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise PublishError("OBJECT_STORE_PUBLISH_FAILED", f"Object-store publish failed: {error}") from error

    def _publish_from_database(self, session: Session, cycle_id: str) -> PublishResult:
        if not _has_table(session, "hydro", "hydro_run"):
            raise PublishError("DELIVERY_SCHEMA_MISSING", "hydro.hydro_run is required for tile publication.")
        if not _has_table(session, "flood", "return_period_result"):
            raise PublishError(
                "DELIVERY_SCHEMA_MISSING",
                "flood.return_period_result is required for flood return-period publication.",
            )
        if not _has_table(session, "map", "tile_layer"):
            raise PublishError("DELIVERY_SCHEMA_MISSING", "map.tile_layer is required for tile publication.")

        cycle_quality = self._cycle_display_quality_state(session, cycle_id)
        runs = self._discover_publishable_runs(session, cycle_id)
        if not runs:
            # Degrade gracefully: when no flood return-period tiles are publishable
            # (e.g. flood.flood_frequency_curve empty so return_period is always
            # NULL), publish the q_down display product instead of hard-failing.
            # Only a genuine "nothing publishable" (neither flood nor q_down) maps
            # back to NO_PUBLISHABLE_PRODUCTS so cycle-status handling is unchanged.
            try:
                result = self._publish_qdown_from_database(session, cycle_id)
            except PublishError as error:
                if error.error_code in {
                    "NO_PUBLISHABLE_QDOWN_PRODUCTS",
                    "PUBLISH_IDENTITY_INCOMPLETE",
                    "DELIVERY_SCHEMA_MISSING",
                }:
                    raise PublishError(
                        "NO_PUBLISHABLE_PRODUCTS",
                        "No publishable flood return-period or q_down display products "
                        f"found for cycle_id={cycle_id}.",
                        {"cycle_id": cycle_id, **cycle_quality},
                    ) from error
                raise
            degraded_lineage = {**result.lineage, "degraded_to_display": True}
            return replace(result, lineage=redact_payload(degraded_lineage))

        layers: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        for run in runs:
            layer = self._upsert_flood_layer(session, cycle_id, run)
            layers.append(layer)
            artifacts.append(
                {
                    "artifact_id": layer["layer_id"],
                    "artifact_type": "geojson_endpoint",
                    "uri": layer["tile_uri_template"],
                    "source_run_id": layer["source_run_id"],
                    "tile_format": "geojson",
                }
            )

        source_run_ids = [str(run["run_id"]) for run in runs]
        copyback_summary = self._copyback_run_products(source_run_ids)
        self._mark_runs_published(session, source_run_ids)
        session.commit()
        layers.sort(key=lambda layer: str(layer["layer_id"]))
        artifacts.sort(key=lambda artifact: str(artifact["artifact_id"]))
        lineage = {
            "cycle_id": cycle_id,
            "published_basins": len(runs),
            "source_run_ids": source_run_ids,
            "quality_state": "ready" if cycle_quality["quality_state"] == "ready" else "degraded",
            "unavailable_products": list(cycle_quality["unavailable_products"]),
            "residual_blockers": list(cycle_quality["residual_blockers"]),
        }
        if copyback_summary is not None:
            lineage["object_store_copyback"] = copyback_summary
        return PublishResult(
            cycle_id=cycle_id,
            status="published",
            layers=tuple(layers),
            artifacts=tuple(artifacts),
            lineage=lineage,
        )

    def _discover_publishable_runs(self, session: Session, cycle_id: str) -> list[dict[str, Any]]:
        hydro_columns = _table_columns(session, "hydro", "hydro_run")
        cycle = _cycle_filter(cycle_id)
        if cycle is None:
            raise PublishError(
                "NON_CANONICAL_CYCLE_ID",
                f"cycle_id must use canonical <source>_YYYYMMDDHH lineage: {cycle_id}.",
                {"cycle_id": cycle_id},
            )
        if not {"source_id", "cycle_time"}.issubset(hydro_columns):
            raise PublishError(
                "DELIVERY_LINEAGE_COLUMNS_MISSING",
                "hydro.hydro_run source_id and cycle_time columns are required for cycle-scoped tile publication.",
                {"required_columns": ["source_id", "cycle_time"]},
            )
        where_clauses = [
            "h.run_type = 'forecast'" if "run_type" in hydro_columns else "1 = 1",
            "h.status IN ('frequency_done', 'published')",
        ]
        params: dict[str, Any] = {"source_id": cycle["source_id"].lower()}
        where_clauses.append("lower(h.source_id) = :source_id")
        if session.get_bind().dialect.name == "sqlite":
            where_clauses.append("strftime('%Y%m%d%H', h.cycle_time) = :compact_time")
            params["compact_time"] = cycle["compact_time"]
        else:
            where_clauses.append("h.cycle_time = :cycle_time")
            params["cycle_time"] = cycle["cycle_time"]

        quality_flags_expr = (
            "GROUP_CONCAT(DISTINCT r.quality_flag)"
            if session.get_bind().dialect.name == "sqlite"
            else "STRING_AGG(DISTINCT r.quality_flag, ',')"
        )
        rows = session.execute(
            text(
                f"""
                SELECT h.run_id, h.scenario_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                       COUNT(r.river_segment_id) AS result_rows,
                       SUM(CASE WHEN r.return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
                       SUM(CASE WHEN r.warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows,
                       COUNT(DISTINCT r.river_network_version_id || '::' || r.river_segment_id) AS segment_count,
                       {quality_flags_expr} AS quality_flags
                FROM hydro.hydro_run h
                JOIN flood.return_period_result r ON r.run_id = h.run_id
                WHERE {' AND '.join(where_clauses)}
                  AND r.max_over_window = true
                GROUP BY h.run_id, h.scenario_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time
                ORDER BY h.run_id
                """
            ),
            params,
        ).mappings()
        return [
            dict(row)
            for row in rows
            if _publish_run_quality_state(dict(row)) == "ready"
        ]

    def _cycle_display_quality_state(self, session: Session, cycle_id: str) -> dict[str, Any]:
        cycle = _cycle_filter(cycle_id)
        if cycle is None or not _has_table(session, "hydro", "hydro_run"):
            return {
                "quality_state": "unavailable",
                "unavailable_products": ["return_period_result"],
                "residual_blockers": [
                    {
                        "code": "CYCLE_ID_UNRESOLVED",
                        "state": "unavailable",
                        "residual_risk": "Cycle identity could not be resolved for display publication.",
                    }
                ],
            }
        hydro_columns = _table_columns(session, "hydro", "hydro_run")
        if not {"source_id", "cycle_time"}.issubset(hydro_columns):
            return {
                "quality_state": "unavailable",
                "unavailable_products": ["hydro_run_lineage"],
                "residual_blockers": [
                    {
                        "code": "HYDRO_RUN_LINEAGE_UNAVAILABLE",
                        "state": "unavailable",
                        "residual_risk": "hydro.hydro_run source/cycle lineage columns are unavailable.",
                    }
                ],
            }
        params: dict[str, Any] = {"source_id": cycle["source_id"].lower()}
        time_clause: str
        if session.get_bind().dialect.name == "sqlite":
            time_clause = "strftime('%Y%m%d%H', h.cycle_time) = :compact_time"
            params["compact_time"] = cycle["compact_time"]
        else:
            time_clause = "h.cycle_time = :cycle_time"
            params["cycle_time"] = cycle["cycle_time"]
        error_code_select = "h.error_code" if "error_code" in hydro_columns else "NULL AS error_code"
        error_message_select = "h.error_message" if "error_message" in hydro_columns else "NULL AS error_message"
        error_code_group = "h.error_code" if "error_code" in hydro_columns else "NULL"
        error_message_group = "h.error_message" if "error_message" in hydro_columns else "NULL"
        rows = list(session.execute(
            text(
                f"""
                SELECT h.run_id, h.model_id, h.status, {error_code_select}, {error_message_select},
                       COUNT(r.river_segment_id) AS result_rows,
                       SUM(CASE WHEN r.return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
                       SUM(CASE WHEN r.warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows
                FROM hydro.hydro_run h
                LEFT JOIN flood.return_period_result r
                  ON r.run_id = h.run_id
                 AND r.max_over_window = true
                WHERE lower(h.source_id) = :source_id
                  AND {time_clause}
                GROUP BY h.run_id, h.model_id, h.status, {error_code_group}, {error_message_group}
                ORDER BY h.run_id
                """
            ),
            params,
        ).mappings())
        blockers = []
        unavailable_products: set[str] = set()
        for row in rows:
            if _publish_run_quality_state(dict(row)) == "ready":
                continue
            code = _display_blocker_code(row)
            unavailable_products.update(_display_unavailable_products(code))
            blockers.append(
                {
                    "code": code,
                    "state": "unavailable",
                    "run_id": row["run_id"],
                    "model_id": row.get("model_id"),
                    "status": row.get("status"),
                    "error_code": row.get("error_code"),
                    "residual_risk": row.get("error_message")
                    or _display_blocker_message(code),
                }
            )
        if not rows:
            unavailable_products.add("return_period_result")
            blockers = [
                {
                    "code": "NO_CYCLE_RUNS",
                    "state": "unavailable",
                    "residual_risk": "No hydro runs were found for the requested source cycle.",
                }
            ]
        if not blockers:
            return {"quality_state": "ready", "unavailable_products": [], "residual_blockers": []}
        return {
            "quality_state": "unavailable",
            "unavailable_products": sorted(unavailable_products),
            "residual_blockers": blockers,
        }

    def _upsert_flood_layer(self, session: Session, cycle_id: str, run: dict[str, Any]) -> dict[str, Any]:
        run_id = str(run["run_id"])
        layer_id = f"flood_return_period_{run_id}"
        tile_uri_template = (
            f"/api/v1/tiles/flood-return-period?run_id={run_id}&duration={{duration}}&valid_time={{valid_time}}"
        )
        now = datetime.now(UTC)
        style_json = json.dumps(
            {
                "cycle_id": cycle_id,
                "type": "geojson",
                "warning_level_property": "warning_level",
                "return_period_property": "return_period",
            },
            sort_keys=True,
        )
        values: dict[str, Any] = {
            "layer_id": layer_id,
            "layer_type": "flood_return_period",
            "source_run_id": run_id,
            "source_product_id": str(run.get("scenario_id") or cycle_id),
            "variable": "return_period",
            "valid_time": None,
            "tile_format": "geojson",
            "tile_uri_template": tile_uri_template,
            "min_zoom": 0,
            "max_zoom": 14,
            "style_json": style_json,
            "published_flag": True,
            "publish_time": now,
            "created_at": now,
        }
        columns = _table_columns(session, "map", "tile_layer")
        insert_columns = [column for column in values if column in columns]
        assignments = [
            f"{column} = EXCLUDED.{column}"
            for column in insert_columns
            if column not in {"layer_id", "created_at"}
        ]
        session.execute(
            text(
                f"""
                INSERT INTO map.tile_layer ({', '.join(insert_columns)})
                VALUES ({', '.join(f':{column}' for column in insert_columns)})
                ON CONFLICT (layer_id) DO UPDATE SET {', '.join(assignments)}
                """
            ),
            values,
        )
        unavailable_products = _publish_run_unavailable_products(run)
        quality_state = _publish_run_quality_state(run)
        return {
            "layer_id": layer_id,
            "layer_type": "flood_return_period",
            "source_run_id": run_id,
            "source_product_id": values["source_product_id"],
            "tile_format": "geojson",
            "tile_uri_template": tile_uri_template,
            "published_flag": True,
            "segment_count": int(run.get("segment_count") or 0),
            "return_period_rows": int(run.get("return_period_rows") or 0),
            "warning_rows": int(run.get("warning_rows") or 0),
            "quality_flags": _quality_flags(run.get("quality_flags")),
            "quality_state": quality_state,
            "unavailable_products": unavailable_products,
            "residual_blockers": _publish_run_residual_blockers(run),
        }

    def _mark_runs_published(self, session: Session, run_ids: list[str]) -> None:
        if not run_ids:
            return
        session.execute(
            text(
                """
                UPDATE hydro.hydro_run
                SET status = 'published'
                WHERE run_id IN :run_ids
                  AND status IN ('frequency_done', 'published')
                """
            ).bindparams(bindparam("run_ids", expanding=True)),
            {"run_ids": tuple(run_ids)},
        )

    def _publish_from_object_store(self, cycle_id: str) -> PublishResult:
        artifact_key = f"tiles/hydro/{cycle_id}/flood-return-period/metadata.json"
        if not self.object_store.exists(artifact_key):
            raise PublishError(
                "DATABASE_URL_MISSING",
                "DATABASE_URL is required unless documented tile metadata already exists in the object store.",
                {"expected_artifact": self.object_store.uri_for_key(artifact_key)},
            )
        metadata_uri = self.object_store.uri_for_key(artifact_key)
        metadata = self._read_publish_metadata(artifact_key, metadata_uri, cycle_id)
        layers = tuple(_normalize_metadata_items(metadata.get("layers"), item_id_key="layer_id"))
        artifacts = tuple(_normalize_metadata_items(metadata.get("artifacts"), item_id_key="artifact_id"))
        _validate_metadata_delivery_references(
            self.object_store,
            layers=layers,
            artifacts=artifacts,
            cycle_id=cycle_id,
            metadata_uri=metadata_uri,
            source_run_ids=_metadata_source_run_ids(metadata),
        )
        if not layers and not artifacts:
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata must include at least one layer or artifact.",
                {"metadata_uri": metadata_uri},
            )
        artifact_uri = self.object_store.uri_for_key(artifact_key)
        return PublishResult(
            cycle_id=cycle_id,
            status="published",
            layers=layers,
            artifacts=artifacts,
            lineage=_publish_metadata_lineage(metadata, cycle_id, artifact_uri),
        )

    def _read_publish_metadata(self, artifact_key: str, metadata_uri: str, cycle_id: str) -> dict[str, Any]:
        try:
            metadata = json.loads(self.object_store.read_bytes(artifact_key).decode("utf-8"))
        except json.JSONDecodeError as error:
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                f"Publish metadata is not valid JSON: {error.msg}.",
                {"metadata_uri": metadata_uri},
            ) from error
        except UnicodeDecodeError as error:
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata must be UTF-8 JSON.",
                {"metadata_uri": metadata_uri},
            ) from error
        if not isinstance(metadata, dict):
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata must be a JSON object.",
                {"metadata_uri": metadata_uri},
            )
        if metadata.get("cycle_id") != cycle_id:
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata cycle_id does not match the requested cycle.",
                {"expected_cycle_id": cycle_id, "metadata_uri": metadata_uri},
            )
        return metadata

    # ------------------------------------------------------------------
    # q_down display publication (frequency-independent path)
    # ------------------------------------------------------------------
    def publish_qdown_cycle(self, cycle_id: str) -> PublishResult:
        """Publish q_down display products without depending on flood-frequency.

        Display readiness is independent from frequency readiness: q_down layers
        publish from ``parsed`` runs, while any missing return-period products are
        recorded as honest ``unavailable_products`` / ``residual_blockers``.
        """
        cycle_id = _validate_cycle_id(cycle_id)
        if not self.database_url:
            raise PublishError(
                "DATABASE_URL_MISSING",
                "DATABASE_URL is required for q_down display publication.",
                {"cycle_id": cycle_id},
            )
        try:
            engine = create_engine(self.database_url, future=True)
            with Session(engine) as session:
                return self._publish_qdown_from_database(session, cycle_id)
        except PublishError:
            raise
        except (SQLAlchemyError, OSError, ValueError) as error:
            raise PublishError("QDOWN_PUBLISH_FAILED", f"q_down publish failed: {error}") from error

    def _publish_qdown_from_database(self, session: Session, cycle_id: str) -> PublishResult:
        if not _has_table(session, "hydro", "hydro_run"):
            raise PublishError("DELIVERY_SCHEMA_MISSING", "hydro.hydro_run is required for q_down publication.")
        if not _has_table(session, "hydro", "river_timeseries"):
            raise PublishError(
                "DELIVERY_SCHEMA_MISSING",
                "hydro.river_timeseries is required for q_down publication.",
            )

        runs = self._discover_qdown_runs(session, cycle_id)
        if not runs:
            quality = _qdown_cycle_quality_summary(runs)
            raise PublishError(
                "NO_PUBLISHABLE_QDOWN_PRODUCTS",
                f"No publishable q_down products found for cycle_id={cycle_id}.",
                {"cycle_id": cycle_id, **quality},
            )

        frequency_available = _has_table(session, "flood", "return_period_result")
        # never-break: tolerate a missing map.tile_layer table by skipping DB
        # registration; when present we must register every published layer + commit.
        tile_layer_available = _has_table(session, "map", "tile_layer")
        layers: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        artifact_writes: list[dict[str, Any]] = []
        registrations: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        cycle_unavailable: set[str] = set()
        cycle_blockers: list[dict[str, Any]] = []
        identity_skips = 0
        for run in runs:
            try:
                identity = self._build_qdown_identity(run)
            except PublishError as error:
                if error.error_code != "PUBLISH_IDENTITY_INCOMPLETE":
                    raise
                # F5: do not let one identity-incomplete run sink publishable peers.
                identity_skips += 1
                cycle_unavailable.add("q_down_timeseries")
                cycle_blockers.extend(_qdown_identity_blockers(error, run))
                continue
            freq = self._qdown_frequency_metadata(session, run, frequency_available=frequency_available)
            cycle_unavailable.update(freq["unavailable_products"])
            cycle_blockers.extend(freq["residual_blockers"])
            layer_artifact = self._build_qdown_run_publication(
                cycle_id=cycle_id,
                run=run,
                identity=identity,
                frequency=freq,
            )
            layers.append(layer_artifact["layer"])
            artifacts.append(layer_artifact["manifest_artifact"])
            artifacts.append(layer_artifact["log_artifact"])
            artifact_writes.extend(layer_artifact["artifact_writes"])
            registrations.append((run, layer_artifact["layer"], layer_artifact["manifest_uri"]))

        if not layers:
            # F5: every candidate run was identity-incomplete; nothing publishable.
            raise PublishError(
                "PUBLISH_IDENTITY_INCOMPLETE",
                "All candidate runs have incomplete identity; q_down display cannot be published.",
                {
                    "cycle_id": cycle_id,
                    "quality_state": "unavailable",
                    "unavailable_products": sorted(cycle_unavailable),
                    "residual_blockers": cycle_blockers,
                },
            )

        quality_state = (
            "ready"
            if not cycle_unavailable and not cycle_blockers and identity_skips == 0
            else "degraded"
        )
        # F1: register each published q_down layer in map.tile_layer so the read
        # replica can discover display products from the DB; tolerate a missing table.
        db_registered = False
        source_run_ids = sorted({str(layer["source_run_id"]) for layer in layers})
        published_runs = [run for run, _layer, _manifest_uri in registrations]
        copyback_summary = self._copyback_qdown_products(published_runs)

        for artifact_write in artifact_writes:
            self._write_qdown_artifact(artifact_write["key"], artifact_write["payload"])

        cycle_manifest_uri = self._write_qdown_cycle_manifest(
            cycle_id=cycle_id,
            layers=layers,
            quality_state=quality_state,
            unavailable_products=sorted(cycle_unavailable),
            residual_blockers=cycle_blockers,
        )

        if tile_layer_available:
            for run, layer, manifest_uri in registrations:
                self._upsert_qdown_layer(
                    session,
                    cycle_id=cycle_id,
                    run=run,
                    layer=layer,
                    manifest_uri=manifest_uri,
                )
            session.commit()
            db_registered = True

        layers.sort(key=lambda layer: str(layer["layer_id"]))
        artifacts.sort(key=lambda artifact: str(artifact["artifact_id"]))
        lineage = {
            "cycle_id": cycle_id,
            "published_basins": len(source_run_ids),
            "published_products": len(layers),
            "source_run_ids": source_run_ids,
            "quality_state": quality_state,
            "unavailable_products": sorted(cycle_unavailable),
            "residual_blockers": cycle_blockers,
            "manifest_uri": cycle_manifest_uri,
            "db_registered": db_registered,
        }
        if copyback_summary is not None:
            lineage["object_store_copyback"] = copyback_summary
        return PublishResult(
            cycle_id=cycle_id,
            status="published",
            layers=tuple(layers),
            artifacts=tuple(artifacts),
            lineage=redact_payload(lineage),
        )

    def _discover_qdown_runs(self, session: Session, cycle_id: str) -> list[dict[str, Any]]:
        hydro_columns = _table_columns(session, "hydro", "hydro_run")
        cycle = _cycle_filter(cycle_id)
        if cycle is None:
            raise PublishError(
                "NON_CANONICAL_CYCLE_ID",
                f"cycle_id must use canonical <source>_YYYYMMDDHH lineage: {cycle_id}.",
                {"cycle_id": cycle_id},
            )
        if not {"source_id", "cycle_time"}.issubset(hydro_columns):
            raise PublishError(
                "DELIVERY_LINEAGE_COLUMNS_MISSING",
                "hydro.hydro_run source_id and cycle_time columns are required for q_down publication.",
                {"required_columns": ["source_id", "cycle_time"]},
            )

        is_sqlite = session.get_bind().dialect.name == "sqlite"
        forcing_table_available = _has_optional_table(session, "met", "forcing_version")
        forcing_columns = _table_columns(session, "met", "forcing_version") if forcing_table_available else set()
        if "forcing_version_id" not in forcing_columns:
            forcing_table_available = False
            forcing_columns = set()
        where_clauses = [
            "h.run_type = 'forecast'" if "run_type" in hydro_columns else "1 = 1",
            "h.status IN ('parsed', 'frequency_done', 'published')",
            "r.variable = 'q_down'",
            "lower(h.source_id) = :source_id",
        ]
        params: dict[str, Any] = {"source_id": cycle["source_id"].lower()}
        if is_sqlite:
            where_clauses.append("strftime('%Y%m%d%H', h.cycle_time) = :compact_time")
            params["compact_time"] = cycle["compact_time"]
        else:
            where_clauses.append("h.cycle_time = :cycle_time")
            params["cycle_time"] = cycle["cycle_time"]

        agg_unit = "GROUP_CONCAT(DISTINCT r.unit)" if is_sqlite else "STRING_AGG(DISTINCT r.unit, ',')"
        agg_quality = (
            "GROUP_CONCAT(DISTINCT r.quality_flag)" if is_sqlite else "STRING_AGG(DISTINCT r.quality_flag, ',')"
        )
        optional = self._qdown_optional_selects(hydro_columns)
        forcing = self._qdown_forcing_selects(
            table_available=forcing_table_available,
            forcing_columns=forcing_columns,
        )
        rows = session.execute(
            text(
                f"""
                SELECT h.run_id, h.model_id, h.basin_version_id, h.forcing_version_id,
                       h.source_id, h.cycle_time, h.scenario_id,
                       r.river_network_version_id,
                       {optional['select']}
                       {forcing['select']}
                       COUNT(DISTINCT r.river_network_version_id || '::' || r.river_segment_id) AS segment_count,
                       COUNT(r.value) AS row_count,
                       MIN(r.valid_time) AS first_valid_time,
                       MAX(r.valid_time) AS last_valid_time,
                       {agg_unit} AS units,
                       {agg_quality} AS quality_flags
                FROM hydro.hydro_run h
                JOIN hydro.river_timeseries r
                  ON r.run_id = h.run_id AND r.variable = 'q_down'
                {forcing['join']}
                WHERE {' AND '.join(where_clauses)}
                GROUP BY h.run_id, h.model_id, h.basin_version_id, h.forcing_version_id,
                         h.source_id, h.cycle_time, h.scenario_id,
                         r.river_network_version_id{optional['group']}{forcing['group']}
                ORDER BY h.run_id
                """
            ),
            params,
        ).mappings()
        return [_with_parsed_forcing_lineage(dict(row)) for row in rows]

    @staticmethod
    def _qdown_optional_selects(hydro_columns: set[str]) -> dict[str, str]:
        select_parts: list[str] = []
        group_parts: list[str] = []
        for column in ("run_manifest_uri", "output_uri"):
            if column in hydro_columns:
                select_parts.append(f"h.{column}")
                group_parts.append(f"h.{column}")
        select = (", ".join(select_parts) + ",") if select_parts else ""
        group = ("," + ", ".join(group_parts)) if group_parts else ""
        return {"select": select, "group": group}

    @staticmethod
    def _qdown_forcing_selects(
        *,
        table_available: bool,
        forcing_columns: set[str],
    ) -> dict[str, str]:
        if not table_available:
            return {
                "join": "",
                "select": (
                    "NULL AS forcing_row_forcing_version_id, "
                    "NULL AS forcing_package_uri, "
                    "NULL AS forcing_checksum, "
                    "NULL AS forcing_lineage_json,"
                ),
                "group": "",
            }
        select_parts = [
            "fv.forcing_version_id AS forcing_row_forcing_version_id",
            (
                "fv.forcing_package_uri AS forcing_package_uri"
                if "forcing_package_uri" in forcing_columns
                else "NULL AS forcing_package_uri"
            ),
            (
                "fv.checksum AS forcing_checksum"
                if "checksum" in forcing_columns
                else "NULL AS forcing_checksum"
            ),
            (
                "fv.lineage_json AS forcing_lineage_json"
                if "lineage_json" in forcing_columns
                else "NULL AS forcing_lineage_json"
            ),
        ]
        group_parts = ["fv.forcing_version_id"]
        group_parts.extend(
            f"fv.{column}"
            for column in ("forcing_package_uri", "checksum", "lineage_json")
            if column in forcing_columns
        )
        return {
            "join": "LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id",
            "select": f"{', '.join(select_parts)},",
            "group": ("," + ", ".join(group_parts)) if group_parts else "",
        }

    def _build_qdown_identity(self, run: dict[str, Any]) -> dict[str, Any]:
        """Assemble the strict-identity manifest, refusing to silently fill NULLs."""
        cycle_time = run.get("cycle_time")
        segment_count = int(run.get("segment_count") or 0)
        # station_count source: hydro_run has no station metadata here, so we honestly
        # carry the distinct river-segment count as a documented proxy rather than fake one.
        station_count = segment_count
        identity = {
            "run_id": run.get("run_id"),
            "source": run.get("source_id"),
            "cycle_time": _qdown_isoformat(cycle_time),
            "model_id": run.get("model_id"),
            "basin_version_id": run.get("basin_version_id"),
            "river_network_version_id": run.get("river_network_version_id"),
            "forcing_version_id": run.get("forcing_version_id"),
            "station_count": station_count,
            "station_count_source": "river_segment_proxy",
            "segment_count": segment_count,
        }
        required = (
            "run_id",
            "source",
            "cycle_time",
            "model_id",
            "basin_version_id",
            "river_network_version_id",
            "forcing_version_id",
        )
        missing = [field for field in required if identity.get(field) in (None, "")]
        if missing:
            raise PublishError(
                "PUBLISH_IDENTITY_INCOMPLETE",
                "Strict identity is incomplete; refusing to publish q_down display with missing lineage.",
                {
                    "run_id": run.get("run_id"),
                    "missing_fields": missing,
                    "residual_blockers": [
                        {
                            "code": "PUBLISH_IDENTITY_INCOMPLETE",
                            "state": "unavailable",
                            "run_id": run.get("run_id"),
                            "model_id": run.get("model_id"),
                            "residual_risk": (
                                "Strict identity fields are NULL in hydro.hydro_run; "
                                "q_down display cannot be published without complete lineage."
                            ),
                        }
                    ],
                },
            )
        return identity

    def _qdown_frequency_metadata(
        self, session: Session, run: dict[str, Any], *, frequency_available: bool
    ) -> dict[str, Any]:
        """Detect flood-frequency availability without fabricating any values."""
        unavailable: list[str] = []
        blockers: list[dict[str, Any]] = []
        return_period_rows = 0
        if frequency_available:
            return_period_rows = self._qdown_return_period_rows(session, str(run["run_id"]))
        if return_period_rows <= 0:
            unavailable.extend(["return_period_result", "frequency_curves", "warning_thresholds"])
            blockers.append(
                {
                    "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                    "state": "unavailable",
                    "run_id": run.get("run_id"),
                    "model_id": run.get("model_id"),
                    "residual_risk": (
                        "Flood-frequency results are unavailable for this run; "
                        "q_down display is published but return-period overlays are not."
                    ),
                }
            )
        return {
            "frequency_ready": return_period_rows > 0,
            "return_period_rows": return_period_rows,
            "unavailable_products": unavailable,
            "residual_blockers": blockers,
        }

    @staticmethod
    def _qdown_return_period_rows(session: Session, run_id: str) -> int:
        row = session.execute(
            text(
                """
                SELECT COUNT(*) AS rows
                FROM flood.return_period_result r
                WHERE r.run_id = :run_id
                  AND r.return_period IS NOT NULL
                """
            ),
            {"run_id": run_id},
        ).mappings().first()
        return int(row["rows"]) if row else 0

    def _build_qdown_run_publication(
        self,
        *,
        cycle_id: str,
        run: dict[str, Any],
        identity: dict[str, Any],
        frequency: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(run["run_id"])
        network_segment = _safe_key_segment(run.get("river_network_version_id"))
        run_key = f"{run_id}_{network_segment}"
        layer_id = f"q_down_{run_key}"
        quality_state = "ready" if frequency["frequency_ready"] else "degraded"
        # Reject any display reference that points at a private workspace path.
        for candidate in (run.get("output_uri"), run.get("run_manifest_uri")):
            if candidate and _is_private_display_path(str(candidate)):
                raise PublishError(
                    "DISPLAY_BOUNDARY_VIOLATION",
                    "q_down display references a private workspace path and cannot be published.",
                    {"run_id": run_id},
                )

        layer = {
            "layer_id": layer_id,
            "layer_type": "q_down_timeseries",
            "source_run_id": run_id,
            "source_product_id": str(run.get("scenario_id") or cycle_id),
            "river_network_version_id": run.get("river_network_version_id"),
            "tile_format": "geojson_timeseries",
            "identity": identity,
            "quality_state": quality_state,
            "unavailable_products": list(frequency["unavailable_products"]),
            "segment_count": int(run.get("segment_count") or 0),
            "row_count": int(run.get("row_count") or 0),
            "units": _quality_flags(run.get("units")),
            "quality_flags": _quality_flags(run.get("quality_flags")),
        }

        manifest_key = self.object_store.normalize_key(
            f"tiles/hydro/{cycle_id}/q-down/{run_id}/{network_segment}/manifest.json"
        )
        manifest_payload = redact_payload(
            {
                "cycle_id": cycle_id,
                "layer": layer,
                "identity": identity,
                "frequency": {
                    "frequency_ready": frequency["frequency_ready"],
                    "unavailable_products": frequency["unavailable_products"],
                    "residual_blockers": frequency["residual_blockers"],
                },
                "time_range": {
                    "first_valid_time": _qdown_isoformat(run.get("first_valid_time")),
                    "last_valid_time": _qdown_isoformat(run.get("last_valid_time")),
                },
            }
        )
        manifest_uri = self._qdown_artifact_uri_for_key(manifest_key)
        _reject_private_display_uri(manifest_uri)

        log_key = self.object_store.normalize_key(
            f"tiles/hydro/{cycle_id}/q-down/{run_id}/{network_segment}/publish.log.json"
        )
        log_payload = redact_payload(
            {
                "cycle_id": cycle_id,
                "run_id": run_id,
                "event": "q_down_published",
                "quality_state": quality_state,
                "unavailable_products": frequency["unavailable_products"],
            }
        )
        log_uri = self._qdown_artifact_uri_for_key(log_key)
        _reject_private_display_uri(log_uri)

        return {
            "layer": layer,
            "manifest_uri": manifest_uri,
            "artifact_writes": (
                {"key": manifest_key, "payload": manifest_payload},
                {"key": log_key, "payload": log_payload},
            ),
            "manifest_artifact": {
                "artifact_id": f"q_down_manifest_{run_key}",
                "artifact_type": "q_down_manifest",
                "uri": manifest_uri,
                "source_run_id": run_id,
            },
            "log_artifact": {
                "artifact_id": f"q_down_log_{run_key}",
                "artifact_type": "q_down_publish_log",
                "uri": log_uri,
                "source_run_id": run_id,
            },
        }

    def _upsert_qdown_layer(
        self,
        session: Session,
        *,
        cycle_id: str,
        run: dict[str, Any],
        layer: dict[str, Any],
        manifest_uri: str,
    ) -> None:
        """Upsert one published q_down layer into map.tile_layer (F1).

        ``tile_uri_template`` references the already-written + boundary-validated
        manifest URI so the read replica can discover the display product from the
        DB. Mirrors _upsert_flood_layer's column-filtering / ON CONFLICT pattern.
        """
        # Defense in depth: manifest_uri was validated at write time, re-check here.
        _reject_private_display_uri(manifest_uri)
        run_id = str(run["run_id"])
        now = datetime.now(UTC)
        style_json = json.dumps(
            {
                "cycle_id": cycle_id,
                "type": "q_down_timeseries",
                "river_network_version_id": run.get("river_network_version_id"),
            },
            sort_keys=True,
        )
        values: dict[str, Any] = {
            "layer_id": layer["layer_id"],
            "layer_type": "q_down_timeseries",
            "source_run_id": run_id,
            "source_product_id": str(run.get("scenario_id") or cycle_id),
            "variable": "q_down",
            "valid_time": None,
            "tile_format": "geojson_timeseries",
            "tile_uri_template": manifest_uri,
            "min_zoom": 0,
            "max_zoom": 14,
            "style_json": style_json,
            "published_flag": True,
            "publish_time": now,
            "created_at": now,
        }
        columns = _table_columns(session, "map", "tile_layer")
        insert_columns = [column for column in values if column in columns]
        assignments = [
            f"{column} = EXCLUDED.{column}"
            for column in insert_columns
            if column not in {"layer_id", "created_at"}
        ]
        session.execute(
            text(
                f"""
                INSERT INTO map.tile_layer ({', '.join(insert_columns)})
                VALUES ({', '.join(f':{column}' for column in insert_columns)})
                ON CONFLICT (layer_id) DO UPDATE SET {', '.join(assignments)}
                """
            ),
            {column: values[column] for column in insert_columns},
        )

    def _write_qdown_cycle_manifest(
        self,
        *,
        cycle_id: str,
        layers: list[dict[str, Any]],
        quality_state: str,
        unavailable_products: list[str],
        residual_blockers: list[dict[str, Any]],
    ) -> str:
        manifest_key = self.object_store.normalize_key(f"tiles/hydro/{cycle_id}/q-down/manifest.json")
        # Each layer is one run x river_network_version_id product. published_basins
        # counts distinct run_id; published_products counts the run x network rows.
        source_run_ids = sorted({str(layer["source_run_id"]) for layer in layers})
        payload = redact_payload(
            {
                "cycle_id": cycle_id,
                "published_basins": len(source_run_ids),
                "published_products": len(layers),
                "source_run_ids": source_run_ids,
                "quality_state": quality_state,
                "unavailable_products": unavailable_products,
                "residual_blockers": residual_blockers,
                "layers": [
                    {"layer_id": layer["layer_id"], "source_run_id": layer["source_run_id"]} for layer in layers
                ],
            }
        )
        manifest_uri = self._write_qdown_artifact(manifest_key, payload)
        _reject_private_display_uri(manifest_uri)
        return manifest_uri

    def _write_qdown_artifact(self, key: str, payload: dict[str, Any]) -> str:
        content = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.object_store.write_bytes_atomic(key, content)
        if self.published_artifact_root is not None:
            published_key = self.object_store.normalize_key(key)
            self._write_published_artifact(published_key, content)
        return self._qdown_artifact_uri_for_key(key)

    def _qdown_artifact_uri_for_key(self, key: str) -> str:
        if self.published_artifact_root is not None:
            published_key = self.object_store.normalize_key(key)
            return f"{_prefix_with_separator(self.published_artifact_uri_prefix)}{published_key}"
        return self.object_store.uri_for_key(key)

    def _write_published_artifact(self, key: str, content: bytes) -> None:
        if self.published_artifact_root is None:
            return
        target = self.published_artifact_root / key
        try:
            ensure_directory_no_follow(self.published_artifact_root)
            atomic_write_bytes_no_follow(
                target,
                content,
                containment_root=self.published_artifact_root,
                temp_suffix="part",
            )
        except (OSError, SafeFilesystemError) as error:
            raise PublishError(
                "PUBLISHED_ARTIFACT_WRITE_FAILED",
                "Failed to write q_down artifact to the published artifact root.",
                {"artifact_key": key},
            ) from error

    def _copyback_run_products(self, run_ids: list[str]) -> dict[str, Any] | None:
        """Mirror complete run products to a shared object-store root.

        ``published`` is intentionally limited to display artifacts. Complete
        SHUD run products keep the object-store keyspace (``runs/<run_id>/...``)
        and are copied to ``NHMS_OBJECT_STORE_COPYBACK_ROOT`` on the control
        node, after compute-node work has finished and before publication is
        marked successful.
        """

        if self.object_store_copyback_root is None:
            return None
        unique_run_ids = sorted({str(run_id).strip() for run_id in run_ids if str(run_id).strip()})
        object_store_root_raw = _configured_path_no_resolve(self.object_store.root)
        copyback_root_raw = self.object_store_copyback_root
        if not unique_run_ids:
            return {
                "status": "skipped",
                "reason": "no_source_run_ids",
                "root": str(copyback_root_raw),
                "run_ids": [],
            }

        try:
            object_store_root = verify_directory_no_follow(object_store_root_raw).resolve()
        except (OSError, SafeFilesystemError) as error:
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Object-store staging root is unsafe for copyback.",
                {
                    "copyback_root": str(copyback_root_raw),
                    "object_store_root": str(object_store_root_raw),
                    "error": str(error),
                },
            ) from error

        copyback_root = self._prepare_copyback_root(
            copyback_root_raw=copyback_root_raw,
            object_store_root_raw=object_store_root_raw,
            object_store_root=object_store_root,
        )

        if copyback_root == object_store_root:
            self._validate_copyback_source_products(
                unique_run_ids,
                copyback_root=copyback_root,
                object_store_root=object_store_root,
            )
            return {
                "status": "skipped",
                "reason": "copyback_root_matches_object_store_root",
                "root": str(copyback_root),
                "run_ids": unique_run_ids,
            }

        if _paths_overlap(copyback_root, object_store_root):
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Object-store copyback root must not overlap OBJECT_STORE_ROOT.",
                {
                    "copyback_root": str(copyback_root),
                    "object_store_root": str(object_store_root),
                    "reason": "copyback_root_object_store_root_overlap",
                },
            )

        try:
            copyback_store = LocalObjectStore(
                copyback_root,
                object_store_prefix=self.object_store.object_store_prefix,
            )
        except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Failed to initialize object-store copyback root.",
                {
                    "copyback_root": str(copyback_root),
                    "object_store_root": str(object_store_root),
                    "error": str(error),
                },
            ) from error

        copied_runs: list[dict[str, Any]] = []
        total_files = 0
        total_bytes = 0
        for run_id in unique_run_ids:
            run_key: str | None = None
            try:
                run_key = _run_product_key(run_id)
                summary = self._copyback_object_tree(
                    run_key,
                    copyback_store,
                    validate_source_tree=lambda source_tree, run_id=run_id: self._validate_copyback_source_tree(
                        run_id, source_tree
                    ),
                )
            except FileNotFoundError as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
                    "Run products are missing from the object-store staging root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Failed to copy run products to the shared object-store root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            if run_key is None:
                raise AssertionError("run_key must be set after successful copyback.")
            copied_runs.append({"run_id": run_id, "object_key": run_key, **summary})
            total_files += int(summary["file_count"])
            total_bytes += int(summary["byte_count"])

        return {
            "status": "copied",
            "root": str(copyback_root),
            "run_ids": unique_run_ids,
            "file_count": total_files,
            "byte_count": total_bytes,
            "runs": copied_runs,
        }

    def _copyback_qdown_products(self, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self.object_store_copyback_root is None:
            return None

        unique_run_ids = sorted({str(run.get("run_id")).strip() for run in runs if str(run.get("run_id")).strip()})
        object_store_root_raw = _configured_path_no_resolve(self.object_store.root)
        copyback_root_raw = self.object_store_copyback_root
        if not unique_run_ids:
            return {
                "status": "skipped",
                "reason": "no_source_run_ids",
                "root": str(copyback_root_raw),
                "run_ids": [],
            }

        try:
            object_store_root = verify_directory_no_follow(object_store_root_raw).resolve()
        except (OSError, SafeFilesystemError) as error:
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Object-store staging root is unsafe for copyback.",
                {
                    "copyback_root": str(copyback_root_raw),
                    "object_store_root": str(object_store_root_raw),
                    "error": str(error),
                },
            ) from error

        copyback_root = self._prepare_copyback_root(
            copyback_root_raw=copyback_root_raw,
            object_store_root_raw=object_store_root_raw,
            object_store_root=object_store_root,
        )
        forcing_refs = self._forcing_package_refs_for_runs(
            runs,
            copyback_root=copyback_root,
            object_store_root=object_store_root,
        )
        self._validate_qdown_copyback_sources(
            run_ids=unique_run_ids,
            forcing_refs=forcing_refs,
            copyback_root=copyback_root,
            object_store_root=object_store_root,
        )

        if copyback_root == object_store_root:
            return {
                "status": "skipped",
                "reason": "copyback_root_matches_object_store_root",
                "root": str(copyback_root),
                "run_ids": unique_run_ids,
                "runs": [{"run_id": run_id, "object_key": _run_product_key(run_id)} for run_id in unique_run_ids],
                "forcing_packages": [
                    {
                        "object_key": ref.object_key,
                        "run_ids": _run_ids_for_forcing_key(self.object_store, ref.object_key, runs),
                        "forcing_version_ids": _forcing_version_ids_for_key(self.object_store, ref.object_key, runs),
                    }
                    for ref in forcing_refs
                ],
            }

        if _paths_overlap(copyback_root, object_store_root):
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Object-store copyback root must not overlap OBJECT_STORE_ROOT.",
                {
                    "copyback_root": str(copyback_root),
                    "object_store_root": str(object_store_root),
                    "reason": "copyback_root_object_store_root_overlap",
                },
            )

        try:
            copyback_store = LocalObjectStore(
                copyback_root,
                object_store_prefix=self.object_store.object_store_prefix,
            )
        except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Failed to initialize object-store copyback root.",
                {
                    "copyback_root": str(copyback_root),
                    "object_store_root": str(object_store_root),
                    "error": str(error),
                },
            ) from error

        copied_runs: list[dict[str, Any]] = []
        copied_forcing: list[dict[str, Any]] = []
        total_files = 0
        total_bytes = 0

        for run_id in unique_run_ids:
            run_key = _run_product_key(run_id)
            try:
                summary = self._copyback_object_tree(
                    run_key,
                    copyback_store,
                    validate_source_tree=lambda source_tree, run_id=run_id: self._validate_copyback_source_tree(
                        run_id, source_tree
                    ),
                )
            except FileNotFoundError as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
                    "Run products are missing from the object-store staging root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Failed to copy run products to the shared object-store root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            copied_runs.append({"run_id": run_id, "object_key": run_key, **summary})
            total_files += int(summary["file_count"])
            total_bytes += int(summary["byte_count"])

        for ref in forcing_refs:
            try:
                summary = self._copyback_object_tree(
                    ref.object_key,
                    copyback_store,
                    validate_source_tree=lambda source_tree, ref=ref: self._validate_forcing_source_tree(
                        ref, source_tree
                    ),
                )
            except FileNotFoundError as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
                    "Forcing package is missing from the object-store staging root.",
                    _forcing_copyback_error_details(
                        ref,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Failed to copy forcing package to the shared object-store root.",
                    _forcing_copyback_error_details(
                        ref,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            copied_forcing.append(
                {
                    "object_key": ref.object_key,
                    "run_ids": _run_ids_for_forcing_key(self.object_store, ref.object_key, runs),
                    "forcing_version_ids": _forcing_version_ids_for_key(self.object_store, ref.object_key, runs),
                    **summary,
                }
            )
            total_files += int(summary["file_count"])
            total_bytes += int(summary["byte_count"])

        return {
            "status": "copied",
            "root": str(copyback_root),
            "run_ids": unique_run_ids,
            "file_count": total_files,
            "byte_count": total_bytes,
            "runs": copied_runs,
            "forcing_packages": copied_forcing,
        }

    def _prepare_copyback_root(
        self,
        *,
        copyback_root_raw: Path,
        object_store_root_raw: Path,
        object_store_root: Path,
    ) -> Path:
        if _paths_overlap(copyback_root_raw, object_store_root_raw) and copyback_root_raw != object_store_root_raw:
            _raise_copyback_root_overlap(copyback_root_raw, object_store_root)

        try:
            _reject_existing_symlink_components(copyback_root_raw)
            if copyback_root_raw == object_store_root_raw:
                verified_copyback_root = verify_directory_no_follow(copyback_root_raw)
            else:
                verified_copyback_root = ensure_directory_no_follow(copyback_root_raw)
            copyback_root = verified_copyback_root.resolve()
        except (OSError, SafeFilesystemError) as error:
            raise PublishError(
                "OBJECT_STORE_COPYBACK_FAILED",
                "Failed to prepare object-store copyback root.",
                {
                    "copyback_root": str(copyback_root_raw),
                    "object_store_root": str(object_store_root),
                    "error": str(error),
                },
            ) from error

        if _paths_overlap(copyback_root, object_store_root) and copyback_root != object_store_root:
            _raise_copyback_root_overlap(copyback_root, object_store_root)
        return copyback_root

    def _validate_copyback_source_products(
        self,
        run_ids: list[str],
        *,
        copyback_root: Path,
        object_store_root: Path,
    ) -> None:
        for run_id in run_ids:
            run_key: str | None = None
            try:
                run_key = _run_product_key(run_id)
                source_tree = _collect_copyback_source_tree(self.object_store, run_key)
                self._validate_copyback_source_tree(run_id, source_tree)
            except FileNotFoundError as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
                    "Run products are missing from the object-store staging root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Failed to validate run products in the object-store staging root.",
                    _copyback_error_details(
                        run_id=run_id,
                        object_key=run_key,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error

    def _forcing_package_refs_for_runs(
        self,
        runs: list[dict[str, Any]],
        *,
        copyback_root: Path,
        object_store_root: Path,
    ) -> list[_ForcingPackageRef]:
        refs_by_key: dict[str, _ForcingPackageRef] = {}
        for run in runs:
            try:
                ref = self._forcing_package_ref_for_run(run)
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Forcing package metadata is missing or unsafe for q_down copyback.",
                    _forcing_metadata_error_details(
                        self.object_store,
                        run,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            existing_ref = refs_by_key.get(ref.object_key)
            if existing_ref is not None and existing_ref.checksum != ref.checksum:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Forcing package metadata is inconsistent for deduplicated q_down copyback.",
                    _forcing_copyback_error_details(
                        ref,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=ValueError(
                            "Forcing package checksum differs for the same normalized forcing package key."
                        ),
                    ),
                )
            refs_by_key.setdefault(ref.object_key, ref)
        return [refs_by_key[key] for key in sorted(refs_by_key)]

    def _forcing_package_ref_for_run(self, run: dict[str, Any]) -> _ForcingPackageRef:
        run_id = str(run.get("run_id") or "")
        forcing_version_id = str(run.get("forcing_version_id") or "").strip()
        if not forcing_version_id:
            raise ValueError("Missing forcing metadata field: forcing_version_id")
        if run.get("forcing_package_uri") in (None, "") and run.get("forcing_checksum") in (None, ""):
            raise ValueError("Missing forcing metadata field: forcing_version")
        package_uri = str(run.get("forcing_package_uri") or "").strip()
        if not package_uri:
            raise ValueError("Missing forcing metadata field: forcing_package_uri")
        checksum = str(run.get("forcing_checksum") or "").strip()
        if not checksum:
            raise ValueError("Missing forcing metadata field: checksum")
        object_key = _normalize_forcing_package_key(self.object_store, package_uri)
        lineage = run.get("forcing_lineage") if isinstance(run.get("forcing_lineage"), dict) else {}
        lineage_manifest_checksum = _optional_nonempty_string(lineage.get("forcing_package_manifest_checksum"))
        if lineage_manifest_checksum is not None and lineage_manifest_checksum != checksum:
            raise ValueError(
                "Forcing package lineage manifest checksum does not match met.forcing_version.checksum"
            )
        output_files = _lineage_output_files(lineage)
        return _ForcingPackageRef(
            run_id=run_id,
            forcing_version_id=forcing_version_id,
            object_key=object_key,
            checksum=checksum,
            lineage_manifest_checksum=lineage_manifest_checksum,
            output_files=output_files,
        )

    def _validate_qdown_copyback_sources(
        self,
        *,
        run_ids: list[str],
        forcing_refs: list[_ForcingPackageRef],
        copyback_root: Path,
        object_store_root: Path,
    ) -> None:
        self._validate_copyback_source_products(
            run_ids,
            copyback_root=copyback_root,
            object_store_root=object_store_root,
        )
        for ref in forcing_refs:
            try:
                source_tree = _collect_copyback_source_tree(self.object_store, ref.object_key)
                self._validate_forcing_source_tree(ref, source_tree)
            except FileNotFoundError as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
                    "Forcing package is missing from the object-store staging root.",
                    _forcing_copyback_error_details(
                        ref,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error
            except (ObjectStoreError, OSError, SafeFilesystemError, ValueError) as error:
                raise PublishError(
                    "OBJECT_STORE_COPYBACK_FAILED",
                    "Failed to validate forcing package in the object-store staging root.",
                    _forcing_copyback_error_details(
                        ref,
                        copyback_root=copyback_root,
                        object_store_root=object_store_root,
                        error=error,
                    ),
                ) from error

    def _copyback_object_tree(
        self,
        key: str,
        target_store: LocalObjectStore,
        *,
        validate_source_tree: Callable[[_CopybackSourceTree], None],
    ) -> dict[str, int]:
        key = self.object_store.normalize_key(key)
        source_tree = _collect_copyback_source_tree(self.object_store, key)
        validate_source_tree(source_tree)
        target_dir = _object_tree_root_path(target_store, key)
        temp_key = _copyback_temp_tree_key(key)
        temp_dir = _object_tree_root_path(target_store, temp_key)
        ensure_directory_no_follow(target_dir.parent, containment_root=target_store.root)

        file_count = 0
        byte_count = 0
        try:
            ensure_directory_no_follow(temp_dir, containment_root=target_store.root)
            for directory_key in source_tree.directories:
                temp_directory_key = _copyback_temp_key(directory_key, source_key=key, temp_key=temp_key)
                ensure_directory_no_follow(target_store.root / temp_directory_key, containment_root=target_store.root)
            for file_key in source_tree.files:
                content = self.object_store.read_bytes(file_key)
                temp_file_key = _copyback_temp_key(file_key, source_key=key, temp_key=temp_key)
                target_store.write_bytes_atomic(temp_file_key, content)
                file_count += 1
                byte_count += len(content)
            _chmod_tree_readable(temp_dir, containment_root=target_store.root)
            _replace_directory_tree_no_follow(temp_dir, target_dir, containment_root=target_store.root)
        except Exception:
            rmtree_no_follow(temp_dir, containment_root=target_store.root, missing_ok=True)
            raise

        return {"file_count": file_count, "byte_count": byte_count}

    def _validate_forcing_source_tree(self, ref: _ForcingPackageRef, source_tree: _CopybackSourceTree) -> None:
        files = set(source_tree.files)
        if not files:
            raise SafeFilesystemError(f"Forcing package tree is empty: {ref.object_key}")

        manifest_key = f"{ref.object_key}/forcing_package.json"
        if manifest_key not in files:
            raise SafeFilesystemError(f"Forcing package manifest is missing: {manifest_key}")

        manifest_bytes = self.object_store.read_bytes(manifest_key)
        manifest_checksum = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_checksum != ref.checksum:
            raise SafeFilesystemError(
                f"Forcing package manifest checksum mismatch: {manifest_key}"
            )
        if ref.lineage_manifest_checksum is not None and ref.lineage_manifest_checksum != ref.checksum:
            raise SafeFilesystemError(
                "Forcing package lineage manifest checksum does not match met.forcing_version.checksum: "
                f"{manifest_key}"
            )

        try:
            manifest_payload = json.loads(manifest_bytes.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise SafeFilesystemError(f"Forcing package manifest must be UTF-8 JSON: {manifest_key}") from error
        except json.JSONDecodeError as error:
            raise SafeFilesystemError(
                f"Forcing package manifest is not valid JSON: {manifest_key}: {error.msg}"
            ) from error

        output_files = [*ref.output_files]
        if isinstance(manifest_payload, dict):
            manifest_files = manifest_payload.get("files")
            if isinstance(manifest_files, list):
                output_files.extend(manifest_files)
        for output_file in output_files:
            output_key = _same_package_output_file_key(self.object_store, output_file, package_key=ref.object_key)
            if output_key is not None and output_key not in files:
                raise SafeFilesystemError(f"Forcing package output file is missing: {output_key}")

    def _validate_copyback_source_tree(self, run_id: str, source_tree: _CopybackSourceTree) -> None:
        files = set(source_tree.files)
        if not files:
            raise SafeFilesystemError(f"Run product tree is empty: runs/{run_id}")

        manifest_key = f"runs/{run_id}/input/manifest.json"
        if manifest_key not in files:
            raise SafeFilesystemError(f"Run product manifest is missing: {manifest_key}")
        if not _has_regular_file_under(files, f"runs/{run_id}/output"):
            raise SafeFilesystemError(f"Run product output files are missing: runs/{run_id}/output")
        if not _has_regular_file_under(files, f"runs/{run_id}/logs"):
            raise SafeFilesystemError(f"Run product log files are missing: runs/{run_id}/logs")

        try:
            manifest_payload = json.loads(self.object_store.read_bytes(manifest_key).decode("utf-8"))
        except UnicodeDecodeError as error:
            raise SafeFilesystemError(f"Run product manifest must be UTF-8 JSON: {manifest_key}") from error
        except json.JSONDecodeError as error:
            raise SafeFilesystemError(f"Run product manifest is not valid JSON: {manifest_key}: {error.msg}") from error

        if isinstance(manifest_payload, dict):
            manifest_run_id = manifest_payload.get("run_id")
            if manifest_run_id is None and "runId" in manifest_payload:
                manifest_run_id = manifest_payload["runId"]
            if manifest_run_id is not None and str(manifest_run_id) != run_id:
                raise SafeFilesystemError(
                    f"Run product manifest run_id does not match source run_id: {manifest_key}"
                )


def _safe_key_segment(value: Any) -> str:
    """Return a single safe path/key segment for a river_network_version_id.

    Keeps the segment matching ``_SAFE_ID_RE`` so it can never inject ``/`` or
    ``..`` into object-store keys. Unsafe characters collapse to ``-`` and an
    empty/None id falls back to ``default``.
    """
    raw = str(value or "").strip()
    if not raw:
        return "default"
    if _SAFE_ID_RE.fullmatch(raw):
        return raw
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "-", raw).strip("-") or "default"
    if not _SAFE_ID_RE.fullmatch(sanitized):
        sanitized = f"x-{sanitized}"
    return sanitized


def _run_product_key(run_id: str) -> str:
    run_id = run_id.strip()
    if not _SAFE_ID_RE.fullmatch(run_id):
        raise ValueError(f"Unsafe run_id for object-store copyback: {run_id!r}")
    return f"runs/{run_id}"


def _normalize_forcing_package_key(object_store: LocalObjectStore, package_uri: str) -> str:
    raw = package_uri.strip()
    if raw.startswith("/"):
        raise ValueError(f"Forcing package key must not be absolute: {package_uri!r}")
    _reject_empty_forcing_package_segment(raw)
    key = object_store.normalize_key(raw).rstrip("/")
    return _validate_forcing_package_key(key)


def _reject_empty_forcing_package_segment(raw: str) -> None:
    candidate = raw
    if candidate.startswith("s3://"):
        candidate = unquote(urlparse(candidate).path)
    candidate = candidate.strip("/")
    if "//" in candidate:
        raise ValueError(f"Forcing package key must not contain empty segments: {raw!r}")


def _validate_forcing_package_key(key: str) -> str:
    parts = PurePosixPath(key).parts
    if len(parts) != 5:
        raise ValueError(f"Forcing package key must use forcing/<source>/<cycle>/<basin>/<model>: {key!r}")
    if parts[0] != "forcing":
        raise ValueError(f"Forcing package key must start with forcing/: {key!r}")
    for part in parts:
        if not part or part in {".", ".."} or "/" in part:
            raise ValueError(f"Forcing package key has an unsafe segment: {key!r}")
        if not _SAFE_ID_RE.fullmatch(part):
            raise ValueError(f"Forcing package key segment is unsafe: {key!r}")
    return "/".join(parts)


def _object_tree_root_path(store: LocalObjectStore, key: str) -> Path:
    parts = PurePosixPath(key).parts
    if _is_run_tree_key(parts):
        return Path(store.root) / parts[0] / parts[1]
    if _is_forcing_tree_key(parts):
        return Path(store.root).joinpath(*parts)
    raise ValueError(f"Unsupported object-store copyback tree key: {key!r}")


def _copyback_temp_tree_key(key: str) -> str:
    parts = PurePosixPath(key).parts
    if _is_run_tree_key(parts):
        return f"runs/{parts[1]}.copyback.{uuid.uuid4().hex}"
    if _is_forcing_tree_key(parts):
        return "/".join((*parts[:-1], f"{parts[-1]}.copyback.{uuid.uuid4().hex}"))
    raise ValueError(f"Unsupported object-store copyback tree key: {key!r}")


def _is_run_tree_key(parts: tuple[str, ...]) -> bool:
    return len(parts) == 2 and parts[0] == "runs" and _SAFE_ID_RE.fullmatch(parts[1]) is not None


def _is_forcing_tree_key(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 5
        and parts[0] == "forcing"
        and all(_SAFE_ID_RE.fullmatch(part) for part in parts)
    )


def _copyback_tree_label(key: str) -> str:
    parts = PurePosixPath(key).parts
    if parts and parts[0] == "forcing":
        return "forcing package"
    return "run product"


def _copyback_tree_label_title(key: str) -> str:
    label = _copyback_tree_label(key)
    return "Forcing package" if label == "forcing package" else "Run product"


def _copyback_path_component_label(key: str) -> str:
    parts = PurePosixPath(key).parts
    return "forcing package" if parts and parts[0] == "forcing" else "run product"


def _run_id_from_product_key(key: str) -> str:
    parts = PurePosixPath(key).parts
    if not _is_run_tree_key(parts):
        raise ValueError(f"Unsupported object-store copyback tree key: {key!r}")
    return parts[1]


def _configured_path_no_resolve(path: Path | str) -> Path:
    configured = Path(path).expanduser()
    return configured if configured.is_absolute() else Path.cwd() / configured


def _reject_existing_symlink_components(path: Path) -> None:
    target = _configured_path_no_resolve(path)
    current = Path(target.anchor)
    for part in _absolute_path_parts(target):
        current = current / part
        try:
            entry_stat = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat path component {current}: {error}", kind="io") from error
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Path component must not be a symlink: {current}")
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise SafeFilesystemError(f"Path component is not a directory: {current}")


def _absolute_path_parts(path: Path) -> tuple[str, ...]:
    return tuple(part for part in path.parts if part not in {path.anchor, ""})


def _copyback_error_details(
    *,
    run_id: str,
    object_key: str | None,
    copyback_root: Path,
    object_store_root: Path,
    error: BaseException,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "run_id": run_id,
        "copyback_root": str(copyback_root),
        "object_store_root": str(object_store_root),
        "error": str(error),
        "error_type": type(error).__name__,
    }
    if object_key is not None:
        details["object_key"] = object_key
    return details


def _forcing_metadata_error_details(
    object_store: LocalObjectStore,
    run: dict[str, Any],
    *,
    copyback_root: Path,
    object_store_root: Path,
    error: BaseException,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "forcing_version_id": run.get("forcing_version_id"),
        "copyback_root": str(copyback_root),
        "object_store_root": str(object_store_root),
        "error": str(error),
        "error_type": type(error).__name__,
    }
    package_uri = run.get("forcing_package_uri")
    if package_uri is not None:
        details["forcing_package_uri"] = package_uri
    missing_field = _missing_forcing_field(run, error)
    if missing_field is not None:
        details["missing_field"] = missing_field
    try:
        if isinstance(package_uri, str) and package_uri.strip():
            details["object_key"] = _normalize_forcing_package_key(object_store, package_uri)
    except Exception:
        pass
    return details


def _forcing_copyback_error_details(
    ref: _ForcingPackageRef,
    *,
    copyback_root: Path,
    object_store_root: Path,
    error: BaseException,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "run_id": ref.run_id,
        "forcing_version_id": ref.forcing_version_id,
        "object_key": ref.object_key,
        "copyback_root": str(copyback_root),
        "object_store_root": str(object_store_root),
        "error": str(error),
        "error_type": type(error).__name__,
    }
    return details


def _missing_forcing_field(run: dict[str, Any], error: BaseException) -> str | None:
    message = str(error)
    marker = "Missing forcing metadata field: "
    if marker in message:
        return message.split(marker, 1)[1].split()[0]
    if not run.get("forcing_version_id"):
        return "forcing_version_id"
    if run.get("forcing_row_forcing_version_id") in (None, ""):
        return "forcing_version"
    if run.get("forcing_package_uri") in (None, ""):
        return "forcing_package_uri"
    if run.get("forcing_checksum") in (None, ""):
        return "checksum"
    return None


def _run_ids_for_forcing_key(object_store: LocalObjectStore, object_key: str, runs: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(run.get("run_id"))
        for run in runs
        if str(run.get("run_id") or "").strip()
        and _run_forcing_object_key(object_store, run) == object_key
    })


def _forcing_version_ids_for_key(
    object_store: LocalObjectStore,
    object_key: str,
    runs: list[dict[str, Any]],
) -> list[str]:
    return sorted({
        str(run.get("forcing_version_id"))
        for run in runs
        if str(run.get("forcing_version_id") or "").strip()
        and _run_forcing_object_key(object_store, run) == object_key
    })


def _run_forcing_object_key(object_store: LocalObjectStore, run: dict[str, Any]) -> str | None:
    package_uri = run.get("forcing_package_uri")
    if not isinstance(package_uri, str) or not package_uri.strip():
        return None
    try:
        return _normalize_forcing_package_key(object_store, package_uri)
    except ValueError:
        return None


def _raise_copyback_root_overlap(copyback_root: Path, object_store_root: Path) -> None:
    raise PublishError(
        "OBJECT_STORE_COPYBACK_FAILED",
        "Object-store copyback root must not overlap OBJECT_STORE_ROOT.",
        {
            "copyback_root": str(copyback_root),
            "object_store_root": str(object_store_root),
            "reason": "copyback_root_object_store_root_overlap",
        },
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    return _path_is_relative_to(left, right) or _path_is_relative_to(right, left)


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _collect_copyback_source_tree(store: LocalObjectStore, key: str) -> _CopybackSourceTree:
    root = Path(store.root)
    parts = PurePosixPath(key).parts
    if not (_is_run_tree_key(parts) or _is_forcing_tree_key(parts)):
        raise ValueError(f"Unsupported object-store copyback tree key: {key!r}")

    root_path = verify_directory_no_follow(root)
    root_fd = os.open(root_path, _COPYBACK_DIR_FLAGS)
    fd = root_fd
    try:
        for part in parts:
            next_fd = _open_copyback_child_dir(fd, part, root / "/".join(parts), tree_key=key)
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        directories: list[str] = []
        files: list[str] = []
        _collect_copyback_source_entries(fd, key, directories, files)
        return _CopybackSourceTree(directories=tuple(sorted(directories)), files=tuple(sorted(files)))
    finally:
        os.close(fd)
        if fd != root_fd:
            os.close(root_fd)


def _collect_copyback_source_entries(
    dir_fd: int,
    directory_key: str,
    directories: list[str],
    files: list[str],
) -> None:
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as error:
        message = f"Failed to list {_copyback_tree_label(directory_key)} directory: {directory_key}: {error}"
        raise SafeFilesystemError(message, kind="io") from error
    for name in names:
        if name in {"", ".", ".."} or "/" in name:
            raise SafeFilesystemError(
                f"Unsafe {_copyback_tree_label(directory_key)} entry name: {directory_key}/{name}"
            )
        entry_key = f"{directory_key}/{name}"
        try:
            entry_stat = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SafeFilesystemError(
                f"Failed to stat {_copyback_tree_label(directory_key)} entry: {entry_key}: {error}",
                kind="io",
            ) from error
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(
                f"{_copyback_tree_label_title(directory_key)} entry must not be a symlink: {entry_key}"
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_copyback_child_dir(dir_fd, name, Path(entry_key), tree_key=directory_key)
            try:
                directories.append(entry_key)
                _collect_copyback_source_entries(child_fd, entry_key, directories, files)
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(entry_stat.st_mode):
            files.append(entry_key)
            continue
        raise SafeFilesystemError(
            f"{_copyback_tree_label_title(directory_key)} entry must be a regular file or directory: {entry_key}"
        )


def _open_copyback_child_dir(parent_fd: int, name: str, path_label: Path, *, tree_key: str) -> int:
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SafeFilesystemError(
            f"Failed to stat {_copyback_path_component_label(tree_key)} directory: {path_label}: {error}",
            kind="io",
        ) from error
    if stat.S_ISLNK(expected.st_mode):
        raise SafeFilesystemError(
            f"{_copyback_tree_label_title(tree_key)} path component must not be a symlink: {path_label}"
        )
    if not stat.S_ISDIR(expected.st_mode):
        raise SafeFilesystemError(
            f"{_copyback_tree_label_title(tree_key)} path component is not a directory: {path_label}"
        )
    try:
        return os.open(name, _COPYBACK_DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except NotADirectoryError as error:
        raise SafeFilesystemError(
            f"{_copyback_tree_label_title(tree_key)} path component is not a directory: {path_label}"
        ) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SafeFilesystemError(
                f"{_copyback_tree_label_title(tree_key)} path component must not be a symlink: {path_label}"
            ) from error
        raise SafeFilesystemError(
            f"Failed to open {_copyback_path_component_label(tree_key)} directory: {path_label}: {error}",
            kind="io",
        ) from error


def _has_regular_file_under(files: set[str], directory_key: str) -> bool:
    prefix = f"{directory_key.rstrip('/')}/"
    return any(file_key.startswith(prefix) for file_key in files)


def _copyback_temp_key(object_key: str, *, source_key: str, temp_key: str) -> str:
    source_prefix = f"{source_key.rstrip('/')}/"
    if object_key == source_key:
        return temp_key
    if not object_key.startswith(source_prefix):
        raise ValueError(f"Copyback source key {object_key!r} is outside {source_key!r}")
    return f"{temp_key}/{object_key[len(source_prefix):]}"


def _replace_directory_tree_no_follow(temp_dir: Path, target_dir: Path, *, containment_root: Path) -> None:
    containment_root = containment_root.expanduser().resolve()
    temp_dir = temp_dir.expanduser()
    target_dir = target_dir.expanduser()
    temp_dir.relative_to(containment_root)
    target_dir.relative_to(containment_root)
    if temp_dir.parent != target_dir.parent:
        raise SafeFilesystemError("Copyback temporary and target directories must be siblings.")
    verify_directory_no_follow(temp_dir)
    ensure_directory_no_follow(target_dir.parent, containment_root=containment_root)
    backup_name = f".{target_dir.name}.copyback-backup.{uuid.uuid4().hex}"
    target_backed_up = False
    promoted = False
    parent_fd = os.open(target_dir.parent, _COPYBACK_DIR_FLAGS)
    try:
        if _directory_entry_exists(parent_fd, target_dir.name, target_dir):
            os.replace(target_dir.name, backup_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            target_backed_up = True
        os.replace(temp_dir.name, target_dir.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        promoted = True
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        if target_backed_up:
            try:
                rmtree_no_follow(target_dir.parent / backup_name, containment_root=containment_root)
            except (OSError, SafeFilesystemError) as error:
                raise SafeFilesystemError(
                    f"Failed to remove previous copyback target backup {target_dir.parent / backup_name}: {error}",
                    kind="io",
                ) from error
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
    except SafeFilesystemError:
        if target_backed_up and not promoted:
            _restore_copyback_backup(
                parent_fd,
                backup_name=backup_name,
                target_name=target_dir.name,
                target_dir=target_dir,
                containment_root=containment_root,
            )
        raise
    except OSError as error:
        if target_backed_up and not promoted:
            _restore_copyback_backup(
                parent_fd,
                backup_name=backup_name,
                target_name=target_dir.name,
                target_dir=target_dir,
                containment_root=containment_root,
            )
        raise SafeFilesystemError(f"Failed to replace copyback target tree {target_dir}: {error}", kind="io") from error
    finally:
        os.close(parent_fd)


def _directory_entry_exists(parent_fd: int, name: str, path_label: Path) -> bool:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SafeFilesystemError(f"Failed to stat copyback target tree {path_label}: {error}", kind="io") from error
    if stat.S_ISLNK(entry_stat.st_mode):
        raise SafeFilesystemError(f"Copyback target tree must not be a symlink: {path_label}")
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise SafeFilesystemError(f"Copyback target tree must be a directory: {path_label}")
    return True


def _restore_copyback_backup(
    parent_fd: int,
    *,
    backup_name: str,
    target_name: str,
    target_dir: Path,
    containment_root: Path,
) -> None:
    try:
        rmtree_no_follow(target_dir, containment_root=containment_root, missing_ok=True)
        os.replace(backup_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    except (OSError, SafeFilesystemError) as error:
        raise SafeFilesystemError(
            f"Failed to restore previous copyback target tree {target_dir}: {error}",
            kind="io",
        ) from error


def _chmod_tree_readable(root: Path, *, containment_root: Path) -> None:
    root = root.resolve()
    containment_root = containment_root.resolve()
    root.relative_to(containment_root)
    entries = [root, *sorted(root.rglob("*"))]
    for entry in entries:
        entry.relative_to(containment_root)
        entry_stat = entry.lstat()
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SafeFilesystemError(f"Copied object-store entry must not be a symlink: {entry}")
        if stat.S_ISDIR(entry_stat.st_mode):
            entry.chmod(0o755)
        elif stat.S_ISREG(entry_stat.st_mode):
            entry.chmod(0o644)
        else:
            raise SafeFilesystemError(f"Copied object-store entry must be a file or directory: {entry}")


def _qdown_isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _prefix_with_separator(prefix: str) -> str:
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _qdown_cycle_quality_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if runs:
        return {"quality_state": "degraded", "unavailable_products": [], "residual_blockers": []}
    return {
        "quality_state": "unavailable",
        "unavailable_products": ["q_down_timeseries"],
        "residual_blockers": [
            {
                "code": "NO_QDOWN_RUNS",
                "state": "unavailable",
                "residual_risk": "No parsed q_down river timeseries were found for the requested cycle.",
            }
        ],
    }


def _qdown_identity_blockers(error: PublishError, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract (or synthesize) residual blockers for an identity-incomplete run (F5)."""
    raw = error.details.get("residual_blockers")
    if isinstance(raw, list) and raw:
        return [dict(blocker) for blocker in raw if isinstance(blocker, dict)]
    return [
        {
            "code": "PUBLISH_IDENTITY_INCOMPLETE",
            "state": "unavailable",
            "run_id": run.get("run_id"),
            "model_id": run.get("model_id"),
            "residual_risk": (
                "Strict identity fields are NULL in hydro.hydro_run; "
                "q_down display cannot be published for this run without complete lineage."
            ),
        }
    ]


def _with_parsed_forcing_lineage(row: dict[str, Any]) -> dict[str, Any]:
    row["forcing_lineage"] = _parse_forcing_lineage(row.get("forcing_lineage_json"))
    return row


def _parse_forcing_lineage(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_parse_error": "invalid_json"}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _optional_nonempty_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lineage_output_files(lineage: dict[str, Any]) -> tuple[Any, ...]:
    raw = lineage.get("output_files")
    if isinstance(raw, list | tuple):
        return tuple(raw)
    return ()


def _same_package_output_file_key(
    object_store: LocalObjectStore,
    output_file: Any,
    *,
    package_key: str,
) -> str | None:
    candidate: Any
    if isinstance(output_file, dict):
        candidate = output_file.get("uri") or output_file.get("key") or output_file.get("path")
    else:
        candidate = output_file
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    value = candidate.strip()
    if value.startswith("s3://"):
        parsed = urlparse(value)
        value = unquote(parsed.path).strip("/")
    elif "://" in value:
        return None
    else:
        if PurePosixPath(value).is_absolute():
            return None
        value = value.strip("/")
    if not value:
        return None
    try:
        normalized = object_store.normalize_key(value)
    except ValueError:
        return None
    package_prefix = f"{package_key.rstrip('/')}/"
    if normalized.startswith(package_prefix):
        return normalized
    if normalized.startswith("forcing/"):
        return None
    if PurePosixPath(normalized).is_absolute():
        return None
    return f"{package_prefix}{normalized}"


def _is_private_display_path(path: str) -> bool:
    """Reject private workspace-only paths for display artifacts.

    Mirrors services/artifacts/reader.py::_local_path_needs_redaction with
    ``redact_absolute=True`` semantics: any absolute local path (bare or behind a
    ``file://`` scheme), plus /scratch, /tmp, and .nhms-runs even when relative,
    is workspace-only. ``published://``, relative object-store keys, and ``s3://``
    targets stay allowlisted. Replicated rather than imported to avoid a fragile
    dependency on a private cross-module helper.
    """
    raw = path.strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    # published:// and remote object-store schemes are allowlisted display targets.
    if parsed.scheme in ("published", "s3"):
        return False
    # file:// path or a bare path is decoded so %2e/%2f cannot smuggle escapes.
    candidate = parsed.path if parsed.scheme == "file" else raw
    try:
        candidate = unquote(candidate)
    except Exception:
        pass
    normalized = candidate.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if ".nhms-runs" in parts:
        return True
    if normalized in ("/scratch", "/tmp") or normalized.startswith(("/scratch/", "/tmp/")):
        return True
    # Any absolute local path (no scheme or file://) is workspace-only.
    if parsed.scheme in ("", "file") and PurePosixPath(normalized).is_absolute():
        return True
    return False


def _reject_private_display_uri(uri: str) -> None:
    if _is_private_display_path(uri):
        raise PublishError(
            "DISPLAY_BOUNDARY_VIOLATION",
            "Generated q_down display artifact URI is outside the published boundary.",
            {"uri": str(redact_payload(uri))},
        )


def failure_payload(cycle_id: str, error: PublishError) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "error_code": error.error_code,
        "error_message": error.message,
        "layers": [],
        "status": "failed_publish",
        **({"details": error.details} if error.details else {}),
    }


def _validate_cycle_id(cycle_id: str) -> str:
    cycle_id = cycle_id.strip()
    if not cycle_id:
        raise PublishError("CYCLE_ID_REQUIRED", "cycle_id is required for tile publication.")
    if not _SAFE_ID_RE.match(cycle_id):
        raise PublishError("INVALID_CYCLE_ID", f"Invalid cycle_id: {cycle_id}")
    return cycle_id


def _normalize_metadata_items(raw_items: Any, *, item_id_key: str) -> list[dict[str, Any]]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise PublishError(
            "INVALID_PUBLISH_METADATA",
            f"Publish metadata field for {item_id_key} entries must be a list.",
        )

    items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata layer/artifact entries must be objects.",
                {"index": index},
            )
        item = dict(raw_item)
        identifiers = [
            item.get(item_id_key),
            item.get("uri"),
            item.get("tile_uri"),
            item.get("tile_uri_template"),
        ]
        if not any(isinstance(identifier, str) and identifier.strip() for identifier in identifiers):
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata layer/artifact entries must include an identifier or URI.",
                {"index": index},
            )
        items.append(item)
    return items


def _publish_metadata_lineage(metadata: dict[str, Any], cycle_id: str, artifact_uri: str) -> dict[str, Any]:
    raw_lineage = metadata.get("lineage")
    lineage = dict(raw_lineage) if isinstance(raw_lineage, dict) else {}
    lineage["cycle_id"] = cycle_id
    lineage.setdefault("metadata_uri", artifact_uri)
    lineage.setdefault("published_basins", 0)
    lineage.setdefault("source_run_ids", [])
    return lineage


def _metadata_source_run_ids(metadata: dict[str, Any]) -> set[str]:
    raw_lineage = metadata.get("lineage")
    if not isinstance(raw_lineage, dict):
        return set()
    raw_source_run_ids = raw_lineage.get("source_run_ids")
    if not isinstance(raw_source_run_ids, list):
        return set()
    return {run_id.strip() for run_id in raw_source_run_ids if isinstance(run_id, str) and run_id.strip()}


def _quality_flags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return sorted({item.strip() for item in value.split(",") if item.strip()})
    if isinstance(value, list | tuple | set):
        return sorted({str(item).strip() for item in value if str(item).strip()})
    return [str(value)]


def _publish_run_unavailable_products(run: dict[str, Any]) -> list[str]:
    unavailable: list[str] = []
    result_rows = int(run.get("result_rows") or 0)
    return_period_rows = int(run.get("return_period_rows") or 0)
    warning_rows = int(run.get("warning_rows") or 0)
    if return_period_rows <= 0:
        unavailable.append("return_period_result")
    elif result_rows > return_period_rows:
        unavailable.append("frequency_curves")
    if return_period_rows > 0 and warning_rows < return_period_rows:
        unavailable.append("warning_thresholds")
    return unavailable


def _publish_run_quality_state(run: dict[str, Any]) -> str:
    return "ready" if not _publish_run_unavailable_products(run) else "unavailable"


def _publish_run_residual_blockers(run: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    result_rows = int(run.get("result_rows") or 0)
    return_period_rows = int(run.get("return_period_rows") or 0)
    warning_rows = int(run.get("warning_rows") or 0)
    if return_period_rows <= 0:
        blockers.append(
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run.get("run_id"),
                "model_id": run.get("model_id"),
                "quality_flags": _quality_flags(run.get("quality_flags")),
                "residual_risk": "No non-null return-period peak rows are publishable for this run.",
            }
        )
    elif result_rows > return_period_rows:
        blockers.append(
            {
                "code": "FREQUENCY_CURVES_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run.get("run_id"),
                "model_id": run.get("model_id"),
                "quality_flags": _quality_flags(run.get("quality_flags")),
                "residual_risk": "Some peak rows have null return_period because frequency curves are unavailable.",
            }
        )
    if return_period_rows > 0 and warning_rows < return_period_rows:
        blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run.get("run_id"),
                "model_id": run.get("model_id"),
                "quality_flags": _quality_flags(run.get("quality_flags")),
                "residual_risk": "warning_level remains null for published return-period rows.",
            }
        )
    return blockers


def _display_blocker_code(row: Any) -> str:
    try:
        result_rows = int(row.get("result_rows") or 0)
        return_period_rows = int(row.get("return_period_rows") or 0)
        warning_rows = int(row.get("warning_rows") or 0)
    except AttributeError:
        return "DISPLAY_PRODUCT_UNAVAILABLE"
    if result_rows > 0 and return_period_rows <= 0:
        return "RETURN_PERIOD_RESULT_UNAVAILABLE"
    if result_rows > return_period_rows:
        return "FREQUENCY_CURVES_UNAVAILABLE"
    if return_period_rows > 0 and warning_rows < return_period_rows:
        return "WARNING_THRESHOLDS_UNAVAILABLE"
    return "DISPLAY_PRODUCT_UNAVAILABLE"


def _display_unavailable_products(code: str) -> list[str]:
    if code == "WARNING_THRESHOLDS_UNAVAILABLE":
        return ["warning_thresholds"]
    if code == "FREQUENCY_CURVES_UNAVAILABLE":
        return ["frequency_curves"]
    if code == "HYDRO_RUN_LINEAGE_UNAVAILABLE":
        return ["hydro_run_lineage"]
    return ["return_period_result"]


def _display_blocker_message(code: str) -> str:
    if code == "WARNING_THRESHOLDS_UNAVAILABLE":
        return "warning_level remains null for max-over-window return-period rows."
    if code == "FREQUENCY_CURVES_UNAVAILABLE":
        return "Some max-over-window rows have null return_period because frequency curves are unavailable."
    return "No max-over-window return-period rows are publishable for this run."


def _validate_metadata_delivery_references(
    object_store: LocalObjectStore,
    *,
    layers: tuple[dict[str, Any], ...],
    artifacts: tuple[dict[str, Any], ...],
    cycle_id: str,
    source_run_ids: set[str],
    metadata_uri: str,
) -> None:
    expected_prefix = f"tiles/hydro/{cycle_id}/"
    for item_type, items in (("layer", layers), ("artifact", artifacts)):
        for index, item in enumerate(items):
            for field in _DELIVERY_REFERENCE_FIELDS:
                if field not in item:
                    continue
                value = item[field]
                if not isinstance(value, str) or not value.strip():
                    raise PublishError(
                        "INVALID_PUBLISH_METADATA",
                        f"Publish metadata {item_type} {field} must be a non-empty string.",
                        {"field": field, "index": index, "item_type": item_type, "metadata_uri": metadata_uri},
                    )
                _validate_metadata_delivery_reference(
                    object_store,
                    value.strip(),
                    cycle_id=cycle_id,
                    expected_prefix=expected_prefix,
                    source_run_ids=source_run_ids,
                    field=field,
                    index=index,
                    item_type=item_type,
                    metadata_uri=metadata_uri,
                )


def _validate_metadata_delivery_reference(
    object_store: LocalObjectStore,
    reference: str,
    *,
    cycle_id: str,
    expected_prefix: str,
    source_run_ids: set[str],
    field: str,
    index: int,
    item_type: str,
    metadata_uri: str,
) -> None:
    if _is_valid_tile_api_reference(reference, cycle_id=cycle_id, source_run_ids=source_run_ids):
        return

    try:
        normalized_key = object_store.normalize_key(reference)
    except ValueError as error:
        raise PublishError(
            "INVALID_PUBLISH_METADATA",
            "Publish metadata delivery reference is outside the configured object-store prefix.",
            {"field": field, "index": index, "item_type": item_type, "metadata_uri": metadata_uri},
        ) from error
    if not normalized_key.startswith(expected_prefix):
        raise PublishError(
            "INVALID_PUBLISH_METADATA",
            "Publish metadata delivery reference is outside the requested cycle object-store prefix.",
            {
                "expected_prefix": expected_prefix,
                "field": field,
                "index": index,
                "item_type": item_type,
                "metadata_uri": metadata_uri,
            },
        )


def _is_valid_tile_api_reference(reference: str, *, cycle_id: str, source_run_ids: set[str]) -> bool:
    if not reference.startswith(_FLOOD_TILE_API_PATH):
        return False
    parsed = urlparse(reference)
    if parsed.path != _FLOOD_TILE_API_PATH:
        return False
    run_ids = [value.strip() for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key == "run_id"]
    if len(run_ids) != 1 or not run_ids[0]:
        return False
    run_id = run_ids[0]
    return run_id in source_run_ids and _run_id_belongs_to_cycle(run_id, cycle_id=cycle_id)


def _run_id_belongs_to_cycle(run_id: str, *, cycle_id: str) -> bool:
    cycle_id_lower = cycle_id.lower()
    run_id_lower = run_id.lower()
    return any(match.group(0).lower() == cycle_id_lower for match in _CYCLE_TOKEN_RE.finditer(run_id_lower))


def _cycle_filter(cycle_id: str) -> dict[str, Any] | None:
    parts = cycle_id.split("_", 1)
    if len(parts) != 2:
        return None
    source_id, compact_time = parts
    if len(compact_time) != 10 or not compact_time.isdigit():
        return None
    try:
        from workers.data_adapters.base import parse_cycle_time

        cycle_time = parse_cycle_time(compact_time)
        expected_cycle_id = cycle_id_for(source_id, cycle_time)
    except (TypeError, ValueError):
        return None
    if expected_cycle_id.lower() != cycle_id.lower():
        return None
    return {"source_id": source_id, "cycle_time": cycle_time, "compact_time": format_cycle_time(cycle_time)}


def _has_table(session: Session, schema: str, table_name: str) -> bool:
    return inspect(session.connection()).has_table(table_name, schema=schema)


def _has_optional_table(session: Session, schema: str, table_name: str) -> bool:
    try:
        return _has_table(session, schema, table_name)
    except Exception:
        return False


def _table_columns(session: Session, schema: str, table_name: str) -> set[str]:
    try:
        return {column["name"] for column in inspect(session.connection()).get_columns(table_name, schema=schema)}
    except Exception:
        return set()
