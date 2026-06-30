from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id
from services.orchestrator import source_cycle_raw_manifest
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
from workers.canonical_converter.converter import evaluate_canonical_readiness
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

REGISTRY_MANIFEST_SCHEMA_VERSION = "nhms.scheduler.file_model_registry.v1"
CANONICAL_READINESS_INDEX_SCHEMA_VERSION = "nhms.scheduler.canonical_readiness_index.v1"
CANONICAL_PRODUCT_CATALOG_SCHEMA_VERSION = "nhms.canonical.product_catalog.v1"
MAX_REGISTRY_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MODEL_PACKAGE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_READINESS_INDEX_BYTES = 16 * 1024 * 1024
MAX_CANONICAL_PRODUCT_CATALOG_BYTES = 16 * 1024 * 1024
MAX_REGISTRY_MODELS = 500
MAX_READINESS_ENTRIES = 5000
MAX_READINESS_PRODUCT_ROWS = 250000
DEFAULT_MAX_MANIFEST_AGE_HOURS = 168
MAX_FILE_PROVIDER_JSON_DEPTH = 64
MAX_FILE_PROVIDER_JSON_NODES = 300_000

__all__ = (
    "CANONICAL_READINESS_INDEX_SCHEMA_VERSION",
    "FileCanonicalReadinessProvider",
    "FileRawHandoffCandidateRepository",
    "FileSchedulerModelRegistry",
    "REGISTRY_MANIFEST_SCHEMA_VERSION",
    "SchedulerFileProviderError",
    "publish_canonical_readiness_index",
    "publish_scheduler_registry_manifest",
)


