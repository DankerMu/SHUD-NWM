from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.common.object_store import LocalObjectStore
from workers.data_adapters.base import cycle_id_for, format_cycle_time

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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


class TilePublisher:
    """Register existing flood return-period products as map delivery evidence."""

    def __init__(
        self,
        *,
        workspace_root: Path | str,
        object_store_root: Path | str,
        object_store_prefix: str = "",
        database_url: str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.object_store = LocalObjectStore(object_store_root, object_store_prefix=object_store_prefix)
        self.database_url = (database_url or "").strip()

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

        runs = self._discover_publishable_runs(session, cycle_id)
        if not runs:
            raise PublishError(
                "NO_PUBLISHABLE_PRODUCTS",
                f"No publishable flood return-period products found for cycle_id={cycle_id}.",
                {"cycle_id": cycle_id},
            )

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

        self._mark_runs_published(session, [str(run["run_id"]) for run in runs])
        session.commit()
        layers.sort(key=lambda layer: str(layer["layer_id"]))
        artifacts.sort(key=lambda artifact: str(artifact["artifact_id"]))
        return PublishResult(
            cycle_id=cycle_id,
            status="published",
            layers=tuple(layers),
            artifacts=tuple(artifacts),
            lineage={
                "cycle_id": cycle_id,
                "published_basins": len(runs),
                "source_run_ids": [str(run["run_id"]) for run in runs],
            },
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

        rows = session.execute(
            text(
                f"""
                SELECT h.run_id, h.scenario_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time,
                       COUNT(r.river_segment_id) AS result_rows,
                       COUNT(DISTINCT r.river_segment_id) AS segment_count
                FROM hydro.hydro_run h
                JOIN flood.return_period_result r ON r.run_id = h.run_id
                WHERE {' AND '.join(where_clauses)}
                GROUP BY h.run_id, h.scenario_id, h.model_id, h.basin_version_id, h.source_id, h.cycle_time
                ORDER BY h.run_id
                """
            ),
            params,
        ).mappings()
        return [dict(row) for row in rows if int(row["result_rows"] or 0) > 0]

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
        return {
            "layer_id": layer_id,
            "layer_type": "flood_return_period",
            "source_run_id": run_id,
            "source_product_id": values["source_product_id"],
            "tile_format": "geojson",
            "tile_uri_template": tile_uri_template,
            "published_flag": True,
            "segment_count": int(run.get("segment_count") or 0),
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
        _validate_metadata_artifact_uris(self.object_store, artifacts, metadata_uri=metadata_uri)
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


def _validate_metadata_artifact_uris(
    object_store: LocalObjectStore,
    items: tuple[dict[str, Any], ...],
    *,
    metadata_uri: str,
) -> None:
    for index, item in enumerate(items):
        uri = item.get("uri") or item.get("tile_uri")
        if not isinstance(uri, str) or not uri.strip() or not uri.startswith("s3://"):
            continue
        try:
            object_store.normalize_key(uri)
        except ValueError as error:
            raise PublishError(
                "INVALID_PUBLISH_METADATA",
                "Publish metadata artifact URI is outside the configured object-store prefix.",
                {"index": index, "metadata_uri": metadata_uri},
            ) from error


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


def _table_columns(session: Session, schema: str, table_name: str) -> set[str]:
    try:
        return {column["name"] for column in inspect(session.connection()).get_columns(table_name, schema=schema)}
    except Exception:
        return set()
