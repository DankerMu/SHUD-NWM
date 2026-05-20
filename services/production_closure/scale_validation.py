from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
)

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:_-]{0,127}$")
SENSITIVE_PREFIX_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;?#&/])[^=/?#;&]*(?:token|password|passwd|pwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"session[_-]?key|signature|x-amz-signature)[^=/?#;&]*=",
    re.IGNORECASE,
)
SENSITIVE_PREFIX_SEPARATOR_RE = re.compile(r"[/;?#&]")

DEFAULT_DATASET_SOURCE = "deterministic_large_fixture"
DEFAULT_SEGMENT_COUNT = 125_000
DEFAULT_MODEL_COUNT = 32
DEFAULT_MIN_SEGMENT_COUNT = 100_000
DEFAULT_MIN_MODEL_COUNT = 16
DEFAULT_TILE_CONTENT_TYPE_EXPECTATION = "application/geo+json"
VALID_TILE_CONTENT_TYPE_EXPECTATIONS = {"application/geo+json", "application/json", "application/x-protobuf"}
DEFAULT_BBOX_SET = ("national", "yangtze", "urban")
DEFAULT_FRONTEND_BREAKPOINTS = ("desktop:1440x900", "mobile:390x844")
DEFAULT_THRESHOLDS_VERSION = "m10-scale-thresholds-v1"
MAX_EVIDENCE_PAYLOAD_BYTES = 768 * 1024
MAX_SAMPLE_COUNT = 128
MAX_OBJECT_LISTING_COUNT = 10_000
MAX_PERCENT_DECODE_ROUNDS = 4
MVT_MISSING_IMPLEMENTATION_WORK = [
    "Opt-in live PostGIS/national-data execution evidence from the target environment",
    "Browser proof against real national MVT tiles rather than deterministic fixture metadata",
]
MVT_ENDPOINT_REFERENCES = [
    "/api/v1/tiles/flood-return-period",
    "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf",
    "/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf",
]
MVT_DETERMINISTIC_BLOCKER_ID = "m16-deterministic-mvt-contract-artifact"
MVT_LIVE_POSTGIS_BLOCKER_ID = "m16-live-postgis-national-proof"
TILE_BYTES_BLOCKER_ID = "m16-tile-byte-budget"
MVT_CONTRACT_P95_THRESHOLD_KEY = "flood_alert_map_ms"
MVT_CONTRACT_BROWSER_TIMING_THRESHOLD_NAME = "frontend_render_ms"
MVT_CONTRACT_MIN_TILE_COUNT = 1
MVT_CONTRACT_MIN_FEATURE_COUNT = 1
MVT_CONTRACT_MIN_COORDINATE_COUNT = 1
MVT_CONTRACT_NUMERIC_FIELDS = (
    "payload_bytes",
    "p95_ms",
    "tile_count",
    "feature_count",
    "coordinate_count",
    "browser_timing_ms",
)
MVT_CONTRACT_STRING_FIELDS = ("sql_shape_hash", "query_plan_hash")
MVT_CONTRACT_ALLOWED_FIELDS = frozenset(
    {
        "observed_content_type",
        "raw_tile_bytes_observed",
        "artifact_paths",
        *MVT_CONTRACT_STRING_FIELDS,
        *MVT_CONTRACT_NUMERIC_FIELDS,
    }
)

QUERY_TARGETS = {
    "model_listing": {
        "endpoint": "/api/v1/models",
        "row_count": DEFAULT_MODEL_COUNT,
        "latency_samples_ms": (42.0, 44.0, 43.0, 45.0, 46.0),
        "threshold_key": "model_listing_ms",
        "plan_lines": (
            "Limit  (cost=0.42..18.00 rows=32 width=96)",
            "  -> Index Scan using model_instance_pkey on core.model_instance",
        ),
    },
    "river_bbox": {
        "endpoint": "/api/v1/basin-versions/{basin_version_id}/river-segments?bbox={bbox}",
        "row_count": 18_500,
        "latency_samples_ms": (158.0, 165.0, 171.0, 168.0, 174.0),
        "threshold_key": "river_bbox_ms",
        "plan_lines": (
            "Bitmap Heap Scan on core.river_segment",
            "  Recheck Cond: (geom && st_transform(st_makeenvelope(...), 4490))",
            "  -> Bitmap Index Scan on river_segment_geom_gix",
        ),
    },
    "flood_alert_summary": {
        "endpoint": "/api/v1/flood-alerts/summary",
        "row_count": 8,
        "latency_samples_ms": (54.0, 57.0, 59.0, 58.0, 60.0),
        "threshold_key": "flood_alert_summary_ms",
        "plan_lines": (
            "Aggregate  (cost=120.00..120.01 rows=1 width=64)",
            "  -> Index Scan using return_period_result_run_valid_idx on flood.return_period_result",
        ),
    },
    "flood_alert_ranking": {
        "endpoint": "/api/v1/flood-alerts/ranking",
        "row_count": 100,
        "latency_samples_ms": (66.0, 68.0, 71.0, 70.0, 72.0),
        "threshold_key": "flood_alert_ranking_ms",
        "plan_lines": (
            "Limit  (cost=240.00..245.00 rows=100 width=128)",
            "  -> Index Scan using return_period_result_rank_idx on flood.return_period_result",
        ),
    },
    "flood_alert_timeline": {
        "endpoint": "/api/v1/flood-alerts/timeline",
        "row_count": 168,
        "latency_samples_ms": (76.0, 78.0, 83.0, 80.0, 84.0),
        "threshold_key": "flood_alert_timeline_ms",
        "plan_lines": (
            "GroupAggregate  (cost=340.00..390.00 rows=168 width=72)",
            "  -> Index Scan using return_period_result_time_idx on flood.return_period_result",
        ),
    },
    "flood_alert_map": {
        "endpoint": "/api/v1/tiles/flood-return-period?run_id={run_id}&duration=1h&valid_time={valid_time}",
        "row_count": 21_000,
        "latency_samples_ms": (205.0, 214.0, 221.0, 218.0, 224.0),
        "threshold_key": "flood_alert_map_ms",
        "plan_lines": (
            "Nested Loop Left Join  (cost=480.00..850.00 rows=21000 width=256)",
            "  -> Index Scan using return_period_result_tile_idx on flood.return_period_result",
            "  -> Index Scan using river_segment_pkey on core.river_segment",
        ),
    },
    "forecast_series": {
        "endpoint": "/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series",
        "row_count": 168,
        "latency_samples_ms": (62.0, 65.0, 64.0, 67.0, 69.0),
        "threshold_key": "forecast_series_ms",
        "plan_lines": (
            "Index Scan using forecast_series_segment_time_idx on hydro.forecast_series",
            "  Index Cond: ((river_segment_id = $1) AND (issue_time = $2))",
        ),
    },
    "jobs": {
        "endpoint": "/api/v1/jobs",
        "row_count": 500,
        "latency_samples_ms": (45.0, 47.0, 49.0, 48.0, 50.0),
        "threshold_key": "jobs_ms",
        "plan_lines": (
            "Index Scan using pipeline_job_created_at_idx on ops.pipeline_job",
            "  Filter: (created_at >= now() - '7 days'::interval)",
        ),
    },
    "job_logs": {
        "endpoint": "/api/v1/jobs/{job_id}/logs",
        "row_count": 200,
        "latency_samples_ms": (38.0, 41.0, 42.0, 40.0, 43.0),
        "threshold_key": "job_logs_ms",
        "plan_lines": (
            "Index Scan using pipeline_job_log_job_id_idx on ops.pipeline_job_log",
            "  Index Cond: (job_id = $1)",
        ),
    },
    "tile_metadata": {
        "endpoint": "/api/v1/tiles/flood-return-period",
        "row_count": 24,
        "latency_samples_ms": (31.0, 34.0, 33.0, 35.0, 36.0),
        "threshold_key": "tile_metadata_ms",
        "plan_lines": (
            "Nested Loop Left Join  (cost=0.27..16.64 rows=24 width=256)",
            "  -> Index Scan using tile_layer_pkey on map.tile_layer",
            "       Filter: ((layer_type = 'flood_return_period') AND published_flag)",
            "  -> Index Scan using tile_cache_pkey on map.tile_cache",
            "       Index Cond: (layer_id = map.tile_layer.layer_id)",
        ),
    },
}


class ProductionScaleValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ProductionScaleThresholds:
    version: str = DEFAULT_THRESHOLDS_VERSION
    min_segment_count: int = DEFAULT_MIN_SEGMENT_COUNT
    min_model_count: int = DEFAULT_MIN_MODEL_COUNT
    p95_query_ms: Mapping[str, float] = field(default_factory=dict)
    max_tile_bytes: int = 5_000_000
    frontend_load_ms: int = 2_500
    frontend_render_ms: int = 1_500
    frontend_timeline_ms: int = 300
    frontend_chart_ms: int = 500
    frontend_memory_mb: int = 384
    oversized_bbox_behavior: str = "reject_or_require_bbox_pagination"
    long_time_range_behavior: str = "reject_over_7_days_or_require_aggregation"
    object_listing_limit: int = MAX_OBJECT_LISTING_COUNT

    @classmethod
    def default(cls) -> ProductionScaleThresholds:
        return cls(
            p95_query_ms={
                "model_listing_ms": 100.0,
                "river_bbox_ms": 250.0,
                "flood_alert_summary_ms": 125.0,
                "flood_alert_ranking_ms": 150.0,
                "flood_alert_timeline_ms": 160.0,
                "flood_alert_map_ms": 300.0,
                "forecast_series_ms": 150.0,
                "jobs_ms": 120.0,
                "job_logs_ms": 120.0,
                "tile_metadata_ms": 100.0,
            }
        )


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    max_payload_bytes: int = MAX_EVIDENCE_PAYLOAD_BYTES
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        _refuse_symlink_components(self.evidence_root)
        _refuse_symlink_components(self.lane_dir.parent)
        if self.lane_dir.exists() or self.lane_dir.is_symlink():
            _refuse_symlink_components(self.lane_dir)
            if not self.lane_dir.is_dir():
                raise ProductionScaleValidationError(
                    "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE",
                    f"Evidence lane path must be a directory: {self.lane_dir}.",
                )
            if any(self.lane_dir.iterdir()) and not self.force:
                raise ProductionScaleValidationError(
                    "PRODUCTION_SCALE_EVIDENCE_EXISTS",
                    f"Evidence bundle already exists: {self.lane_dir}. Use --force to overwrite an existing run_id.",
                )
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_lane.relative_to(self.evidence_root)
        except ValueError as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under evidence root.",
            ) from error
        try:
            ensure_directory_no_follow(self.evidence_root)
            ensure_directory_no_follow(self.lane_dir, containment_root=self.evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_SCALE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionScaleValidationError(
                error_code,
                f"Failed to prepare evidence lane {self.lane_dir}: {error}",
            ) from error

    def write_json(self, path: Path, payload: Any) -> None:
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if len(content) > self.max_payload_bytes:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_PAYLOAD_TOO_LARGE",
                f"Evidence payload exceeds configured limit of {self.max_payload_bytes} bytes.",
            )
        self._write_bytes(path, content)

    def _write_bytes(self, path: Path, content: bytes) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite an existing run_id bundle.",
            )
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_SCALE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionScaleValidationError(
                error_code,
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error
        except OSError as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_WRITE_FAILED",
                f"Failed to write evidence file {safe_path}: {error}",
            ) from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_SYMLINK",
                f"Evidence file must not be a symlink: {path}",
            )
        _refuse_symlink_components(path.parent)
        resolved_parent = path.parent.resolve(strict=False)
        resolved_lane = self.lane_dir.resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_lane)
        except ValueError as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under the current scale lane directory.",
            ) from error
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.lane_dir)
        except SafeFilesystemError as error:
            error_code = (
                "PRODUCTION_SCALE_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "PRODUCTION_SCALE_EVIDENCE_PATH_UNSAFE"
            )
            raise ProductionScaleValidationError(
                error_code,
                f"Failed to prepare evidence file parent {path.parent}: {error}",
            ) from error
        return resolved_parent / path.name


@dataclass(frozen=True)
class ProductionScaleConfig:
    evidence_root: Path
    run_id: str
    dataset_source: str
    segment_count: int
    model_count: int
    min_segment_count: int
    min_model_count: int
    bbox_set: tuple[str, ...]
    tile_content_type_expectation: str
    frontend_breakpoints: tuple[str, ...]
    api_base_url: str
    object_prefix: str
    thresholds: ProductionScaleThresholds
    thresholds_file: Path | None = None
    latency_fixture: str = "valid"
    mvt_contract_artifact: Path | None = None
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "scale"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path,
        run_id: str | None,
        dataset_source: str | None = None,
        segment_count: int | None = None,
        model_count: int | None = None,
        min_segment_count: int | None = None,
        min_model_count: int | None = None,
        bbox_set: str | None = None,
        thresholds_file: Path | None = None,
        tile_content_type_expectation: str | None = None,
        frontend_breakpoints: str | None = None,
        api_base_url: str | None = None,
        object_prefix: str | None = None,
        latency_fixture: str | None = None,
        mvt_contract_artifact: Path | None = None,
        force: bool = False,
    ) -> ProductionScaleConfig:
        resolved_evidence_root = _safe_resolved_evidence_root(evidence_root)
        resolved_run_id = _safe_run_id(run_id or datetime.now(UTC).strftime("m10-%Y%m%dT%H%M%SZ"))
        resolved_thresholds_file = thresholds_file or _optional_path_env("NHMS_PRODUCTION_SCALE_THRESHOLDS_FILE")
        thresholds = _load_thresholds(
            resolved_thresholds_file,
            min_segment_count=min_segment_count,
            min_model_count=min_model_count,
        )
        resolved_object_prefix = object_prefix or os.getenv(
            "NHMS_PRODUCTION_SCALE_OBJECT_PREFIX",
            f"s3://nhms-production-like/scale/{resolved_run_id}",
        )
        _validate_object_prefix_safe(resolved_object_prefix)
        resolved_api_base = api_base_url or os.getenv("NHMS_PRODUCTION_SCALE_API_BASE_URL", "deterministic-scale-api")
        _validate_api_base_url(resolved_api_base)
        return cls(
            evidence_root=resolved_evidence_root,
            run_id=resolved_run_id,
            dataset_source=dataset_source
            or os.getenv("NHMS_PRODUCTION_SCALE_DATASET_SOURCE", DEFAULT_DATASET_SOURCE),
            segment_count=segment_count
            if segment_count is not None
            else _positive_int_env("NHMS_PRODUCTION_SCALE_SEGMENT_COUNT", DEFAULT_SEGMENT_COUNT),
            model_count=model_count
            if model_count is not None
            else _positive_int_env("NHMS_PRODUCTION_SCALE_MODEL_COUNT", DEFAULT_MODEL_COUNT),
            min_segment_count=thresholds.min_segment_count,
            min_model_count=thresholds.min_model_count,
            bbox_set=_parse_csv_tuple(bbox_set or os.getenv("NHMS_PRODUCTION_SCALE_BBOX_SET"), DEFAULT_BBOX_SET),
            tile_content_type_expectation=(
                tile_content_type_expectation
                or os.getenv("NHMS_PRODUCTION_SCALE_TILE_CONTENT_TYPE_EXPECTATION")
                or DEFAULT_TILE_CONTENT_TYPE_EXPECTATION
            ),
            frontend_breakpoints=_parse_csv_tuple(
                frontend_breakpoints or os.getenv("NHMS_PRODUCTION_SCALE_FRONTEND_BREAKPOINTS"),
                DEFAULT_FRONTEND_BREAKPOINTS,
            ),
            api_base_url=resolved_api_base,
            object_prefix=resolved_object_prefix,
            thresholds=thresholds,
            thresholds_file=resolved_thresholds_file,
            latency_fixture=latency_fixture or os.getenv("NHMS_PRODUCTION_SCALE_LATENCY_FIXTURE", "valid"),
            mvt_contract_artifact=mvt_contract_artifact
            or _optional_path_env("NHMS_PRODUCTION_SCALE_MVT_CONTRACT_ARTIFACT"),
            force=force,
        )


def validate_scale(config: ProductionScaleConfig) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    _validate_config(config)
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    preflight = _preflight_payload(config)
    writer.write_json(config.lane_dir / "preflight.json", preflight)

    dataset_manifest = _dataset_manifest(config)
    writer.write_json(config.lane_dir / "dataset_manifest.json", dataset_manifest)

    thresholds = _thresholds_payload(config)
    writer.write_json(config.lane_dir / "thresholds.json", thresholds)

    query_evidence = _query_latency_evidence(config, dataset_manifest)
    writer.write_json(config.lane_dir / "query_latency_evidence.json", query_evidence)

    tile_evidence = _tile_evidence(config, dataset_manifest)
    writer.write_json(config.lane_dir / "tile_evidence.json", tile_evidence)

    frontend_evidence = _frontend_large_layer_evidence(config, dataset_manifest)
    writer.write_json(config.lane_dir / "frontend_large_layer_evidence.json", frontend_evidence)

    resource_evidence = _resource_bounds_evidence(config)
    writer.write_json(config.lane_dir / "resource_bounds_evidence.json", resource_evidence)

    environment = _environment_payload(config)
    writer.write_json(config.lane_dir / "environment.json", environment)

    blockers = _summary_blockers(dataset_manifest, query_evidence, tile_evidence, frontend_evidence, resource_evidence)
    status = "ready" if not blockers else "blocked"
    summary = _summary(
        config,
        status=status,
        blockers=blockers,
        dataset_manifest=dataset_manifest,
        query_evidence=query_evidence,
        tile_evidence=tile_evidence,
        frontend_evidence=frontend_evidence,
    )
    writer.write_json(config.lane_dir / "summary.json", summary)
    return summary


