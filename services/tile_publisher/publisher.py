from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.common.object_store import LocalObjectStore
from workers.data_adapters.base import cycle_id_for, format_cycle_time

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CYCLE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_\d{10}")
_FLOOD_TILE_API_PATH = "/api/v1/tiles/flood-return-period"
_DELIVERY_REFERENCE_FIELDS = ("uri", "tile_uri", "tile_uri_template")


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

        cycle_quality = self._cycle_display_quality_state(session, cycle_id)
        runs = self._discover_publishable_runs(session, cycle_id)
        if not runs:
            raise PublishError(
                "NO_PUBLISHABLE_PRODUCTS",
                f"No publishable flood return-period products found for cycle_id={cycle_id}.",
                {"cycle_id": cycle_id, **cycle_quality},
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
                "quality_state": "ready" if cycle_quality["quality_state"] == "ready" else "degraded",
                "unavailable_products": list(cycle_quality["unavailable_products"]),
                "residual_blockers": list(cycle_quality["residual_blockers"]),
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


def _table_columns(session: Session, schema: str, table_name: str) -> set[str]:
    try:
        return {column["name"] for column in inspect(session.connection()).get_columns(table_name, schema=schema)}
    except Exception:
        return set()