class SchedulerFileProviderError(RuntimeError):
    def __init__(self, reason: str, *, field: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class _ProviderRoots:
    object_store_root: str | Path | None = None
    object_store_prefix: str | None = None
    published_artifact_root: str | Path | None = None
    now: datetime | None = None
    max_age_hours: int = DEFAULT_MAX_MANIFEST_AGE_HOURS


class FileSchedulerModelRegistry:
    def __init__(
        self,
        manifest_uri: str | Path,
        *,
        object_store_root: str | Path | None = None,
        object_store_prefix: str | None = None,
        published_artifact_root: str | Path | None = None,
        now: datetime | None = None,
        max_age_hours: int = DEFAULT_MAX_MANIFEST_AGE_HOURS,
    ) -> None:
        self.manifest_uri = str(manifest_uri)
        self._roots = _ProviderRoots(
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            now=now,
            max_age_hours=max_age_hours,
        )
        self._loaded = False
        self._models: list[dict[str, Any]] = []
        self._model_by_id: dict[str, dict[str, Any]] = {}
        self._evidence: dict[str, Any] = {
            "status": "not_loaded",
            "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
            "manifest": _uri_evidence(self.manifest_uri),
        }

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        self._load_once()
        rows = list(self._models)
        if basin_version_id not in (None, ""):
            rows = [row for row in rows if row.get("basin_version_id") == basin_version_id]
        if active is True:
            rows = [
                row
                for row in rows
                if row.get("active_flag") is not False and str(row.get("lifecycle_state") or "active") == "active"
            ]
        elif active is False:
            rows = [
                row
                for row in rows
                if row.get("active_flag") is False or str(row.get("lifecycle_state") or "active") != "active"
            ]
        offset = max(int(offset), 0)
        limit = max(int(limit), 0)
        return {
            "items": [dict(row) for row in rows[offset : offset + limit]],
            "total": len(rows),
            "limit": limit,
            "offset": offset,
        }

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        self._load_once()
        try:
            return dict(self._model_by_id[str(model_id)])
        except KeyError as error:
            raise KeyError(model_id) from error

    def get_model_internal(self, model_id: str) -> Mapping[str, Any]:
        return self.get_model(model_id)

    def scheduler_registry_evidence(self) -> dict[str, Any]:
        self._load_once()
        return dict(self._evidence)

    def refresh(self) -> None:
        self._loaded = False
        self._models = []
        self._model_by_id = {}
        self._evidence = {
            "status": "not_loaded",
            "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
            "manifest": _uri_evidence(self.manifest_uri),
        }

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload, content = _read_json_mapping(
                self.manifest_uri,
                roots=self._roots,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            self._models, self._model_by_id, self._evidence = _validate_registry_manifest(
                payload,
                content=content,
                manifest_uri=self.manifest_uri,
                roots=self._roots,
            )
        except SchedulerFileProviderError as error:
            self._models = []
            self._model_by_id = {}
            self._evidence = {
                "status": "blocked",
                "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
                "manifest": _uri_evidence(self.manifest_uri),
                "blockers": [_provider_blocker(error.reason, error.field, evidence=error.evidence)],
            }


class FileCanonicalReadinessProvider:
    def __init__(
        self,
        index_uri: str | Path,
        *,
        object_store_root: str | Path | None = None,
        object_store_prefix: str | None = None,
        published_artifact_root: str | Path | None = None,
        now: datetime | None = None,
        max_age_hours: int = DEFAULT_MAX_MANIFEST_AGE_HOURS,
    ) -> None:
        self.index_uri = str(index_uri)
        self._roots = _ProviderRoots(
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            now=now,
            max_age_hours=max_age_hours,
        )
        self._loaded = False
        self._entries: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._evidence: dict[str, Any] = {
            "status": "not_loaded",
            "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
            "index": _uri_evidence(self.index_uri),
        }

    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        self._load_once()
        index_evidence = dict(self._evidence)
        if index_evidence.get("status") != "ready":
            return _file_readiness_unavailable(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
                reason=str(_first_blocker_reason(index_evidence) or "canonical_readiness_index_unavailable"),
                index_evidence=index_evidence,
                retryable=True,
            )

        key = _readiness_key(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            canonical_product_id=canonical_product_id,
        )
        entry = self._entries.get(key)
        requested_hours = sorted({int(hour) for hour in forecast_hours})
        if entry is None:
            missing_entry = {
                "source_id": source_id,
                "cycle_time": cycle_time,
                "model_id": model_id,
                "basin_id": basin_id,
                "canonical_product_id": canonical_product_id,
                "forecast_hours": requested_hours,
                "policy_identity": dict(policy_identity),
                "source_object_identity": dict(source_object_identity),
                "products": [],
            }
            try:
                products, product_source_evidence = _readiness_products_from_catalog(missing_entry, roots=self._roots)
            except SchedulerFileProviderError as error:
                return _file_readiness_unavailable(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    forecast_hours=forecast_hours,
                    policy_identity=policy_identity,
                    source_object_identity=source_object_identity,
                    canonical_product_id=canonical_product_id,
                    model_id=model_id,
                    basin_id=basin_id,
                    reason=error.reason,
                    index_evidence={
                        **index_evidence,
                        "entry_status": "missing_catalog_unavailable",
                        "catalog": _provider_blocker(error.reason, error.field, evidence=error.evidence),
                    },
                    retryable=True,
                )
            result = evaluate_canonical_readiness(
                source_id=source_id,
                cycle_time=cycle_time,
                products=products,
                forecast_hours=requested_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
            ).evidence
            result = _sanitize_file_provider_evidence(result)
            result["readiness_index"] = _evidence_safe(
                {
                    **index_evidence,
                    "entry_status": "missing",
                    "entry_product_row_count": len(products),
                    "entry_product_source": product_source_evidence.get("source")
                    if products
                    else "missing_identity_zero_rows",
                    "entry_forecast_hours": requested_hours[:200],
                    "entry_forecast_hour_count": len(requested_hours),
                    "canonical_product_catalog": product_source_evidence,
                }
            )
            return _evidence_safe(result)

        entry_policy = dict(entry.get("policy_identity") or {})
        entry_object = dict(entry.get("source_object_identity") or {})
        if _stable_json(entry_policy) != _stable_json(policy_identity) or _stable_json(entry_object) != _stable_json(
            source_object_identity
        ):
            return _file_readiness_unavailable(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
                reason="canonical_readiness_index_identity_mismatch",
                index_evidence={**index_evidence, "entry_status": "identity_mismatch"},
                retryable=True,
            )

        entry_hours = sorted({int(hour) for hour in entry.get("forecast_hours") or []})
        if not set(requested_hours).issubset(set(entry_hours)):
            return _file_readiness_unavailable(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
                reason="canonical_readiness_index_forecast_hours_missing",
                index_evidence={
                    **index_evidence,
                    "entry_status": "forecast_hours_missing",
                    "entry_forecast_hour_count": len(entry_hours),
                    "requested_forecast_hour_count": len(requested_hours),
                },
                retryable=True,
            )

        try:
            products, product_source_evidence = _readiness_entry_products(
                entry,
                roots=self._roots,
            )
        except SchedulerFileProviderError as error:
            return _file_readiness_unavailable(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=canonical_product_id,
                model_id=model_id,
                basin_id=basin_id,
                reason=error.reason,
                index_evidence={
                    **index_evidence,
                    "entry_status": "catalog_unavailable",
                    "catalog": _provider_blocker(error.reason, error.field, evidence=error.evidence),
                },
                retryable=True,
            )

        result = evaluate_canonical_readiness(
            source_id=source_id,
            cycle_time=cycle_time,
            products=products,
            forecast_hours=requested_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=canonical_product_id,
            model_id=model_id,
            basin_id=basin_id,
        ).evidence
        result = _sanitize_file_provider_evidence(result)
        result["readiness_index"] = _evidence_safe(
            {
                **index_evidence,
                "entry_status": "ready",
                "entry_product_row_count": len(products),
                "entry_product_source": product_source_evidence.get("source"),
                "entry_forecast_hours": entry_hours[:200],
                "entry_forecast_hour_count": len(entry_hours),
                "canonical_product_catalog": product_source_evidence,
            }
        )
        return _evidence_safe(result)

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload, content = _read_json_mapping(
                self.index_uri,
                roots=self._roots,
                max_bytes=MAX_READINESS_INDEX_BYTES,
            )
            self._entries, self._evidence = _validate_readiness_index(
                payload,
                content=content,
                index_uri=self.index_uri,
                roots=self._roots,
            )
        except SchedulerFileProviderError as error:
            self._entries = {}
            self._evidence = {
                "status": "blocked",
                "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
                "index": _uri_evidence(self.index_uri),
                "blockers": [_provider_blocker(error.reason, error.field, evidence=error.evidence)],
            }

    def refresh(self) -> None:
        self._loaded = False
        self._entries = {}
        self._evidence = {
            "status": "not_loaded",
            "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
            "index": _uri_evidence(self.index_uri),
        }


class FileRawHandoffCandidateRepository:
    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return False

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return False

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return False

    def active_slurm_jobs(self, *, source_id: str, cycle_time: datetime, model_id: str) -> list[dict[str, Any]]:
        del source_id, cycle_time, model_id
        return []

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        del model_id
        readiness = source_cycle_raw_manifest.nfs_raw_manifest_readiness_from_env(source_id, cycle_time)
        if readiness is None:
            return None
        payload: dict[str, Any] = {
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "candidate_id": candidate_id,
            "nfs_raw_manifest": _public_raw_manifest_evidence(readiness),
        }
        if isinstance(readiness, Mapping) and readiness.get("status") == "ready":
            payload["forecast_cycle"] = _sanitize_file_provider_evidence(
                source_cycle_raw_manifest.forecast_cycle_from_raw_manifest_readiness(
                    readiness,
                    source_id=source_id,
                    cycle_time=cycle_time,
                )
            )
        return payload


def publish_scheduler_registry_manifest(
    models: Sequence[Mapping[str, Any]],
    destination_uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    roots = _ProviderRoots(
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        now=generated_at,
    )
    payload = {
        "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
        "generated_at": _format_utc(generated_at or _now(roots)),
        "models": [dict(model) for model in models],
    }
    content_without_checksum = _canonical_json_bytes(payload)
    checksum = _sha256_label(content_without_checksum)
    payload["checksum"] = checksum
    content = _canonical_json_bytes(payload, pretty=True)
    _validate_registry_manifest(payload, content=content, manifest_uri=str(destination_uri), roots=roots)
    _write_json_bytes(str(destination_uri), content, roots=roots)
    return _evidence_safe(
        {
            "status": "published",
            "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
            "destination": _uri_evidence(destination_uri),
            "checksum": checksum,
            "generated_at": payload["generated_at"],
            "model_count": len(models),
            "manifest_last": True,
            "atomic_write": True,
        }
    )


def publish_canonical_readiness_index(
    entries: Sequence[Mapping[str, Any]],
    destination_uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    roots = _ProviderRoots(
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        now=generated_at,
    )
    payload = {
        "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
        "generated_at": _format_utc(generated_at or _now(roots)),
        "entries": [dict(entry) for entry in entries],
    }
    content_without_checksum = _canonical_json_bytes(payload)
    checksum = _sha256_label(content_without_checksum)
    payload["checksum"] = checksum
    content = _canonical_json_bytes(payload, pretty=True)
    _validate_readiness_index(payload, content=content, index_uri=str(destination_uri), roots=roots)
    _write_json_bytes(str(destination_uri), content, roots=roots)
    return _evidence_safe(
        {
            "status": "published",
            "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
            "destination": _uri_evidence(destination_uri),
            "checksum": checksum,
            "generated_at": payload["generated_at"],
            "entry_count": len(entries),
            "product_row_count": sum(len(entry.get("products") or []) for entry in entries),
            "index_last": True,
            "atomic_write": True,
        }
    )


def _validate_registry_manifest(
    payload: Mapping[str, Any],
    *,
    content: bytes,
    manifest_uri: str,
    roots: _ProviderRoots,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    _require_schema(payload, REGISTRY_MANIFEST_SCHEMA_VERSION, field="schema_version")
    generated_at = _require_fresh_generated_at(payload, field="generated_at", roots=roots)
    _require_payload_checksum(payload, field="checksum")
    models = payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes | bytearray):
        raise SchedulerFileProviderError("registry_models_invalid", field="models")
    if len(models) > MAX_REGISTRY_MODELS:
        raise SchedulerFileProviderError(
            "registry_model_limit_exceeded",
            field="models",
            evidence={"model_count": len(models), "max_models": MAX_REGISTRY_MODELS},
        )
    seen_model_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(models):
        if not isinstance(item, Mapping):
            raise SchedulerFileProviderError("registry_model_not_object", field=f"models[{index}]")
        row = _normalize_registry_model(item, index=index, roots=roots)
        model_id = str(row["model_id"])
        if model_id in seen_model_ids:
            raise SchedulerFileProviderError(
                "registry_duplicate_model_id",
                field="models[].model_id",
                evidence={"model_id": model_id},
            )
        seen_model_ids.add(model_id)
        rows.append(row)
    evidence = {
        "status": "ready",
        "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
        "manifest": _uri_evidence(manifest_uri),
        "generated_at": _format_utc(generated_at),
        "checksum": _safe_checksum(payload.get("checksum")),
        "content_checksum_verified": _checksum_matches(payload.get("checksum"), _payload_checksum(payload)),
        "model_count": len(rows),
        "model_ids": [row["model_id"] for row in rows[:200]],
        "manifest_bytes": len(content),
    }
    return rows, {str(row["model_id"]): dict(row) for row in rows}, _evidence_safe(evidence)


def _validate_readiness_index(
    payload: Mapping[str, Any],
    *,
    content: bytes,
    index_uri: str,
    roots: _ProviderRoots,
) -> tuple[dict[tuple[str, str, str, str, str], dict[str, Any]], dict[str, Any]]:
    _require_schema(payload, CANONICAL_READINESS_INDEX_SCHEMA_VERSION, field="schema_version")
    generated_at = _require_fresh_generated_at(payload, field="generated_at", roots=roots)
    _require_payload_checksum(payload, field="checksum")
    entries_value = payload.get("entries", payload.get("cycles"))
    if not isinstance(entries_value, Sequence) or isinstance(entries_value, str | bytes | bytearray):
        raise SchedulerFileProviderError("readiness_entries_invalid", field="entries")
    if len(entries_value) > MAX_READINESS_ENTRIES:
        raise SchedulerFileProviderError(
            "readiness_entry_limit_exceeded",
            field="entries",
            evidence={"entry_count": len(entries_value), "max_entries": MAX_READINESS_ENTRIES},
        )
    entries: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    total_products = 0
    object_count = 0
    for index, item in enumerate(entries_value):
        if not isinstance(item, Mapping):
            raise SchedulerFileProviderError("readiness_entry_not_object", field=f"entries[{index}]")
        entry = _normalize_readiness_entry(item, index=index, roots=roots)
        total_products += len(entry.get("products") or [])
        object_count += int(entry.get("object_count") or 0)
        if total_products > MAX_READINESS_PRODUCT_ROWS:
            raise SchedulerFileProviderError(
                "readiness_product_row_limit_exceeded",
                field="entries[].products",
                evidence={"product_row_count": total_products, "max_product_rows": MAX_READINESS_PRODUCT_ROWS},
            )
        key = _readiness_key(
            source_id=str(entry["source_id"]),
            cycle_time=parse_cycle_time(entry["cycle_time"]),
            model_id=str(entry["model_id"]),
            basin_id=str(entry["basin_id"]),
            canonical_product_id=str(entry["canonical_product_id"]),
        )
        if key in entries:
            raise SchedulerFileProviderError(
                "readiness_duplicate_identity",
                field="entries[]",
                evidence={"source_id": key[0], "cycle_time": key[1], "model_id": key[2], "basin_id": key[3]},
            )
        entries[key] = entry
    evidence = {
        "status": "ready",
        "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
        "index": _uri_evidence(index_uri),
        "generated_at": _format_utc(generated_at),
        "checksum": _safe_checksum(payload.get("checksum")),
        "content_checksum_verified": _checksum_matches(payload.get("checksum"), _payload_checksum(payload)),
        "entry_count": len(entries),
        "product_row_count": total_products,
        "object_count": object_count,
        "index_bytes": len(content),
    }
    return entries, _evidence_safe(evidence)


def _normalize_registry_model(item: Mapping[str, Any], *, index: int, roots: _ProviderRoots) -> dict[str, Any]:
    required = (
        "model_id",
        "basin_id",
        "basin_version_id",
        "river_network_version_id",
        "model_package_uri",
        "manifest_uri",
        "package_checksum",
        "resource_profile",
        "display_capabilities",
        "shud_code_version",
    )
    row = dict(item)
    for field in required:
        if row.get(field) in (None, ""):
            raise SchedulerFileProviderError("registry_model_required_field_missing", field=f"models[{index}].{field}")
    resource_profile = _required_mapping(row.get("resource_profile"), field=f"models[{index}].resource_profile")
    display_capabilities = _required_mapping(
        row.get("display_capabilities"),
        field=f"models[{index}].display_capabilities",
        allow_empty=True,
    )
    segment_count = _optional_nonnegative_int(row.get("segment_count"), field=f"models[{index}].segment_count")
    output_segment_count = _optional_nonnegative_int(
        row.get("output_segment_count", resource_profile.get("output_segment_count")),
        field=f"models[{index}].output_segment_count",
    )
    source_policy = _mapping(row.get("source_policy") or row.get("source_policy_metadata"))
    model_package_uri = str(row["model_package_uri"])
    manifest_uri = str(row["manifest_uri"])
    package_checksum = str(row["package_checksum"])
    _require_supported_internal_reference(
        model_package_uri,
        roots=roots,
        field=f"models[{index}].model_package_uri",
        reason_prefix="registry_model_package_uri",
    )
    _verify_referenced_checksum(
        manifest_uri,
        package_checksum,
        roots=roots,
        field=f"models[{index}].manifest_uri",
        max_bytes=MAX_MODEL_PACKAGE_MANIFEST_BYTES,
        reason_prefix="registry_model_package_manifest",
        allow_embedded_checksum=True,
    )
    resource_profile = {
        **resource_profile,
        "manifest_uri": manifest_uri,
        "package_checksum": package_checksum,
        "display_capabilities": display_capabilities,
    }
    if output_segment_count is not None:
        resource_profile["output_segment_count"] = output_segment_count
    if source_policy:
        resource_profile["source_policy"] = source_policy
    return {
        **row,
        "model_id": str(row["model_id"]),
        "basin_id": str(row["basin_id"]),
        "basin_version_id": str(row["basin_version_id"]),
        "river_network_version_id": str(row["river_network_version_id"]),
        "model_package_uri": model_package_uri,
        "manifest_uri": manifest_uri,
        "package_checksum": package_checksum,
        "segment_count": segment_count,
        "output_segment_count": output_segment_count,
        "shud_code_version": str(row["shud_code_version"]),
        "active_flag": row.get("active_flag", True) is not False,
        "lifecycle_state": str(row.get("lifecycle_state") or "active"),
        "resource_profile": resource_profile,
        "display_capabilities": display_capabilities,
        "source_policy": source_policy,
    }


def _normalize_readiness_entry(item: Mapping[str, Any], *, index: int, roots: _ProviderRoots) -> dict[str, Any]:
    row = dict(item)
    for field in ("source_id", "cycle_time", "model_id", "basin_id"):
        if row.get(field) in (None, ""):
            raise SchedulerFileProviderError(
                "readiness_entry_required_field_missing",
                field=f"entries[{index}].{field}",
            )
    source_id = normalize_source_id(str(row["source_id"]))
    cycle_time = parse_cycle_time(row["cycle_time"])
    canonical_product_id = str(
        row.get("canonical_product_id") or f"canon_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    )
    forecast_hours = _forecast_hours(row.get("forecast_hours"), field=f"entries[{index}].forecast_hours")
    policy_identity = _mapping(row.get("policy_identity") or row.get("source_policy"))
    source_object_identity = _mapping(row.get("source_object_identity") or row.get("object_identity"))
    products_value = row.get("products")
    if not isinstance(products_value, Sequence) or isinstance(products_value, str | bytes | bytearray):
        raise SchedulerFileProviderError("readiness_products_invalid", field=f"entries[{index}].products")
    products: list[dict[str, Any]] = []
    object_count = 0
    for product_index, product in enumerate(products_value):
        if not isinstance(product, Mapping):
            raise SchedulerFileProviderError(
                "readiness_product_not_object",
                field=f"entries[{index}].products[{product_index}]",
            )
        normalized = _normalize_product_row(
            product,
            index=index,
            product_index=product_index,
            source_id=source_id,
            cycle_time=cycle_time,
            canonical_product_id=canonical_product_id,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            roots=roots,
        )
        products.append(normalized)
        if normalized.get("object_uri") not in (None, ""):
            object_count += 1
    return {
        **row,
        "source_id": source_id,
        "cycle_time": _format_utc(cycle_time),
        "model_id": str(row["model_id"]),
        "basin_id": str(row["basin_id"]),
        "canonical_product_id": canonical_product_id,
        "forecast_hours": forecast_hours,
        "policy_identity": policy_identity,
        "source_object_identity": source_object_identity,
        "products": products,
        "product_row_count": len(products),
        "object_count": object_count,
    }


def _readiness_entry_products(
    entry: Mapping[str, Any],
    *,
    roots: _ProviderRoots,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    products = list(entry.get("products") or [])
    if products:
        return products, {"status": "not_needed", "source": "index", "product_row_count": len(products)}

    catalog_products, catalog_evidence = _readiness_products_from_catalog(entry, roots=roots)
    if catalog_products:
        return catalog_products, catalog_evidence
    return products, catalog_evidence


def _readiness_products_from_catalog(
    entry: Mapping[str, Any],
    *,
    roots: _ProviderRoots,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_id = normalize_source_id(str(entry["source_id"]))
    cycle_time = parse_cycle_time(entry["cycle_time"])
    catalog_uris = _canonical_product_catalog_uris(source_id=source_id, cycle_time=cycle_time, roots=roots)
    missing_uris: list[str] = []
    for catalog_uri in catalog_uris:
        try:
            payload, content = _read_json_mapping(
                catalog_uri,
                roots=roots,
                max_bytes=MAX_CANONICAL_PRODUCT_CATALOG_BYTES,
            )
        except SchedulerFileProviderError as error:
            if error.reason == "file_manifest_missing":
                missing_uris.append(_uri_evidence(catalog_uri))
                continue
            raise
        products = _normalize_catalog_products(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            entry=entry,
            roots=roots,
        )
        return products, {
            "status": "ready",
            "source": "catalog",
            "schema_version": CANONICAL_PRODUCT_CATALOG_SCHEMA_VERSION,
            "catalog": _uri_evidence(catalog_uri),
            "catalog_bytes": len(content),
            "product_row_count": len(products),
        }
    return [], {
        "status": "missing",
        "source": "index_empty",
        "schema_version": CANONICAL_PRODUCT_CATALOG_SCHEMA_VERSION,
        "catalogs_checked": missing_uris,
        "product_row_count": 0,
    }


def _canonical_product_catalog_uris(
    *,
    source_id: str,
    cycle_time: datetime,
    roots: _ProviderRoots,
) -> list[str]:
    catalog_key = f"canonical/{source_id}/{format_cycle_time(cycle_time)}/_catalog/catalog.json"
    uris: list[str] = []
    object_store_prefix = str(roots.object_store_prefix or os.getenv("OBJECT_STORE_PREFIX") or "").strip()
    if object_store_prefix:
        uris.append(f"{object_store_prefix.rstrip('/')}/{catalog_key}")
    object_store_root = roots.object_store_root or os.getenv("OBJECT_STORE_ROOT")
    if object_store_root not in (None, ""):
        uris.append(str(Path(object_store_root).expanduser() / catalog_key))
    if not uris:
        uris.append(catalog_key)
    return list(dict.fromkeys(uris))


def _normalize_catalog_products(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    entry: Mapping[str, Any],
    roots: _ProviderRoots,
) -> list[dict[str, Any]]:
    _require_schema(payload, CANONICAL_PRODUCT_CATALOG_SCHEMA_VERSION, field="schema_version")
    payload_source = normalize_source_id(str(payload.get("source_id") or source_id))
    if payload_source != source_id:
        raise SchedulerFileProviderError("canonical_product_catalog_source_mismatch", field="source_id")
    payload_cycle = parse_cycle_time(payload.get("cycle_time", cycle_time))
    if format_cycle_time(payload_cycle) != format_cycle_time(cycle_time):
        raise SchedulerFileProviderError("canonical_product_catalog_cycle_mismatch", field="cycle_time")

    products_value = payload.get("products")
    if not isinstance(products_value, Sequence) or isinstance(products_value, str | bytes | bytearray):
        raise SchedulerFileProviderError("canonical_product_catalog_products_invalid", field="products")

    products: list[dict[str, Any]] = []
    policy_identity = _mapping(entry.get("policy_identity"))
    source_object_identity = _mapping(entry.get("source_object_identity"))
    canonical_product_id = str(entry["canonical_product_id"])
    for product_index, product in enumerate(products_value):
        if not isinstance(product, Mapping):
            raise SchedulerFileProviderError(
                "canonical_product_catalog_product_not_object",
                field=f"products[{product_index}]",
            )
        products.append(
            _normalize_product_row(
                product,
                index=0,
                product_index=product_index,
                source_id=source_id,
                cycle_time=cycle_time,
                canonical_product_id=canonical_product_id,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                roots=roots,
            )
        )
    return products


def _normalize_product_row(
    product: Mapping[str, Any],
    *,
    index: int,
    product_index: int,
    source_id: str,
    cycle_time: datetime,
    canonical_product_id: str,
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
    roots: _ProviderRoots,
) -> dict[str, Any]:
    row = dict(product)
    for field in ("variable", "lead_time_hours", "object_uri", "checksum"):
        if row.get(field) in (None, ""):
            raise SchedulerFileProviderError(
                "readiness_product_required_field_missing",
                field=f"entries[{index}].products[{product_index}].{field}",
            )
    row_source_id = normalize_source_id(str(row.get("source_id") or source_id))
    if row_source_id != source_id:
        raise SchedulerFileProviderError(
            "readiness_product_source_mismatch",
            field=f"entries[{index}].products[{product_index}].source_id",
        )
    row_cycle_time = parse_cycle_time(row.get("cycle_time", cycle_time))
    if format_cycle_time(row_cycle_time) != format_cycle_time(cycle_time):
        raise SchedulerFileProviderError(
            "readiness_product_cycle_mismatch",
            field=f"entries[{index}].products[{product_index}].cycle_time",
        )
    object_uri = str(row["object_uri"])
    checksum = str(row["checksum"])
    _verify_referenced_checksum(
        object_uri,
        checksum,
        roots=roots,
        field=f"entries[{index}].products[{product_index}].object_uri",
        max_bytes=MAX_READINESS_INDEX_BYTES,
        reason_prefix="readiness_product_object",
        allow_embedded_checksum=False,
    )
    lineage = _mapping(row.get("lineage_json"))
    if "policy_identity" not in lineage:
        lineage["policy_identity"] = dict(policy_identity)
    if "source_object_identity" not in lineage:
        lineage["source_object_identity"] = dict(source_object_identity)
    return {
        **row,
        "source_id": source_id,
        "cycle_time": _format_utc(cycle_time),
        "canonical_product_id": str(row.get("canonical_product_id") or canonical_product_id),
        "lead_time_hours": int(row["lead_time_hours"]),
        "object_uri": object_uri,
        "checksum": checksum,
        "quality_flag": str(row.get("quality_flag") or "ok"),
        "lineage_json": lineage,
    }


def _read_json_mapping(uri: str, *, roots: _ProviderRoots, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    try:
        exists = _uri_exists(uri, roots=roots)
    except (OSError, SafeFilesystemError, ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError(
            "file_manifest_unreadable",
            field="manifest",
            evidence={"error_type": type(error).__name__},
        ) from error
    if not exists:
        raise SchedulerFileProviderError("file_manifest_missing", field="manifest")
    try:
        content = _read_bytes(uri, roots=roots, max_bytes=max_bytes)
    except FileNotFoundError as error:
        raise SchedulerFileProviderError("file_manifest_missing", field="manifest") from error
    except (OSError, SafeFilesystemError, ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError(
            "file_manifest_unreadable",
            field="manifest",
            evidence={"error_type": type(error).__name__},
        ) from error
    if len(content) > max_bytes:
        raise SchedulerFileProviderError(
            "file_manifest_size_limit_exceeded",
            field="manifest",
            evidence={"max_bytes": max_bytes},
        )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SchedulerFileProviderError(
            "file_manifest_malformed_json",
            field="manifest",
            evidence={"error_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping):
        raise SchedulerFileProviderError("file_manifest_not_object", field="manifest")
    _validate_json_complexity(payload)
    return dict(payload), content


def _read_bytes(uri: str, *, roots: _ProviderRoots, max_bytes: int) -> bytes:
    parsed = urlparse(uri)
    if parsed.scheme in {"s3", "published"}:
        return _object_store_for(uri, roots).read_bytes_limited(_object_key_for_uri(uri), max_bytes=max_bytes)
    path = Path(uri).expanduser()
    content = read_bytes_limited_no_follow(path, max_bytes=max_bytes)
    if len(content) > max_bytes:
        raise SchedulerFileProviderError("file_manifest_size_limit_exceeded", field="manifest")
    return content


def _write_json_bytes(uri: str, content: bytes, *, roots: _ProviderRoots) -> None:
    parsed = urlparse(uri)
    if parsed.scheme in {"s3", "published"}:
        _object_store_for(uri, roots).write_bytes_atomic(_object_key_for_uri(uri), content)
        return
    path = Path(uri).expanduser()
    atomic_write_bytes_no_follow(path, content)


def _object_store_for(uri: str, roots: _ProviderRoots) -> LocalObjectStore:
    parsed = urlparse(uri)
    if parsed.scheme == "published":
        root = roots.published_artifact_root or roots.object_store_root or os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT")
        prefix = "published://"
    else:
        root = roots.object_store_root or os.getenv("OBJECT_STORE_ROOT")
        prefix = roots.object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", "")
    if root in (None, ""):
        raise ObjectStoreError("object store root is required for file provider object URI reads")
    return LocalObjectStore(root, object_store_prefix=prefix or "")


def _require_supported_internal_reference(
    uri: str,
    *,
    roots: _ProviderRoots,
    field: str,
    reason_prefix: str,
) -> None:
    parsed = urlparse(uri)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in {"s3", "published"}:
        raise SchedulerFileProviderError(f"{reason_prefix}_unsupported_uri", field=field)
    try:
        if scheme == "s3" and not (roots.object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", "")).strip():
            raise ValueError("object_store_prefix_required")
        key = _object_key_for_uri(uri)
        store = _object_store_for(uri, roots)
        store.normalize_key(key)
    except (ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError(
            f"{reason_prefix}_unsafe_uri",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error


def _object_key_for_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "published":
        return "/".join(part.strip("/") for part in (parsed.netloc, parsed.path) if part.strip("/"))
    return uri


def _verify_referenced_checksum(
    uri: str,
    expected_checksum: str,
    *,
    roots: _ProviderRoots,
    field: str,
    max_bytes: int,
    reason_prefix: str,
    allow_embedded_checksum: bool,
) -> None:
    _require_supported_internal_reference(uri, roots=roots, field=field, reason_prefix=reason_prefix)
    try:
        exists = _uri_exists(uri, roots=roots)
    except (OSError, SafeFilesystemError, ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError(
            f"{reason_prefix}_unreadable",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    if not exists:
        raise SchedulerFileProviderError(f"{reason_prefix}_missing", field=field)
    try:
        content = _read_bytes(uri, roots=roots, max_bytes=max_bytes)
    except FileNotFoundError as error:
        raise SchedulerFileProviderError(f"{reason_prefix}_missing", field=field) from error
    except SchedulerFileProviderError:
        raise
    except (OSError, SafeFilesystemError, ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError(
            f"{reason_prefix}_unreadable",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    actual_checksum = sha256_bytes(content)
    if _checksum_matches(expected_checksum, actual_checksum):
        return
    if not allow_embedded_checksum:
        raise SchedulerFileProviderError(f"{reason_prefix}_checksum_mismatch", field=field)
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        manifest = None
    if isinstance(manifest, Mapping):
        for key in ("package_checksum", "checksum", "sha256"):
            if _checksum_matches(expected_checksum, manifest.get(key)):
                return
    raise SchedulerFileProviderError(f"{reason_prefix}_checksum_mismatch", field=field)


def _uri_exists(uri: str, *, roots: _ProviderRoots) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme in {"s3", "published"}:
        return _object_store_for(uri, roots).exists(_object_key_for_uri(uri))
    try:
        stat_no_follow(Path(uri).expanduser())
    except FileNotFoundError:
        return False
    return True


def _validate_json_complexity(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > MAX_FILE_PROVIDER_JSON_NODES:
            raise SchedulerFileProviderError(
                "file_manifest_json_node_limit_exceeded",
                field="manifest",
                evidence={"max_nodes": MAX_FILE_PROVIDER_JSON_NODES},
            )
        if depth > MAX_FILE_PROVIDER_JSON_DEPTH:
            raise SchedulerFileProviderError(
                "file_manifest_json_depth_exceeded",
                field="manifest",
                evidence={"max_depth": MAX_FILE_PROVIDER_JSON_DEPTH},
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            stack.extend((child, depth + 1) for child in item)


def _require_schema(payload: Mapping[str, Any], expected: str, *, field: str) -> None:
    if payload.get(field) != expected:
        raise SchedulerFileProviderError("file_manifest_schema_unsupported", field=field)


def _require_fresh_generated_at(payload: Mapping[str, Any], *, field: str, roots: _ProviderRoots) -> datetime:
    try:
        generated_at = parse_cycle_time(str(payload.get(field) or ""))
    except (TypeError, ValueError) as error:
        raise SchedulerFileProviderError("file_manifest_generated_at_invalid", field=field) from error
    now = _now(roots)
    max_age = timedelta(hours=max(int(roots.max_age_hours), 1))
    if generated_at > now + timedelta(minutes=5):
        raise SchedulerFileProviderError("file_manifest_generated_at_future", field=field)
    if now - generated_at > max_age:
        raise SchedulerFileProviderError(
            "file_manifest_stale",
            field=field,
            evidence={"max_age_hours": int(roots.max_age_hours)},
        )
    return generated_at


def _require_payload_checksum(payload: Mapping[str, Any], *, field: str) -> None:
    checksum = payload.get(field)
    if checksum in (None, ""):
        raise SchedulerFileProviderError("file_manifest_checksum_missing", field=field)
    actual = _payload_checksum(payload)
    if not _checksum_matches(checksum, actual):
        raise SchedulerFileProviderError("file_manifest_checksum_mismatch", field=field)


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    content = _canonical_json_bytes({key: value for key, value in payload.items() if key != "checksum"})
    return sha256_bytes(content)


def _canonical_json_bytes(payload: Mapping[str, Any], *, pretty: bool = False) -> bytes:
    if pretty:
        return json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256_label(content: bytes) -> str:
    return f"sha256:{sha256_bytes(content)}"


def _checksum_matches(expected: Any, actual: Any) -> bool:
    if expected in (None, "") or actual in (None, ""):
        return False
    return _checksum_value(expected) == _checksum_value(actual)


def _checksum_value(value: Any) -> str:
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        return text.split(":", 1)[1]
    return text


def _safe_checksum(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return "sha256:[redacted]" if text.startswith("sha256:") else "[redacted]"


def _uri_evidence(value: str | Path) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme:
        return "[object-uri]" if parsed.scheme in {"s3", "published"} else "[uri]"
    return "[local-path]"


def _provider_blocker(reason: str, field: str, *, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _evidence_safe(
        {
            "code": reason,
            "reason": reason,
            "field": field,
            "message": "DB-free scheduler file provider validation failed closed.",
            **dict(evidence or {}),
        }
    )


def _file_readiness_unavailable(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    policy_identity: Mapping[str, Any],
    source_object_identity: Mapping[str, Any],
    canonical_product_id: str,
    model_id: str,
    basin_id: str,
    reason: str,
    index_evidence: Mapping[str, Any],
    retryable: bool,
) -> dict[str, Any]:
    parsed_cycle_time = _ensure_utc(cycle_time)
    return _evidence_safe(
        {
            "source": source_id,
            "source_id": source_id,
            "cycle_id": cycle_id_for(source_id, parsed_cycle_time),
            "cycle_time": _format_utc(parsed_cycle_time),
            "status": "canonical_unavailable",
            "ready": False,
            "reason": reason,
            "canonical_product_id": canonical_product_id,
            "model_id": model_id,
            "basin_id": basin_id,
            "expected_leads": list(forecast_hours),
            "accepted_horizon": _accepted_horizon_from_hours(forecast_hours),
            "policy_identity": _sanitize_file_provider_evidence(dict(policy_identity)),
            "source_object_identity": _sanitize_file_provider_evidence(dict(source_object_identity)),
            "policy_identity_matched": False,
            "source_object_identity_matched": False,
            "readiness_index": dict(index_evidence),
            "dependency": {
                "name": "file_canonical_readiness_index",
                "status": "unavailable",
                "retryable": retryable,
            },
            "failure": {
                "classifier": "file_readiness_unavailable",
                "reason_code": reason.upper(),
                "dependency": "file_canonical_readiness_index",
                "retryable": retryable,
                "permanent": not retryable,
            },
        }
    )


def _accepted_horizon_from_hours(forecast_hours: Sequence[int]) -> dict[str, Any]:
    hours = sorted({int(hour) for hour in forecast_hours})
    return {
        "first_lead_hour": min(hours) if hours else None,
        "last_lead_hour": max(hours) if hours else None,
        "lead_count": len(hours),
    }


def _readiness_key(
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    basin_id: str,
    canonical_product_id: str,
) -> tuple[str, str, str, str, str]:
    return (
        normalize_source_id(source_id),
        format_cycle_time(parse_cycle_time(cycle_time)),
        str(model_id),
        str(basin_id),
        str(canonical_product_id),
    )


def _first_blocker_reason(evidence: Mapping[str, Any]) -> str | None:
    blockers = evidence.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, str | bytes | bytearray) and blockers:
        first = blockers[0]
        if isinstance(first, Mapping):
            return str(first.get("reason") or first.get("code") or "") or None
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _required_mapping(value: Any, *, field: str, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchedulerFileProviderError("file_manifest_mapping_invalid", field=field)
    result = dict(value)
    if not result and not allow_empty:
        raise SchedulerFileProviderError("file_manifest_mapping_empty", field=field)
    return result


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise SchedulerFileProviderError("file_manifest_nonnegative_int_invalid", field=field) from error
    if parsed < 0:
        raise SchedulerFileProviderError("file_manifest_nonnegative_int_invalid", field=field)
    return parsed


def _forecast_hours(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise SchedulerFileProviderError("readiness_forecast_hours_invalid", field=field)
    hours: list[int] = []
    for item in value:
        try:
            hour = int(item)
        except (TypeError, ValueError) as error:
            raise SchedulerFileProviderError("readiness_forecast_hours_invalid", field=field) from error
        if hour < 0:
            raise SchedulerFileProviderError("readiness_forecast_hours_invalid", field=field)
        hours.append(hour)
    return sorted(set(hours))


def _now(roots: _ProviderRoots) -> datetime:
    if roots.now is not None:
        return _ensure_utc(roots.now)
    return datetime.now(tz=UTC)


def _stable_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def _public_raw_manifest_evidence(readiness: Mapping[str, Any]) -> dict[str, Any]:
    hidden_local_fields = {"object_store_root", "manifest_path", "source_object_store_root", "target_object_store_root"}
    payload: dict[str, Any] = {}
    for key, value in readiness.items():
        key_text = str(key)
        if key_text in hidden_local_fields:
            continue
        payload[key_text] = _sanitize_file_provider_evidence_scalar(key_text, value)
    for key in hidden_local_fields:
        if key in readiness and readiness.get(key) not in (None, ""):
            payload[key] = "[local-path]"
    return _evidence_safe(payload)


def _sanitize_file_provider_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_file_provider_evidence_scalar(str(key), nested)
            for key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_file_provider_evidence(item) for item in value]
    return _sanitize_file_provider_scalar(value)


def _sanitize_file_provider_evidence_scalar(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered.endswith("_path") or lowered.endswith("_root") or lowered in {"path", "root"}:
        return "[local-path]" if value not in (None, "") else value
    if lowered.endswith("_uri") or lowered in {"uri", "object_uri", "manifest_uri"}:
        return _sanitize_file_provider_scalar(value)
    return _sanitize_file_provider_evidence(value)


def _sanitize_file_provider_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    parsed = urlparse(text)
    if parsed.scheme in {"s3", "published"}:
        return "[object-uri]"
    if parsed.scheme:
        return "[uri]"
    if text.startswith("/") or text.startswith("~"):
        return "[local-path]"
    return value