def _preflight_payload(config: ProductionScaleConfig) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.scale.preflight.v1",
        "issue": 151,
        "run_id": config.run_id,
        "dataset_source": config.dataset_source,
        "minimum_counts": {
            "segment_count": config.min_segment_count,
            "model_count": config.min_model_count,
        },
        "configured_counts": {
            "segment_count": config.segment_count,
            "model_count": config.model_count,
        },
        "bbox_set": list(config.bbox_set),
        "thresholds_file": str(config.thresholds_file) if config.thresholds_file else "generated_default",
        "thresholds_version": config.thresholds.version,
        "tile_content_type_expectation": config.tile_content_type_expectation,
        "frontend_breakpoints": list(config.frontend_breakpoints),
        "api_base_url": config.api_base_url,
        "object_prefix": config.object_prefix,
        "evidence_root": str(config.evidence_root),
        "evidence_dir": str(config.lane_dir),
        "execution_policy": {
            "default_fast_path": "deterministic_large_fixture",
            "real_national_data_required": False,
            "postgis_required": False,
            "live_api_required": False,
            "browser_required": False,
            "mvt_encoder_required": False,
        },
    }


def _dataset_manifest(config: ProductionScaleConfig) -> dict[str, Any]:
    bbox_sizes = _bbox_sizes(config.bbox_set)
    bounds = {
        "min_lon": 73.5,
        "min_lat": 18.1,
        "max_lon": 134.8,
        "max_lat": 53.6,
    }
    checksum_basis = {
        "dataset_source": config.dataset_source,
        "segment_count": config.segment_count,
        "model_count": config.model_count,
        "bounds": bounds,
        "bbox_sizes": bbox_sizes,
        "generation_mode": _generation_mode(config),
    }
    blockers = []
    if config.segment_count < config.min_segment_count:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_SEGMENT_COUNT_BELOW_THRESHOLD",
                "observed": config.segment_count,
                "threshold": config.min_segment_count,
            }
        )
    if config.model_count < config.min_model_count:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_MODEL_COUNT_BELOW_THRESHOLD",
                "observed": config.model_count,
                "threshold": config.min_model_count,
            }
        )
    if config.min_segment_count < DEFAULT_MIN_SEGMENT_COUNT:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_MIN_SEGMENT_COUNT_BELOW_PRODUCTION_FLOOR",
                "configured_minimum": config.min_segment_count,
                "production_floor": DEFAULT_MIN_SEGMENT_COUNT,
            }
        )
    if config.min_model_count < DEFAULT_MIN_MODEL_COUNT:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_MIN_MODEL_COUNT_BELOW_PRODUCTION_FLOOR",
                "configured_minimum": config.min_model_count,
                "production_floor": DEFAULT_MIN_MODEL_COUNT,
            }
        )
    if config.segment_count < DEFAULT_MIN_SEGMENT_COUNT:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_SEGMENT_COUNT_BELOW_PRODUCTION_FLOOR",
                "observed": config.segment_count,
                "production_floor": DEFAULT_MIN_SEGMENT_COUNT,
            }
        )
    if config.model_count < DEFAULT_MIN_MODEL_COUNT:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_MODEL_COUNT_BELOW_PRODUCTION_FLOOR",
                "observed": config.model_count,
                "production_floor": DEFAULT_MIN_MODEL_COUNT,
            }
        )
    return {
        "schema": "nhms.production_closure.scale.dataset_manifest.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "dataset_source": config.dataset_source,
        "generation_mode": _generation_mode(config),
        "segment_count": config.segment_count,
        "model_count": config.model_count,
        "minimum_counts": {
            "segment_count": config.min_segment_count,
            "model_count": config.min_model_count,
        },
        "geometry_bounds": bounds,
        "bbox_sizes": bbox_sizes,
        "checksum": _stable_sha256(checksum_basis),
        "crs": "EPSG:4490 source geometry; API bboxes supplied in EPSG:4326 and transformed for PostGIS.",
        "geometry_assumptions": {
            "geometry_type": "LineString/MultiLineString river centerline",
            "deterministic_fixture_spacing": "stable Hilbert-like sweep over national bounds",
            "null_geometry_allowed": False,
        },
        "blockers": blockers,
    }


def _thresholds_payload(config: ProductionScaleConfig) -> dict[str, Any]:
    thresholds = config.thresholds
    return {
        "schema": "nhms.production_closure.scale.thresholds.v1",
        "run_id": config.run_id,
        "version": thresholds.version,
        "source": str(config.thresholds_file) if config.thresholds_file else "generated_default",
        "minimum_counts": {
            "segment_count": thresholds.min_segment_count,
            "model_count": thresholds.min_model_count,
        },
        "p95_query_targets_ms": dict(thresholds.p95_query_ms),
        "max_tile_bytes": thresholds.max_tile_bytes,
        "frontend_budgets": {
            "load_ms": thresholds.frontend_load_ms,
            "render_ms": thresholds.frontend_render_ms,
            "timeline_ms": thresholds.frontend_timeline_ms,
            "chart_ms": thresholds.frontend_chart_ms,
            "memory_mb": thresholds.frontend_memory_mb,
        },
        "oversized_bbox_behavior": thresholds.oversized_bbox_behavior,
        "long_time_range_behavior": thresholds.long_time_range_behavior,
        "object_listing_limit": thresholds.object_listing_limit,
        "pass_fail_semantics": {
            "summary_ready_requires": [
                "dataset counts meet minimums",
                "query p95 samples are finite and within thresholds",
                "tile content expectation is satisfied or GeoJSON compatibility mode avoids MVT readiness claim",
                "frontend deterministic timings fit load/render/timeline/chart/memory budgets",
                "resource bounds are enforced for oversized bbox, long time ranges, and object listings",
            ],
            "malformed_or_non_finite_samples": "blocked",
            "unbounded_payloads": "blocked",
        },
    }


def _query_latency_evidence(
    config: ProductionScaleConfig,
    dataset_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    queries = []
    for query_name, fixture in QUERY_TARGETS.items():
        samples = _latency_samples(config, query_name, fixture["latency_samples_ms"])
        sample_blocker = _validate_samples(query_name, samples)
        if sample_blocker:
            blockers.append(sample_blocker)
        threshold_key = str(fixture["threshold_key"])
        threshold_ms = config.thresholds.p95_query_ms[threshold_key]
        p95_ms = _p95(samples) if sample_blocker is None else None
        threshold_passed = p95_ms is not None and p95_ms <= threshold_ms
        if not threshold_passed and sample_blocker is None:
            blockers.append(
                {
                    "error_code": "PRODUCTION_SCALE_QUERY_P95_THRESHOLD_EXCEEDED",
                    "query": query_name,
                    "p95_ms": p95_ms,
                    "threshold_ms": threshold_ms,
                }
            )
        row_count = _query_row_count(query_name, fixture, dataset_manifest)
        plan_lines = _query_plan_lines(query_name, fixture, row_count)
        plan_text = "\n".join(plan_lines)
        queries.append(
            {
                "query": query_name,
                "endpoint": fixture["endpoint"],
                "row_count": row_count,
                "bounded_payload": True,
                "plan_text": plan_text,
                "plan_hash": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
                "index_usage_recorded": "Index Scan" in plan_text or "Bitmap Index Scan" in plan_text,
                "latency_samples_ms": _evidence_samples(samples),
                "p95_ms": p95_ms,
                "threshold_key": threshold_key,
                "threshold_ms": threshold_ms,
                "threshold_passed": threshold_passed,
            }
        )
    return {
        "schema": "nhms.production_closure.scale.query_latency.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "execution_mode": "deterministic_query_plan_fixture",
        "live_db_executed": False,
        "live_api_executed": False,
        "dataset_checksum": dataset_manifest["checksum"],
        "geometry_bounds": dataset_manifest["geometry_bounds"],
        "queries": queries,
        "blockers": blockers,
    }


def _tile_evidence(config: ProductionScaleConfig, dataset_manifest: Mapping[str, Any]) -> dict[str, Any]:
    deterministic_contract = _deterministic_mvt_contract_record(config, dataset_manifest)
    current_content_type = deterministic_contract["observed_content_type"]
    compatible_expectations = {"application/geo+json", "application/json"}
    tile_bytes = int(deterministic_contract["payload_bytes"])
    max_tile_bytes = config.thresholds.max_tile_bytes
    endpoints = MVT_ENDPOINT_REFERENCES
    blockers = []
    production_mvt_readiness_claimed = False
    deterministic_mvt_passed = bool(deterministic_contract["passed"])
    if config.tile_content_type_expectation == "application/x-protobuf":
        if not deterministic_mvt_passed:
            blockers.append(
                {
                    "error_code": "PRODUCTION_SCALE_MVT_DETERMINISTIC_CONTRACT_BLOCKED",
                    "blocker_id": MVT_DETERMINISTIC_BLOCKER_ID,
                    "surface": "deterministic_mvt_contract",
                    "status": deterministic_contract["status"],
                    "expected_content_type": "application/x-protobuf",
                    "observed_content_type": current_content_type,
                    "affected_endpoints": endpoints,
                    "removal_criteria": (
                        "Provide a safe measured deterministic MVT contract artifact proving raw PBF bytes, SQL "
                        "shape, query-plan hash, payload budget, tile counts, feature counts, coordinate counts, "
                        "and browser timing for the canonical MVT endpoints."
                    ),
                    "residual_risk": (
                        "Passing deterministic artifacts proves repeatable contract and encoder evidence only; "
                        "live PostGIS national data volume and target-environment query plans remain separately "
                        "blocked until the live MVT blocker is removed."
                    ),
                    "message": (
                        "Deterministic MVT pass requires measured contract artifacts, "
                        "not content-type expectation alone."
                    ),
                    "artifact_links": deterministic_contract["artifact_paths"],
                }
            )
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_MVT_DELIVERY_BLOCKED",
                "blocker_id": MVT_LIVE_POSTGIS_BLOCKER_ID,
                "surface": "live_postgis_national_frontend_evidence",
                "status": "not_executed",
                "expected_content_type": "application/x-protobuf",
                "observed_content_type": current_content_type,
                "affected_endpoints": endpoints,
                "missing_implementation_work": MVT_MISSING_IMPLEMENTATION_WORK,
                "removal_criteria": (
                    "Run opt-in live PostGIS national tile validation plus browser proof in the target environment "
                    "and record passing artifacts for river-network, hydro, and flood-return-period MVT endpoints."
                ),
                "residual_risk": (
                    "Deterministic CI proves contract/cache/SQL shape only; it cannot prove target data volume "
                    "or PostGIS plan behavior."
                ),
                "artifact_links": [
                    "tile_evidence.json",
                    "query_latency_evidence.json",
                    "frontend_large_layer_evidence.json",
                ],
                "message": (
                    "Live production MVT readiness is not claimed; "
                    "deterministic contract status is reported separately."
                ),
            }
        )
    elif tile_bytes > max_tile_bytes:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_TILE_BYTES_EXCEEDED",
                "blocker_id": TILE_BYTES_BLOCKER_ID,
                "surface": "tile_payload_byte_budget",
                "status": "blocked",
                "affected_endpoints": endpoints,
                "observed_bytes": tile_bytes,
                "threshold_bytes": max_tile_bytes,
                "removal_criteria": (
                    "Reduce deterministic tile payload bytes below the configured max_tile_bytes threshold or "
                    "raise the threshold with measured production-scale evidence and release approval."
                ),
                "residual_risk": (
                    "Passing the generic byte budget only proves deterministic payload size; live national MVT "
                    "query plans and browser rendering remain separately evidenced."
                ),
                "artifact_links": deterministic_contract["artifact_paths"],
            }
        )
    return {
        "schema": "nhms.production_closure.scale.tile.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "execution_mode": "deterministic_tile_contract_evidence",
        "live_postgis_execution_mode": "not_executed",
        "live_postgis_status": "not_executed",
        "tile_content_type_expectation": config.tile_content_type_expectation,
        "observed_content_type": current_content_type,
        "content_type_satisfied": (
            config.tile_content_type_expectation in compatible_expectations
            or config.tile_content_type_expectation == current_content_type
        )
        and not blockers,
        "production_mvt_readiness_claimed": production_mvt_readiness_claimed,
        "deterministic_mvt_passed": deterministic_mvt_passed,
        "geojson_compatibility_mode": config.tile_content_type_expectation in compatible_expectations,
        "geojson_compatibility_note": (
            "GeoJSON compatibility evidence is ready, but this mode does not claim production MVT readiness."
        ),
        "max_bytes_comparison": {
            "observed_bytes": tile_bytes,
            "threshold_bytes": max_tile_bytes,
            "passed": tile_bytes <= max_tile_bytes,
        },
        "mvt_deterministic_contract": deterministic_contract,
        "mvt_deterministic_metrics": deterministic_contract["metrics"],
        "endpoint_references": endpoints,
        "layer_metadata": {
            "layer_id": "flood-return-period",
            "tile_format": "mvt" if deterministic_mvt_passed else "geojson_compatibility",
            "url_template": "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
            "maplibre_source_layer": "flood_return_period",
            "property_schema_version": "m16-hydrology-mvt-v1",
            "min_zoom": 0,
            "max_zoom": 14,
            "source": config.dataset_source,
            "segment_count": dataset_manifest["segment_count"],
            "fields": ["segment_id", "value", "unit", "quality_flag", "return_period", "warning_level"],
            "legacy_pbf_route_behavior": (
                "canonical .pbf route is live-PostGIS-only; bounded GeoJSON remains query compatibility"
            ),
            "mvt_encoder_executed": deterministic_mvt_passed,
        },
        "blockers": blockers,
    }


def _deterministic_mvt_contract_record(
    config: ProductionScaleConfig,
    dataset_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if config.tile_content_type_expectation != "application/x-protobuf":
        payload_bytes = 1_280_000
        return {
            "status": "not_executed",
            "passed": False,
            "observed_content_type": "application/json",
            "payload_bytes": payload_bytes,
            "artifact_source": "geojson_compatibility_mode",
            "artifact_paths": [],
            "metrics": {
                "status": "not_executed",
                "payload_bytes": payload_bytes,
                "thresholds": _mvt_contract_thresholds(config),
                "threshold_comparison": _mvt_contract_comparisons(
                    {
                        "payload_bytes": payload_bytes,
                        "p95_ms": 0.0,
                        "browser_timing_ms": 0.0,
                        "tile_count": 0,
                        "feature_count": 0,
                        "coordinate_count": 0,
                    },
                    config,
                ),
            },
        }

    if config.mvt_contract_artifact is None:
        return _blocked_mvt_contract_record(
            config,
            status="not_executed",
            artifact_source="missing_mvt_contract_artifact",
            message="No measured deterministic MVT artifact path was supplied.",
        )

    artifact_path = config.mvt_contract_artifact
    _refuse_symlink_components(artifact_path)
    try:
        content = read_bytes_limited_no_follow(artifact_path, max_bytes=MAX_EVIDENCE_PAYLOAD_BYTES)
        if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
            return _blocked_mvt_contract_record(
                config,
                status="blocked",
                artifact_source="oversized_mvt_contract_artifact",
                message=(
                    "Measured deterministic MVT artifact exceeds configured limit "
                    f"of {MAX_EVIDENCE_PAYLOAD_BYTES} bytes."
                ),
                artifact_paths=[str(artifact_path)],
            )
        measured = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SafeFilesystemError) as error:
        return _blocked_mvt_contract_record(
            config,
            status="blocked",
            artifact_source="invalid_mvt_contract_artifact",
            message=f"Measured deterministic MVT artifact could not be read safely: {error}",
            artifact_paths=[str(artifact_path)],
        )
    if not isinstance(measured, Mapping):
        return _blocked_mvt_contract_record(
            config,
            status="blocked",
            artifact_source="invalid_mvt_contract_artifact",
            message="Measured deterministic MVT artifact must contain a JSON object.",
            artifact_paths=[str(artifact_path)],
        )
    artifact_sha256 = hashlib.sha256(content).hexdigest()
    normalized = _validated_mvt_contract_metrics(
        measured,
        config=config,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )
    if not normalized["ok"]:
        return _blocked_mvt_contract_record(
            config,
            status="blocked",
            artifact_source="invalid_mvt_contract_artifact",
            message=str(normalized["message"]),
            artifact_paths=[str(artifact_path)],
            artifact_sha256=artifact_sha256,
            extra_metrics=normalized.get("metrics") if isinstance(normalized.get("metrics"), Mapping) else None,
        )
    metrics = normalized["metrics"]
    return {
        "status": "passed",
        "passed": True,
        "observed_content_type": "application/x-protobuf",
        "payload_bytes": metrics["payload_bytes"],
        "artifact_source": "measured_mvt_contract_artifact",
        "artifact_paths": [str(artifact_path)],
        "metrics": metrics,
    }


def _validated_mvt_contract_metrics(
    measured: Mapping[str, Any],
    *,
    config: ProductionScaleConfig,
    artifact_path: Path,
    artifact_sha256: str,
) -> dict[str, Any]:
    required_fields = {
        "observed_content_type",
        "raw_tile_bytes_observed",
        *MVT_CONTRACT_STRING_FIELDS,
        *MVT_CONTRACT_NUMERIC_FIELDS,
    }
    missing = sorted(field for field in required_fields if field not in measured)
    if missing:
        return {
            "ok": False,
            "message": f"Measured deterministic MVT artifact is missing required fields: {', '.join(missing)}.",
        }
    if measured["observed_content_type"] != "application/x-protobuf":
        return {"ok": False, "message": "Measured deterministic MVT artifact did not observe application/x-protobuf."}
    if measured["raw_tile_bytes_observed"] is not True:
        return {
            "ok": False,
            "message": "Measured deterministic MVT artifact must confirm raw tile bytes were observed.",
        }
    parsed_strings: dict[str, str] = {}
    for field_name in MVT_CONTRACT_STRING_FIELDS:
        if not isinstance(measured[field_name], str) or not measured[field_name]:
            return {
                "ok": False,
                "message": f"Measured deterministic MVT artifact field {field_name} must be a non-empty string.",
            }
        parsed_strings[field_name] = measured[field_name]

    parsed_numbers: dict[str, int | float] = {}
    for field_name in MVT_CONTRACT_NUMERIC_FIELDS:
        parsed = _mvt_contract_finite_number(measured[field_name], field_name)
        if parsed is None:
            return {
                "ok": False,
                "message": f"Measured deterministic MVT artifact field {field_name} must be finite numeric evidence.",
            }
        parsed_numbers[field_name] = parsed
    comparisons = _mvt_contract_comparisons(parsed_numbers, config)
    failed = [
        f"{field_name} {comparison['operator']} {comparison['threshold']}"
        for field_name, comparison in comparisons.items()
        if not comparison["passed"]
    ]
    if failed:
        failed_metrics = {
            **parsed_strings,
            **parsed_numbers,
            "observed_content_type": "application/x-protobuf",
            "raw_tile_bytes_observed": True,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_sha256,
            "artifact_paths": [str(artifact_path)],
            "thresholds": _mvt_contract_thresholds(config),
            "threshold_comparison": comparisons,
        }
        return {
            "ok": False,
            "message": (
                "Measured deterministic MVT artifact failed threshold/minimum checks: "
                f"{', '.join(failed)}."
            ),
            "metrics": failed_metrics,
        }

    normalized = {
        **parsed_strings,
        **parsed_numbers,
        "observed_content_type": "application/x-protobuf",
        "raw_tile_bytes_observed": True,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "artifact_paths": [str(artifact_path)],
        "thresholds": _mvt_contract_thresholds(config),
        "threshold_comparison": comparisons,
    }
    return {"ok": True, "metrics": normalized}


def _mvt_contract_thresholds(config: ProductionScaleConfig) -> dict[str, Any]:
    return {
        "payload_bytes": config.thresholds.max_tile_bytes,
        "p95_ms": config.thresholds.p95_query_ms[MVT_CONTRACT_P95_THRESHOLD_KEY],
        "p95_threshold_key": MVT_CONTRACT_P95_THRESHOLD_KEY,
        "browser_timing_ms": config.thresholds.frontend_render_ms,
        "browser_timing_threshold_name": MVT_CONTRACT_BROWSER_TIMING_THRESHOLD_NAME,
        "tile_count_min": MVT_CONTRACT_MIN_TILE_COUNT,
        "feature_count_min": MVT_CONTRACT_MIN_FEATURE_COUNT,
        "coordinate_count_min": MVT_CONTRACT_MIN_COORDINATE_COUNT,
    }


def _mvt_contract_comparisons(
    parsed_numbers: Mapping[str, int | float],
    config: ProductionScaleConfig,
) -> dict[str, dict[str, Any]]:
    return {
        "payload_bytes": _threshold_comparison(
            float(parsed_numbers["payload_bytes"]),
            float(config.thresholds.max_tile_bytes),
        ),
        "p95_ms": {
            **_threshold_comparison(
                float(parsed_numbers["p95_ms"]),
                float(config.thresholds.p95_query_ms[MVT_CONTRACT_P95_THRESHOLD_KEY]),
            ),
            "threshold_key": MVT_CONTRACT_P95_THRESHOLD_KEY,
        },
        "browser_timing_ms": {
            **_threshold_comparison(
                float(parsed_numbers["browser_timing_ms"]),
                float(config.thresholds.frontend_render_ms),
            ),
            "threshold_name": MVT_CONTRACT_BROWSER_TIMING_THRESHOLD_NAME,
        },
        "tile_count": _minimum_comparison(parsed_numbers["tile_count"], MVT_CONTRACT_MIN_TILE_COUNT),
        "feature_count": _minimum_comparison(parsed_numbers["feature_count"], MVT_CONTRACT_MIN_FEATURE_COUNT),
        "coordinate_count": _minimum_comparison(
            parsed_numbers["coordinate_count"],
            MVT_CONTRACT_MIN_COORDINATE_COUNT,
        ),
    }


def _minimum_comparison(observed: int | float, minimum: int) -> dict[str, Any]:
    return {
        "observed": observed,
        "threshold": minimum,
        "operator": ">=",
        "passed": math.isfinite(float(observed)) and observed >= minimum,
    }


def _mvt_contract_finite_number(value: Any, field_name: str) -> int | float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        return None
    if field_name in {"payload_bytes", "tile_count", "feature_count", "coordinate_count"}:
        if not parsed.is_integer():
            return None
        return int(parsed)
    return parsed


def _blocked_mvt_contract_record(
    config: ProductionScaleConfig,
    *,
    status: str,
    artifact_source: str,
    message: str,
    artifact_paths: list[str] | None = None,
    artifact_sha256: str | None = None,
    extra_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "status": status,
        "payload_bytes": 0,
        "thresholds": _mvt_contract_thresholds(config),
        "threshold_comparison": _mvt_contract_comparisons(
            {
                "payload_bytes": 0,
                "p95_ms": 0.0,
                "browser_timing_ms": 0.0,
                "tile_count": 0,
                "feature_count": 0,
                "coordinate_count": 0,
            },
            config,
        ),
        "message": message,
    }
    if extra_metrics:
        metrics.update(dict(extra_metrics))
        metrics["status"] = status
        metrics["message"] = message
    if artifact_paths:
        metrics["artifact_paths"] = artifact_paths
        metrics["artifact_path"] = artifact_paths[0]
    if artifact_sha256:
        metrics["artifact_sha256"] = artifact_sha256
    return {
        "status": status,
        "passed": False,
        "observed_content_type": "not_measured",
        "payload_bytes": 0,
        "artifact_source": artifact_source,
        "artifact_paths": artifact_paths or [],
        "metrics": metrics,
    }


def _frontend_large_layer_evidence(
    config: ProductionScaleConfig,
    dataset_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = []
    breakpoints = []
    for breakpoint in config.frontend_breakpoints:
        width, height = _parse_breakpoint(breakpoint)
        is_mobile = width <= 640
        timings = {
            "load_ms": 1320.0 if not is_mobile else 1780.0,
            "render_ms": 790.0 if not is_mobile else 1040.0,
            "timeline_ms": 115.0 if not is_mobile else 145.0,
            "chart_ms": 230.0 if not is_mobile else 280.0,
        }
        memory_mb = 212.0 if not is_mobile else 176.0
        comparisons = {
            "load_ms": _threshold_comparison(timings["load_ms"], config.thresholds.frontend_load_ms),
            "render_ms": _threshold_comparison(timings["render_ms"], config.thresholds.frontend_render_ms),
            "timeline_ms": _threshold_comparison(timings["timeline_ms"], config.thresholds.frontend_timeline_ms),
            "chart_ms": _threshold_comparison(timings["chart_ms"], config.thresholds.frontend_chart_ms),
            "memory_mb": _threshold_comparison(memory_mb, config.thresholds.frontend_memory_mb),
        }
        for metric, comparison in comparisons.items():
            if not comparison["passed"]:
                blockers.append(
                    {
                        "error_code": "PRODUCTION_SCALE_FRONTEND_BUDGET_EXCEEDED",
                        "breakpoint": breakpoint,
                        "metric": metric,
                        "observed": comparison["observed"],
                        "threshold": comparison["threshold"],
                    }
                )
        breakpoints.append(
            {
                "breakpoint": breakpoint,
                "width": width,
                "height": height,
                "mode": "mobile" if is_mobile else "desktop",
                "timings_ms": timings,
                "memory_mb": memory_mb,
                "threshold_comparison": comparisons,
                "large_layer_render": {"segment_count": dataset_manifest["segment_count"], "status": "within_budget"},
                "segment_selection": {"timing_ms": 48.0 if not is_mobile else 61.0, "status": "ready"},
                "timeline_movement": {"timing_ms": timings["timeline_ms"], "status": "ready"},
                "chart_render": {"timing_ms": timings["chart_ms"], "status": "ready"},
            }
        )
    return {
        "schema": "nhms.production_closure.scale.frontend_large_layer.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "execution_mode": "deterministic_frontend_performance_fixture",
        "live_frontend_executed": False,
        "mock_only_live_readiness_claimed": False,
        "frontend_api_base": config.api_base_url,
        "lineage": {
            "dataset_source": config.dataset_source,
            "segment_count": dataset_manifest["segment_count"],
            "model_count": dataset_manifest["model_count"],
            "dataset_checksum": dataset_manifest["checksum"],
            "run_id": config.run_id,
            "thresholds_version": config.thresholds.version,
            "tile_expectation": config.tile_content_type_expectation,
        },
        "breakpoints": breakpoints,
        "recoverable_states": {
            "oversized_layer": "bounded warning with retry after narrower bbox",
            "unavailable_layer": "non-fatal empty-state with retained timeline/chart controls",
            "oversized_or_unavailable_breaks_page": False,
        },
        "blockers": blockers,
    }


def _resource_bounds_evidence(config: ProductionScaleConfig) -> dict[str, Any]:
    blockers = []
    if config.thresholds.object_listing_limit > MAX_OBJECT_LISTING_COUNT:
        blockers.append(
            {
                "error_code": "PRODUCTION_SCALE_OBJECT_LISTING_LIMIT_EXCEEDED",
                "observed": config.thresholds.object_listing_limit,
                "maximum": MAX_OBJECT_LISTING_COUNT,
            }
        )
    return {
        "schema": "nhms.production_closure.scale.resource_bounds.v1",
        "run_id": config.run_id,
        "status": "blocked" if blockers else "ready",
        "oversized_bbox": {
            "input": [-180.0, -90.0, 180.0, 90.0],
            "behavior": config.thresholds.oversized_bbox_behavior,
            "status": "bounded",
            "error_code": "PRODUCTION_SCALE_BBOX_TOO_LARGE",
        },
        "long_time_range": {
            "input_hours": 24 * 30,
            "behavior": config.thresholds.long_time_range_behavior,
            "status": "bounded",
            "error_code": "PRODUCTION_SCALE_TIME_RANGE_TOO_LONG",
        },
        "object_listing": {
            "observed_entries": config.thresholds.object_listing_limit,
            "limit": config.thresholds.object_listing_limit,
            "status": "bounded",
            "unbounded_payloads_rejected": True,
        },
        "evidence_payload_limit_bytes": MAX_EVIDENCE_PAYLOAD_BYTES,
        "blockers": blockers,
    }


def _summary(
    config: ProductionScaleConfig,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    dataset_manifest: Mapping[str, Any],
    query_evidence: Mapping[str, Any],
    tile_evidence: Mapping[str, Any],
    frontend_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    live_db_executed = query_evidence["live_db_executed"]
    live_api_executed = query_evidence["live_api_executed"]
    live_frontend_executed = frontend_evidence["live_frontend_executed"]
    deterministic_fixture = not (live_db_executed and live_api_executed and live_frontend_executed)
    return {
        "schema": "nhms.production_closure.scale.v1",
        "issue": 151,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "execution_mode": "live_scale_validation" if not deterministic_fixture else "deterministic_fixture",
        "deterministic_fixture": deterministic_fixture,
        "final_production_readiness_claimed": False,
        "dataset_source": config.dataset_source,
        "segment_count": dataset_manifest["segment_count"],
        "model_count": dataset_manifest["model_count"],
        "thresholds_version": config.thresholds.version,
        "tile_content_type_expectation": config.tile_content_type_expectation,
        "tile_status": tile_evidence["status"],
        "production_mvt_readiness_claimed": tile_evidence["production_mvt_readiness_claimed"],
        "query_status": query_evidence["status"],
        "frontend_status": frontend_evidence["status"],
        "live_db_executed": live_db_executed,
        "live_api_executed": live_api_executed,
        "live_frontend_executed": live_frontend_executed,
        "blockers": blockers,
        "ready_semantics": (
            "ready means deterministic thresholds pass and tile expectation is satisfied; GeoJSON compatibility "
            "mode remains non-MVT and does not claim production MVT readiness."
        ),
        "files": [
            "preflight.json",
            "dataset_manifest.json",
            "thresholds.json",
            "query_latency_evidence.json",
            "tile_evidence.json",
            "frontend_large_layer_evidence.json",
            "resource_bounds_evidence.json",
            "environment.json",
            "summary.json",
        ],
    }


def _summary_blockers(
    dataset_manifest: Mapping[str, Any],
    query_evidence: Mapping[str, Any],
    tile_evidence: Mapping[str, Any],
    frontend_evidence: Mapping[str, Any],
    resource_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers = []
    for evidence in (dataset_manifest, query_evidence, tile_evidence, frontend_evidence, resource_evidence):
        blockers.extend(evidence.get("blockers", []))
    return blockers


def _environment_payload(config: ProductionScaleConfig) -> dict[str, Any]:
    env_keys = [
        "NHMS_RUN_PRODUCTION_CLOSURE",
        "NHMS_PRODUCTION_SCALE_DATASET_SOURCE",
        "NHMS_PRODUCTION_SCALE_SEGMENT_COUNT",
        "NHMS_PRODUCTION_SCALE_MODEL_COUNT",
        "NHMS_PRODUCTION_SCALE_MIN_SEGMENT_COUNT",
        "NHMS_PRODUCTION_SCALE_MIN_MODEL_COUNT",
        "NHMS_PRODUCTION_SCALE_BBOX_SET",
        "NHMS_PRODUCTION_SCALE_THRESHOLDS_FILE",
        "NHMS_PRODUCTION_SCALE_TILE_CONTENT_TYPE_EXPECTATION",
        "NHMS_PRODUCTION_SCALE_FRONTEND_BREAKPOINTS",
        "NHMS_PRODUCTION_SCALE_API_BASE_URL",
        "NHMS_PRODUCTION_SCALE_OBJECT_PREFIX",
        "NHMS_PRODUCTION_SCALE_LATENCY_FIXTURE",
        "NHMS_PRODUCTION_SCALE_MVT_CONTRACT_ARTIFACT",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
    ]
    return {
        "schema": "nhms.production_closure.scale.environment.v1",
        "run_id": config.run_id,
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "env": {key: os.getenv(key, "") for key in env_keys if key in os.environ},
        "redaction": {
            "secret_shaped_values_redacted": True,
            "stdout_redacted": True,
            "evidence_redacted": True,
        },
    }


def _load_thresholds(
    thresholds_file: Path | None,
    *,
    min_segment_count: int | None,
    min_model_count: int | None,
) -> ProductionScaleThresholds:
    thresholds = ProductionScaleThresholds.default()
    env_min_segments = (
        _positive_int_config(min_segment_count, "min_segment_count")
        if min_segment_count is not None
        else _optional_positive_int_env("NHMS_PRODUCTION_SCALE_MIN_SEGMENT_COUNT")
    )
    env_min_models = (
        _positive_int_config(min_model_count, "min_model_count")
        if min_model_count is not None
        else _optional_positive_int_env("NHMS_PRODUCTION_SCALE_MIN_MODEL_COUNT")
    )
    if thresholds_file is not None:
        _refuse_symlink_components(thresholds_file)
        try:
            content = read_bytes_limited_no_follow(thresholds_file, max_bytes=MAX_EVIDENCE_PAYLOAD_BYTES)
            if len(content) > MAX_EVIDENCE_PAYLOAD_BYTES:
                raise ProductionScaleValidationError(
                    "PRODUCTION_SCALE_THRESHOLDS_INVALID",
                    f"Thresholds file exceeds configured limit of {MAX_EVIDENCE_PAYLOAD_BYTES} bytes.",
                )
            raw = json.loads(content.decode("utf-8"))
        except ProductionScaleValidationError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_THRESHOLDS_INVALID",
                f"Thresholds file could not be read as JSON: {error}",
            ) from error
        except SafeFilesystemError as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_THRESHOLDS_INVALID",
                f"Thresholds file could not be read safely: {error}",
            ) from error
        if not isinstance(raw, Mapping):
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_THRESHOLDS_INVALID",
                "Thresholds file must contain a JSON object.",
            )
        thresholds = _thresholds_from_mapping(raw, thresholds)
    if env_min_segments is not None or env_min_models is not None:
        thresholds = replace(
            thresholds,
            min_segment_count=env_min_segments if env_min_segments is not None else thresholds.min_segment_count,
            min_model_count=env_min_models if env_min_models is not None else thresholds.min_model_count,
        )
    return thresholds


def _thresholds_from_mapping(
    raw: Mapping[str, Any],
    default: ProductionScaleThresholds,
) -> ProductionScaleThresholds:
    minimum_counts = _threshold_object(raw, "minimum_counts")
    frontend_budgets = _threshold_object(raw, "frontend_budgets")
    p95 = raw.get("p95_query_targets_ms", default.p95_query_ms)
    if not isinstance(p95, Mapping):
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            "p95_query_targets_ms must be an object.",
        )
    merged_p95 = dict(default.p95_query_ms)
    for key, value in p95.items():
        merged_p95[str(key)] = _finite_positive_number(value, f"p95_query_targets_ms.{key}")
    return ProductionScaleThresholds(
        version=str(raw.get("version", default.version)),
        min_segment_count=_positive_int_threshold(
            minimum_counts.get("segment_count", default.min_segment_count),
            "minimum_counts.segment_count",
        ),
        min_model_count=_positive_int_threshold(
            minimum_counts.get("model_count", default.min_model_count),
            "minimum_counts.model_count",
        ),
        p95_query_ms=merged_p95,
        max_tile_bytes=_positive_int_threshold(raw.get("max_tile_bytes", default.max_tile_bytes), "max_tile_bytes"),
        frontend_load_ms=_positive_int_threshold(
            frontend_budgets.get("load_ms", default.frontend_load_ms),
            "frontend_budgets.load_ms",
        ),
        frontend_render_ms=_positive_int_threshold(
            frontend_budgets.get("render_ms", default.frontend_render_ms),
            "frontend_budgets.render_ms",
        ),
        frontend_timeline_ms=_positive_int_threshold(
            frontend_budgets.get("timeline_ms", default.frontend_timeline_ms),
            "frontend_budgets.timeline_ms",
        ),
        frontend_chart_ms=_positive_int_threshold(
            frontend_budgets.get("chart_ms", default.frontend_chart_ms),
            "frontend_budgets.chart_ms",
        ),
        frontend_memory_mb=_positive_int_threshold(
            frontend_budgets.get("memory_mb", default.frontend_memory_mb),
            "frontend_budgets.memory_mb",
        ),
        oversized_bbox_behavior=str(raw.get("oversized_bbox_behavior", default.oversized_bbox_behavior)),
        long_time_range_behavior=str(raw.get("long_time_range_behavior", default.long_time_range_behavior)),
        object_listing_limit=_positive_int_threshold(
            raw.get("object_listing_limit", default.object_listing_limit),
            "object_listing_limit",
        ),
    )


def _validate_config(config: ProductionScaleConfig) -> None:
    _validate_identifier(config.dataset_source, "dataset_source")
    for value, field_name in (
        (config.segment_count, "segment_count"),
        (config.model_count, "model_count"),
        (config.min_segment_count, "min_segment_count"),
        (config.min_model_count, "min_model_count"),
        (config.thresholds.max_tile_bytes, "max_tile_bytes"),
        (config.thresholds.frontend_load_ms, "frontend_load_ms"),
        (config.thresholds.frontend_render_ms, "frontend_render_ms"),
        (config.thresholds.frontend_timeline_ms, "frontend_timeline_ms"),
        (config.thresholds.frontend_chart_ms, "frontend_chart_ms"),
        (config.thresholds.frontend_memory_mb, "frontend_memory_mb"),
        (config.thresholds.object_listing_limit, "object_listing_limit"),
    ):
        if value <= 0:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_CONFIG_INVALID",
                f"{field_name} must be positive.",
            )
    if config.tile_content_type_expectation not in VALID_TILE_CONTENT_TYPE_EXPECTATIONS:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_TILE_EXPECTATION_INVALID",
            "Tile content-type expectation must be application/geo+json, application/json, or application/x-protobuf.",
        )
    for bbox in config.bbox_set:
        _validate_identifier(bbox, "bbox")
    for breakpoint in config.frontend_breakpoints:
        _parse_breakpoint(breakpoint)
    required_threshold_keys = {str(item["threshold_key"]) for item in QUERY_TARGETS.values()}
    missing = sorted(required_threshold_keys - set(config.thresholds.p95_query_ms))
    if missing:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"Thresholds file is missing required p95 query targets: {', '.join(missing)}.",
        )
    if config.latency_fixture not in {"valid", "non_finite"}:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_LATENCY_FIXTURE_INVALID",
            "Latency fixture must be valid or non_finite.",
        )


def _validate_identifier(value: str, field_name: str) -> None:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_IDENTIFIER_UNSAFE",
            f"{field_name} must be a safe production-scale identifier.",
        )


def _validate_api_base_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_API_BASE_URL_UNSAFE",
            "Scale API base URL must not contain credential material.",
        ) from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_API_BASE_URL_UNSAFE",
            "Scale API base URL must not contain userinfo credentials, query parameters, or fragments.",
        )
    _guard_canonical_path_segments(
        parsed.path,
        error_code="PRODUCTION_SCALE_API_BASE_URL_UNSAFE",
        message="Scale API base URL path must not contain credential assignments, traversal, or encoded separators.",
    )
    if any(
        SENSITIVE_PREFIX_ASSIGNMENT_RE.search(part)
        for part in _canonical_decode_steps(
            value,
            error_code="PRODUCTION_SCALE_API_BASE_URL_UNSAFE",
            message="Scale API base URL must not contain credential assignments or over-encoded percent escapes.",
        )
    ):
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_API_BASE_URL_UNSAFE",
            "Scale API base URL must not contain credential assignments.",
        )


def _validate_object_prefix_safe(prefix: str) -> None:
    if not prefix:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_OBJECT_PREFIX_INVALID",
            "Scale object prefix must not be empty.",
        )
    try:
        parsed = urlsplit(prefix)
    except ValueError as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_OBJECT_PREFIX_UNSAFE",
            "Scale object prefix must not contain credential material.",
        ) from error
    if not parsed.scheme or not parsed.netloc:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_OBJECT_PREFIX_INVALID",
            "Scale object prefix must be an object URI prefix such as s3://bucket/prefix.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_OBJECT_PREFIX_UNSAFE",
            "Scale object prefix must not contain userinfo credentials, query parameters, or fragments.",
        )
    _guard_canonical_path_segments(
        parsed.path,
        error_code="PRODUCTION_SCALE_OBJECT_PREFIX_UNSAFE",
        message=(
            "Scale object prefix path segments must not contain credential assignments, traversal, "
            "or encoded separators."
        ),
    )


def _canonical_decode_steps(value: str, *, error_code: str, message: str) -> tuple[str, ...]:
    steps = [value]
    current = value
    for _ in range(MAX_PERCENT_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        steps.append(decoded)
        current = decoded
    if unquote(current) != current:
        raise ProductionScaleValidationError(error_code, message)
    return tuple(steps)


def _guard_canonical_path_segments(path: str, *, error_code: str, message: str) -> None:
    for raw_segment in path.split("/"):
        if raw_segment == "":
            continue
        for segment in _canonical_decode_steps(raw_segment, error_code=error_code, message=message):
            if (
                "/" in segment
                or "\\" in segment
                or "?" in segment
                or "#" in segment
                or ";" in segment
                or "&" in segment
                or segment in {".", ".."}
                or SENSITIVE_PREFIX_ASSIGNMENT_RE.search(segment)
            ):
                raise ProductionScaleValidationError(error_code, message)


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionScaleValidationError(
        "PRODUCTION_SCALE_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    _refuse_symlink_components_to_deepest_existing(root)
    return root.resolve(strict=False)


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _refuse_symlink_components_to_deepest_existing(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )
        if not current.exists():
            break


def _bbox_sizes(names: Sequence[str]) -> dict[str, Any]:
    catalog = {
        "national": {"bbox": [73.5, 18.1, 134.8, 53.6], "approx_area_degrees2": 2176.15},
        "yangtze": {"bbox": [90.0, 24.0, 122.0, 35.0], "approx_area_degrees2": 352.0},
        "urban": {"bbox": [115.7, 39.4, 117.4, 41.1], "approx_area_degrees2": 2.89},
    }
    fallback = {"bbox": [73.5, 18.1, 134.8, 53.6], "approx_area_degrees2": 2176.15}
    return {name: catalog.get(name, fallback) for name in names}


def _generation_mode(config: ProductionScaleConfig) -> str:
    if config.dataset_source == DEFAULT_DATASET_SOURCE:
        return "generated_deterministic_fixture"
    return "consumed_imported_dataset_metadata"


def _latency_samples(config: ProductionScaleConfig, query_name: str, defaults: Sequence[float]) -> tuple[float, ...]:
    if config.latency_fixture == "non_finite" and query_name == "river_bbox":
        return (120.0, float("nan"), 122.0)
    env_key = f"NHMS_PRODUCTION_SCALE_{query_name.upper()}_LATENCY_MS"
    raw = os.getenv(env_key)
    if not raw:
        return tuple(defaults)
    samples = []
    for part in raw.split(","):
        try:
            samples.append(float(part.strip()))
        except ValueError as error:
            raise ProductionScaleValidationError(
                "PRODUCTION_SCALE_LATENCY_SAMPLE_INVALID",
                f"{env_key} contains a non-numeric latency sample.",
            ) from error
    return tuple(samples)


def _query_row_count(
    query_name: str,
    fixture: Mapping[str, Any],
    dataset_manifest: Mapping[str, Any],
) -> int:
    if query_name == "model_listing":
        return int(dataset_manifest["model_count"])
    if query_name in {"river_bbox", "flood_alert_map"}:
        return min(int(fixture["row_count"]), int(dataset_manifest["segment_count"]))
    return int(fixture["row_count"])


def _query_plan_lines(query_name: str, fixture: Mapping[str, Any], row_count: int) -> tuple[str, ...]:
    if query_name == "model_listing":
        return (
            f"Limit  (cost=0.42..18.00 rows={row_count} width=96)",
            "  -> Index Scan using model_instance_pkey on core.model_instance",
        )
    return tuple(str(line) for line in fixture["plan_lines"])


def _validate_samples(query_name: str, samples: Sequence[float]) -> dict[str, Any] | None:
    if not samples or len(samples) > MAX_SAMPLE_COUNT:
        return {
            "error_code": "PRODUCTION_SCALE_LATENCY_SAMPLE_INVALID",
            "query": query_name,
            "message": f"Latency sample count must be between 1 and {MAX_SAMPLE_COUNT}.",
        }
    for sample in samples:
        if not math.isfinite(sample) or sample < 0:
            return {
                "error_code": "PRODUCTION_SCALE_LATENCY_SAMPLE_INVALID",
                "query": query_name,
                "message": "Latency samples must be finite non-negative numbers.",
            }
    return None


def _evidence_samples(samples: Sequence[float]) -> list[float | str]:
    evidence_values: list[float | str] = []
    for sample in samples:
        evidence_values.append(sample if math.isfinite(sample) else "non_finite")
    return evidence_values


def _p95(samples: Sequence[float]) -> float:
    if len(samples) == 1:
        return round(float(samples[0]), 3)
    return round(float(quantiles(samples, n=100, method="inclusive")[94]), 3)


def _threshold_comparison(observed: float, threshold: float) -> dict[str, Any]:
    return {
        "observed": observed,
        "threshold": threshold,
        "operator": "<=",
        "passed": math.isfinite(observed) and observed <= threshold,
    }


def _parse_breakpoint(value: str) -> tuple[int, int]:
    label = value.split(":", 1)[-1]
    if "x" not in label:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_FRONTEND_BREAKPOINT_INVALID",
            "Frontend breakpoints must use label:WIDTHxHEIGHT or WIDTHxHEIGHT.",
        )
    width_raw, height_raw = label.lower().split("x", 1)
    try:
        width = int(width_raw)
        height = int(height_raw)
    except ValueError as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_FRONTEND_BREAKPOINT_INVALID",
            "Frontend breakpoint width and height must be integers.",
        ) from error
    if width <= 0 or height <= 0 or width > 10000 or height > 10000:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_FRONTEND_BREAKPOINT_INVALID",
            "Frontend breakpoint dimensions must be positive and bounded.",
        )
    return width, height


def _parse_csv_tuple(value: str | None, default: Sequence[str]) -> tuple[str, ...]:
    if not value:
        return tuple(default)
    parsed = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parsed:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            "Comma-separated scale configuration must contain at least one value.",
        )
    return tuple(dict.fromkeys(parsed))


def _stable_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int_env(env_name: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{env_name} must be an integer.",
        ) from error
    if value <= 0:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{env_name} must be positive.",
        )
    return value


def _optional_positive_int_env(env_name: str) -> int | None:
    raw = os.getenv(env_name)
    if raw is None:
        return None
    return _positive_int_env(env_name, 1)


def _positive_int_config(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{field_name} must be an integer.",
        )
    if isinstance(value, float) and not value.is_integer():
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{field_name} must be an integer.",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{field_name} must be an integer.",
        ) from error
    if parsed <= 0:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_CONFIG_INVALID",
            f"{field_name} must be positive.",
        )
    return parsed


def _threshold_object(raw: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = raw.get(field_name, {})
    if isinstance(value, Mapping):
        return value
    raise ProductionScaleValidationError(
        "PRODUCTION_SCALE_THRESHOLDS_INVALID",
        f"{field_name} must be an object.",
    )


def _positive_int_threshold(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be an integer.",
        )
    if isinstance(value, float) and not value.is_integer():
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be an integer.",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be an integer.",
        ) from error
    if parsed <= 0:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be positive.",
        )
    return parsed


def _optional_path_env(env_name: str) -> Path | None:
    raw = os.getenv(env_name)
    if not raw:
        return None
    return Path(raw).expanduser()


def _finite_positive_number(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be numeric.",
        ) from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ProductionScaleValidationError(
            "PRODUCTION_SCALE_THRESHOLDS_INVALID",
            f"{field_name} must be finite and positive.",
        )
    return parsed


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.command("validate-scale")
    @click.option("--evidence-root", type=click.Path(path_type=Path), required=True)
    @click.option("--run-id")
    @click.option("--dataset-source", default=None)
    @click.option("--segment-count", type=int, default=None)
    @click.option("--model-count", type=int, default=None)
    @click.option("--min-segment-count", type=int, default=None)
    @click.option("--min-model-count", type=int, default=None)
    @click.option("--bbox-set", default=None)
    @click.option("--thresholds-file", type=click.Path(path_type=Path), default=None)
    @click.option("--tile-content-type-expectation", default=None)
    @click.option("--frontend-breakpoints", default=None)
    @click.option("--api-base-url", default=None)
    @click.option("--object-prefix", default=None)
    @click.option("--latency-fixture", default=None)
    @click.option("--mvt-contract-artifact", type=click.Path(path_type=Path), default=None)
    @click.option("--force", is_flag=True, default=False)
    def validate_scale_command(
        evidence_root: Path,
        run_id: str | None,
        dataset_source: str | None,
        segment_count: int | None,
        model_count: int | None,
        min_segment_count: int | None,
        min_model_count: int | None,
        bbox_set: str | None,
        thresholds_file: Path | None,
        tile_content_type_expectation: str | None,
        frontend_breakpoints: str | None,
        api_base_url: str | None,
        object_prefix: str | None,
        latency_fixture: str | None,
        mvt_contract_artifact: Path | None,
        force: bool,
    ) -> None:
        try:
            summary = validate_scale(
                ProductionScaleConfig.from_env(
                    evidence_root=evidence_root,
                    run_id=run_id,
                    dataset_source=dataset_source,
                    segment_count=segment_count,
                    model_count=model_count,
                    min_segment_count=min_segment_count,
                    min_model_count=min_model_count,
                    bbox_set=bbox_set,
                    thresholds_file=thresholds_file,
                    tile_content_type_expectation=tile_content_type_expectation,
                    frontend_breakpoints=frontend_breakpoints,
                    api_base_url=api_base_url,
                    object_prefix=object_prefix,
                    latency_fixture=latency_fixture,
                    mvt_contract_artifact=mvt_contract_artifact,
                    force=force,
                )
            )
            click.echo(json.dumps(redact_payload(summary), sort_keys=True))
        except ProductionScaleValidationError as error:
            click.echo(f"{error.error_code}: {redact_text(error.message)}", err=True)
            raise SystemExit(1) from error
        except Exception as error:
            click.echo(f"PRODUCTION_SCALE_VALIDATION_FAILED: {redact_text(str(error))}", err=True)
            raise SystemExit(1) from error

    try:
        validate_scale_command.main(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click.ClickException as error:
        error.show()
        raise SystemExit(error.exit_code) from error
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-production validate-scale")
    _add_argparse_options(parser)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                redact_payload(
                    validate_scale(
                        ProductionScaleConfig.from_env(
                            evidence_root=args.evidence_root,
                            run_id=args.run_id,
                            dataset_source=args.dataset_source,
                            segment_count=args.segment_count,
                            model_count=args.model_count,
                            min_segment_count=args.min_segment_count,
                            min_model_count=args.min_model_count,
                            bbox_set=args.bbox_set,
                            thresholds_file=args.thresholds_file,
                            tile_content_type_expectation=args.tile_content_type_expectation,
                            frontend_breakpoints=args.frontend_breakpoints,
                            api_base_url=args.api_base_url,
                            object_prefix=args.object_prefix,
                            latency_fixture=args.latency_fixture,
                            mvt_contract_artifact=args.mvt_contract_artifact,
                            force=args.force,
                        )
                    )
                ),
                sort_keys=True,
            )
        )
    except ProductionScaleValidationError as error:
        print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"PRODUCTION_SCALE_VALIDATION_FAILED: {redact_text(str(error))}", file=sys.stderr)
        return 1
    return 0


def _add_argparse_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--dataset-source", default=None)
    parser.add_argument("--segment-count", type=int, default=None)
    parser.add_argument("--model-count", type=int, default=None)
    parser.add_argument("--min-segment-count", type=int, default=None)
    parser.add_argument("--min-model-count", type=int, default=None)
    parser.add_argument("--bbox-set", default=None)
    parser.add_argument("--thresholds-file", type=Path, default=None)
    parser.add_argument("--tile-content-type-expectation", default=None)
    parser.add_argument("--frontend-breakpoints", default=None)
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--object-prefix", default=None)
    parser.add_argument("--latency-fixture", default=None)
    parser.add_argument("--mvt-contract-artifact", type=Path, default=None)
    parser.add_argument("--force", action="store_true")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
