from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    read_provider_snapshot,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
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
MAX_CANONICAL_PRODUCT_OBJECT_BYTES = 64 * 1024 * 1024 * 1024
MAX_REGISTRY_MODELS = 500
MAX_READINESS_ENTRIES = 5000
MAX_READINESS_PRODUCT_ROWS = 250000
MAX_FILE_PROVIDER_OBJECT_STORE_CACHE_ENTRIES = 16
DEFAULT_MAX_MANIFEST_AGE_HOURS = 168
MAX_FILE_PROVIDER_JSON_DEPTH = 64
MAX_FILE_PROVIDER_JSON_NODES = 300_000
MAX_CANONICAL_CATALOG_CYCLE_DIRS = 4096
READINESS_DERIVATION_SOURCES = ("gfs", "IFS")
_COMPACT_CYCLE_RE = re.compile(r"^[0-9]{10}$")
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")

__all__ = (
    "CANONICAL_READINESS_INDEX_SCHEMA_VERSION",
    "FileCanonicalReadinessProvider",
    "FileRawHandoffCandidateRepository",
    "FileSchedulerModelRegistry",
    "REGISTRY_MANIFEST_SCHEMA_VERSION",
    "SchedulerFileProviderError",
    "capture_scheduler_provider_preimage",
    "derive_catalog_bound_readiness_entries",
    "load_canonical_readiness_entries_for_renewal",
    "publish_canonical_readiness_index",
    "publish_scheduler_registry_manifest",
    "validate_catalog_bound_readiness_entries",
    "validate_readiness_registry_model_set",
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
    object_store_cache: dict[tuple[str, str, str], LocalObjectStore] = dataclass_field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


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
        resolved_object_store_root = object_store_root or _infer_object_store_root_from_scheduler_file_uri(
            self.index_uri
        )
        self._roots = _ProviderRoots(
            object_store_root=resolved_object_store_root,
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
        entry_hours = sorted({int(hour) for hour in entry.get("forecast_hours") or []})
        if _stable_json(entry_policy) != _stable_json(policy_identity) or _stable_json(entry_object) != _stable_json(
            source_object_identity
        ):
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
                    reason="canonical_readiness_index_identity_mismatch",
                    index_evidence={
                        **index_evidence,
                        "entry_status": "identity_mismatch",
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
            if not result.get("ready"):
                result["reason"] = "canonical_identity_mismatch_cache_miss"
            result["readiness_index"] = _evidence_safe(
                {
                    **index_evidence,
                    "entry_status": "identity_mismatch_recomputed",
                    "entry_product_row_count": len(products),
                    "entry_product_source": product_source_evidence.get("source"),
                    "entry_forecast_hours": entry_hours[:200],
                    "entry_forecast_hour_count": len(entry_hours),
                    "requested_forecast_hours": requested_hours[:200],
                    "requested_forecast_hour_count": len(requested_hours),
                    "canonical_product_catalog": product_source_evidence,
                    "recomputed_product_row_count": len(products),
                }
            )
            return _evidence_safe(result)

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


def _infer_object_store_root_from_scheduler_file_uri(uri: str | Path) -> Path | None:
    parsed = urlparse(str(uri))
    if parsed.scheme:
        return None
    path = Path(uri).expanduser()
    if path.parent.name != "canonical-readiness":
        return None
    scheduler_dir = path.parent.parent
    if scheduler_dir.name != "scheduler":
        return None
    return scheduler_dir.parent


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
    expected_preimage: ProviderPreimage | Mapping[str, object] | None = None,
    commit_observer: Callable[[ProviderPreimage], None] | None = None,
    cutover_gate: Mapping[str, Any] | None = None,
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
    committed = _write_json_bytes(
        str(destination_uri),
        content,
        roots=roots,
        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
        expected_preimage=expected_preimage,
    )
    if commit_observer is not None:
        commit_observer(committed)
    receipt: dict[str, Any] = {
        "status": "published",
        "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
        "destination": _uri_evidence(destination_uri),
        "checksum": checksum,
        "content_sha256": sha256_bytes(content),
        "generated_at": payload["generated_at"],
        "model_count": len(models),
        "manifest_last": True,
        "atomic_write": True,
    }
    # R2-A1: mirror the caller's cutover_gate audit block on the receipt so
    # downstream operators reading `manifest-last.json`'s companion receipt
    # see the same audit fact the CLI summary/runner receipt records.
    if cutover_gate is not None:
        receipt["cutover_gate"] = {
            "mode": str(cutover_gate.get("mode") or "not_wired"),
            "declaration_env": cutover_gate.get("declaration_env"),
            "declaration_present": bool(cutover_gate.get("declaration_present")),
        }
    return _evidence_safe(receipt)


def publish_canonical_readiness_index(
    entries: Sequence[Mapping[str, Any]],
    destination_uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    generated_at: datetime | None = None,
    expected_preimage: ProviderPreimage | Mapping[str, object] | None = None,
    verify_external_references: bool = False,
    commit_observer: Callable[[ProviderPreimage], None] | None = None,
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
    _validate_readiness_index(
        payload,
        content=content,
        index_uri=str(destination_uri),
        roots=roots,
        verify_external_references=verify_external_references,
    )
    committed = _write_json_bytes(
        str(destination_uri),
        content,
        roots=roots,
        max_bytes=MAX_READINESS_INDEX_BYTES,
        expected_preimage=expected_preimage,
    )
    if commit_observer is not None:
        commit_observer(committed)
    return _evidence_safe(
        {
            "status": "published",
            "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
            "destination": _uri_evidence(destination_uri),
            "checksum": checksum,
            "content_sha256": sha256_bytes(content),
            "generated_at": payload["generated_at"],
            "entry_count": len(entries),
            "product_row_count": sum(len(entry.get("products") or []) for entry in entries),
            "index_last": True,
            "atomic_write": True,
        }
    )


def derive_catalog_bound_readiness_entries(
    registry_models: Sequence[Mapping[str, Any]],
    *,
    object_store_root: str | Path,
    object_store_prefix: str,
    sources: Sequence[str] = READINESS_DERIVATION_SOURCES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a fresh readiness generation from current private catalogs.

    The newest cycle directory for every configured source is authoritative.
    An invalid newest catalog blocks the generation; older catalogs are never
    searched as a fallback.  Only bounded catalog metadata and product rows are
    read, and the returned index entries externalize products behind an exact
    URI/content-digest/row-count binding.
    """

    root = Path(object_store_root).expanduser()
    if not root.is_absolute():
        raise SchedulerFileProviderError("canonical_catalog_root_invalid", field="object_store_root")
    prefix = str(object_store_prefix or "").strip()
    if not prefix:
        raise SchedulerFileProviderError("canonical_catalog_prefix_missing", field="object_store_prefix")
    normalized_sources = tuple(dict.fromkeys(normalize_source_id(str(source)) for source in sources))
    if not normalized_sources:
        raise SchedulerFileProviderError("readiness_derivation_sources_empty", field="sources")
    models = _registry_readiness_identities(registry_models)
    roots = _ProviderRoots(object_store_root=root, object_store_prefix=prefix)
    entries: list[dict[str, Any]] = []
    catalogs: list[dict[str, Any]] = []
    for source_id in normalized_sources:
        snapshot = _latest_canonical_catalog_snapshot(source_id=source_id, roots=roots)
        catalog_entry = {
            "source_id": source_id,
            "cycle_time": snapshot["cycle_time"],
            "canonical_product_id": snapshot["canonical_product_id"],
            "forecast_hours": snapshot["forecast_hours"],
            "policy_identity": snapshot["policy_identity"],
            "source_object_identity": snapshot["source_object_identity"],
            "products": [],
            "catalog_uri": snapshot["catalog_uri"],
            "catalog_sha256": snapshot["catalog_sha256"],
            "catalog_row_count": snapshot["catalog_row_count"],
        }
        entries.extend(
            {
                **catalog_entry,
                "model_id": model_id,
                "basin_id": basin_id,
            }
            for model_id, basin_id in models
        )
        catalogs.append(
            {
                "source_id": source_id,
                "cycle_time": snapshot["cycle_time"],
                "catalog_sha256": _safe_checksum(snapshot["catalog_sha256"]),
                "catalog_row_count": snapshot["catalog_row_count"],
                "forecast_hour_count": len(snapshot["forecast_hours"]),
            }
        )
    model_set_evidence = validate_readiness_registry_model_set(
        entries,
        registry_models,
        sources=normalized_sources,
    )
    return entries, _evidence_safe(
        {
            "status": "ready",
            "derivation": "current_catalog_bound",
            "entry_count": len(entries),
            "model_count": len(models),
            "source_count": len(normalized_sources),
            "catalogs": catalogs,
            "model_set": model_set_evidence,
        }
    )


def validate_catalog_bound_readiness_entries(
    entries: Sequence[Mapping[str, Any]],
    registry_models: Sequence[Mapping[str, Any]],
    *,
    destination_uri: str | Path,
    object_store_root: str | Path,
    object_store_prefix: str,
    sources: Sequence[str] = READINESS_DERIVATION_SOURCES,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate a prospective readiness generation without publishing it."""

    roots = _ProviderRoots(
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        now=generated_at,
    )
    payload = {
        "schema_version": CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
        "generated_at": _format_utc(generated_at or _now(roots)),
        "entries": [dict(entry) for entry in entries],
    }
    payload["checksum"] = _sha256_label(_canonical_json_bytes(payload))
    content = _canonical_json_bytes(payload, pretty=True)
    _entries, evidence = _validate_readiness_index(
        payload,
        content=content,
        index_uri=str(destination_uri),
        roots=roots,
        verify_external_references=True,
        require_catalog_binding=True,
    )
    model_set = validate_readiness_registry_model_set(
        list(_entries.values()),
        registry_models,
        sources=sources,
    )
    return _evidence_safe({**evidence, "model_set": model_set})


def validate_readiness_registry_model_set(
    entries: Sequence[Mapping[str, Any]],
    registry_models: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[str] = READINESS_DERIVATION_SOURCES,
) -> dict[str, Any]:
    """Require one readiness identity per registry model and source."""

    models = set(_registry_readiness_identities(registry_models))
    normalized_sources = tuple(dict.fromkeys(normalize_source_id(str(source)) for source in sources))
    actual_by_source: dict[str, list[tuple[str, str]]] = {source: [] for source in normalized_sources}
    for index, entry in enumerate(entries):
        try:
            source_id = normalize_source_id(str(entry["source_id"]))
            identity = (str(entry["model_id"]), str(entry["basin_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise SchedulerFileProviderError(
                "readiness_registry_identity_invalid",
                field=f"entries[{index}]",
            ) from error
        if source_id not in actual_by_source:
            raise SchedulerFileProviderError(
                "readiness_registry_source_set_mismatch",
                field=f"entries[{index}].source_id",
            )
        actual_by_source[source_id].append(identity)
    for source_id, identities in actual_by_source.items():
        if len(identities) != len(set(identities)) or set(identities) != models:
            raise SchedulerFileProviderError(
                "readiness_registry_model_set_mismatch",
                field="entries[].model_id",
                evidence={
                    "source_id": source_id,
                    "expected_model_count": len(models),
                    "actual_model_count": len(identities),
                    "actual_unique_model_count": len(set(identities)),
                },
            )
    expected_count = len(models) * len(normalized_sources)
    if len(entries) != expected_count:
        raise SchedulerFileProviderError(
            "readiness_registry_entry_count_mismatch",
            field="entries",
            evidence={"expected_entry_count": expected_count, "actual_entry_count": len(entries)},
        )
    return {
        "status": "matched",
        "model_count": len(models),
        "source_entry_counts": {
            source: len(actual_by_source[source]) for source in normalized_sources
        },
    }


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
    enforce_freshness: bool = True,
    verify_external_references: bool = False,
    require_catalog_binding: bool = False,
) -> tuple[dict[tuple[str, str, str, str, str], dict[str, Any]], dict[str, Any]]:
    _require_schema(payload, CANONICAL_READINESS_INDEX_SCHEMA_VERSION, field="schema_version")
    generated_at = _require_fresh_generated_at(
        payload,
        field="generated_at",
        roots=roots,
        enforce_freshness=enforce_freshness,
    )
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
        if require_catalog_binding and not _has_complete_catalog_binding(entry):
            raise SchedulerFileProviderError(
                "readiness_catalog_binding_required",
                field=f"entries[{index}].catalog_uri",
            )
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
        if verify_external_references:
            referenced_products, _reference_evidence = _readiness_entry_products(entry, roots=roots)
            if not referenced_products:
                raise SchedulerFileProviderError(
                    "readiness_renewal_products_missing",
                    field=f"entries[{index}].products",
                )
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


def _registry_readiness_identities(
    registry_models: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    if not registry_models:
        raise SchedulerFileProviderError("readiness_registry_models_empty", field="registry.models")
    if len(registry_models) > MAX_REGISTRY_MODELS:
        raise SchedulerFileProviderError(
            "registry_model_limit_exceeded",
            field="registry.models",
            evidence={"model_count": len(registry_models), "max_models": MAX_REGISTRY_MODELS},
        )
    identities: list[tuple[str, str]] = []
    model_ids: set[str] = set()
    for index, model in enumerate(registry_models):
        if not isinstance(model, Mapping):
            raise SchedulerFileProviderError("registry_model_not_object", field=f"registry.models[{index}]")
        model_id = str(model.get("model_id") or "")
        basin_id = str(model.get("basin_id") or "")
        if not model_id or not basin_id:
            raise SchedulerFileProviderError(
                "readiness_registry_identity_invalid",
                field=f"registry.models[{index}]",
            )
        if model_id in model_ids:
            raise SchedulerFileProviderError(
                "registry_duplicate_model_id",
                field="registry.models[].model_id",
                evidence={"model_id": model_id},
            )
        model_ids.add(model_id)
        identities.append((model_id, basin_id))
    return sorted(identities)


def _latest_canonical_catalog_snapshot(
    *,
    source_id: str,
    roots: _ProviderRoots,
) -> dict[str, Any]:
    object_store_root = roots.object_store_root
    if object_store_root in (None, ""):
        raise SchedulerFileProviderError("canonical_catalog_root_invalid", field="object_store_root")
    store = LocalObjectStore(object_store_root, object_store_prefix=str(roots.object_store_prefix or ""))
    source_root = Path(store.root) / "canonical" / source_id
    try:
        source_metadata = stat_no_follow(source_root, containment_root=Path(store.root))
    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
        raise SchedulerFileProviderError(
            "canonical_catalog_source_missing",
            field="catalog.source_id",
        ) from error
    if not stat.S_ISDIR(source_metadata.st_mode):
        raise SchedulerFileProviderError("canonical_catalog_source_invalid", field="catalog.source_id")
    cycle_names: list[str] = []
    scanned = 0
    try:
        with os.scandir(source_root) as candidates:
            for candidate in candidates:
                scanned += 1
                if scanned > MAX_CANONICAL_CATALOG_CYCLE_DIRS:
                    raise SchedulerFileProviderError(
                        "canonical_catalog_cycle_limit_exceeded",
                        field="catalog.cycles",
                        evidence={"max_cycle_entries": MAX_CANONICAL_CATALOG_CYCLE_DIRS},
                    )
                metadata = candidate.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise SchedulerFileProviderError(
                        "canonical_catalog_scan_unsafe_entry",
                        field="catalog.cycles",
                    )
                if _COMPACT_CYCLE_RE.fullmatch(candidate.name) and stat.S_ISDIR(metadata.st_mode):
                    cycle_names.append(candidate.name)
    except SchedulerFileProviderError:
        raise
    except OSError as error:
        raise SchedulerFileProviderError("canonical_catalog_scan_failed", field="catalog.cycles") from error
    if not cycle_names:
        raise SchedulerFileProviderError("canonical_catalog_cycle_missing", field="catalog.cycles")
    latest_cycle = max(cycle_names)
    try:
        cycle_time = parse_cycle_time(latest_cycle)
    except ValueError as error:
        raise SchedulerFileProviderError("canonical_catalog_cycle_invalid", field="catalog.cycle_time") from error
    catalog_key = f"canonical/{source_id}/{latest_cycle}/_catalog/catalog.json"
    catalog_uri = store.uri_for_key(catalog_key)
    payload, content = _read_json_mapping(
        catalog_uri,
        roots=roots,
        max_bytes=MAX_CANONICAL_PRODUCT_CATALOG_BYTES,
    )
    products_value = payload.get("products")
    if not isinstance(products_value, Sequence) or isinstance(products_value, str | bytes | bytearray):
        raise SchedulerFileProviderError("canonical_product_catalog_products_invalid", field="products")
    if not products_value:
        raise SchedulerFileProviderError("canonical_product_catalog_products_empty", field="products")
    if len(products_value) > MAX_READINESS_PRODUCT_ROWS:
        raise SchedulerFileProviderError(
            "readiness_product_row_limit_exceeded",
            field="products",
            evidence={"product_row_count": len(products_value), "max_product_rows": MAX_READINESS_PRODUCT_ROWS},
        )
    policy_identity = _catalog_uniform_lineage_identity(
        products_value,
        keys=("policy_identity", "source_policy", "canonical_policy_identity"),
        field="products[].lineage_json.policy_identity",
    )
    source_object_identity = _catalog_uniform_lineage_identity(
        products_value,
        keys=("source_object_identity", "source_identity", "object_identity"),
        field="products[].lineage_json.source_object_identity",
    )
    canonical_product_id = f"canon_{source_id.lower()}_{latest_cycle}"
    probe_entry = {
        "source_id": source_id,
        "cycle_time": _format_utc(cycle_time),
        "canonical_product_id": canonical_product_id,
        "policy_identity": policy_identity,
        "source_object_identity": source_object_identity,
    }
    products = _normalize_catalog_products(
        payload,
        source_id=source_id,
        cycle_time=cycle_time,
        entry=probe_entry,
        roots=roots,
    )
    forecast_hours = sorted({int(product["lead_time_hours"]) for product in products})
    readiness = evaluate_canonical_readiness(
        source_id=source_id,
        cycle_time=cycle_time,
        products=products,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
        canonical_product_id=canonical_product_id,
    )
    if not readiness.ready:
        raise SchedulerFileProviderError(
            "canonical_product_catalog_incomplete",
            field="products",
            evidence={
                "candidate_row_count": int(readiness.evidence.get("candidate_row_count") or 0),
                "missing_lead_count": int(readiness.evidence.get("missing_lead_count") or 0),
            },
        )
    return {
        "source_id": source_id,
        "cycle_time": _format_utc(cycle_time),
        "canonical_product_id": canonical_product_id,
        "forecast_hours": forecast_hours,
        "policy_identity": policy_identity,
        "source_object_identity": source_object_identity,
        "catalog_uri": catalog_uri,
        "catalog_sha256": _sha256_label(content),
        "catalog_row_count": len(products),
    }


def _catalog_uniform_lineage_identity(
    products: Sequence[Any],
    *,
    keys: Sequence[str],
    field: str,
) -> dict[str, Any]:
    identities: dict[str, dict[str, Any]] = {}
    for product in products:
        if not isinstance(product, Mapping):
            raise SchedulerFileProviderError("canonical_product_catalog_product_not_object", field="products[]")
        lineage = product.get("lineage_json")
        if not isinstance(lineage, Mapping):
            raise SchedulerFileProviderError("canonical_product_catalog_lineage_invalid", field=field)
        identity: dict[str, Any] = {}
        for key in keys:
            candidate = lineage.get(key)
            if isinstance(candidate, Mapping) and candidate:
                identity = dict(candidate)
                break
        if not identity:
            raise SchedulerFileProviderError("canonical_product_catalog_lineage_missing", field=field)
        identities[_stable_json(identity)] = identity
        if len(identities) > 1:
            raise SchedulerFileProviderError("canonical_product_catalog_lineage_mismatch", field=field)
    return next(iter(identities.values()))


def _normalize_catalog_binding(
    row: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
    field: str,
    roots: _ProviderRoots,
) -> dict[str, Any]:
    values = (row.get("catalog_uri"), row.get("catalog_sha256"), row.get("catalog_row_count"))
    present = tuple(value not in (None, "") for value in values)
    if not any(present):
        return {}
    if not all(present):
        raise SchedulerFileProviderError("readiness_catalog_binding_incomplete", field=f"{field}.catalog_uri")
    catalog_uri = str(values[0])
    catalog_sha256 = str(values[1])
    if not _SHA256_RE.fullmatch(catalog_sha256):
        raise SchedulerFileProviderError("readiness_catalog_checksum_invalid", field=f"{field}.catalog_sha256")
    row_count_value = values[2]
    if isinstance(row_count_value, bool):
        raise SchedulerFileProviderError("readiness_catalog_row_count_invalid", field=f"{field}.catalog_row_count")
    try:
        catalog_row_count = int(row_count_value)
    except (TypeError, ValueError) as error:
        raise SchedulerFileProviderError(
            "readiness_catalog_row_count_invalid",
            field=f"{field}.catalog_row_count",
        ) from error
    if catalog_row_count < 1 or catalog_row_count > MAX_READINESS_PRODUCT_ROWS:
        raise SchedulerFileProviderError("readiness_catalog_row_count_invalid", field=f"{field}.catalog_row_count")
    _require_supported_internal_reference(
        catalog_uri,
        roots=roots,
        field=f"{field}.catalog_uri",
        reason_prefix="readiness_catalog_uri",
    )
    store = _object_store_for(catalog_uri, roots)
    expected_key = f"canonical/{source_id}/{format_cycle_time(cycle_time)}/_catalog/catalog.json"
    try:
        actual_key = store.normalize_key(catalog_uri)
    except (ObjectStoreError, ValueError) as error:
        raise SchedulerFileProviderError("readiness_catalog_uri_unsafe", field=f"{field}.catalog_uri") from error
    if actual_key != expected_key:
        raise SchedulerFileProviderError("readiness_catalog_identity_mismatch", field=f"{field}.catalog_uri")
    return {
        "catalog_uri": catalog_uri,
        "catalog_sha256": f"sha256:{_checksum_value(catalog_sha256)}",
        "catalog_row_count": catalog_row_count,
    }


def _has_complete_catalog_binding(entry: Mapping[str, Any]) -> bool:
    return all(entry.get(field) not in (None, "") for field in ("catalog_uri", "catalog_sha256", "catalog_row_count"))


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
    catalog_binding = _normalize_catalog_binding(
        row,
        source_id=source_id,
        cycle_time=cycle_time,
        field=f"entries[{index}]",
        roots=roots,
    )
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
        **catalog_binding,
        "products": products,
        "product_row_count": len(products),
        "object_count": object_count,
    }


def _readiness_entry_products(
    entry: Mapping[str, Any],
    *,
    roots: _ProviderRoots,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if _has_complete_catalog_binding(entry):
        return _readiness_products_from_catalog(entry, roots=roots)
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
    bound_catalog_uri = entry.get("catalog_uri")
    catalog_uris = (
        [str(bound_catalog_uri)]
        if bound_catalog_uri not in (None, "")
        else _canonical_product_catalog_uris(source_id=source_id, cycle_time=cycle_time, roots=roots)
    )
    missing_uris: list[str] = []
    for catalog_uri in catalog_uris:
        try:
            payload, content = _read_json_mapping(
                catalog_uri,
                roots=roots,
                max_bytes=MAX_CANONICAL_PRODUCT_CATALOG_BYTES,
            )
        except SchedulerFileProviderError as error:
            if error.reason == "file_manifest_missing" and bound_catalog_uri in (None, ""):
                missing_uris.append(_uri_evidence(catalog_uri))
                continue
            raise
        if bound_catalog_uri not in (None, ""):
            expected_sha256 = str(entry.get("catalog_sha256") or "")
            if not _checksum_matches(expected_sha256, sha256_bytes(content)):
                raise SchedulerFileProviderError(
                    "readiness_catalog_checksum_mismatch",
                    field="catalog_sha256",
                )
        products = _normalize_catalog_products(
            payload,
            source_id=source_id,
            cycle_time=cycle_time,
            entry=entry,
            roots=roots,
        )
        if bound_catalog_uri not in (None, "") and len(products) != int(entry.get("catalog_row_count", -1)):
            raise SchedulerFileProviderError(
                "readiness_catalog_row_count_mismatch",
                field="catalog_row_count",
                evidence={
                    "expected_row_count": int(entry.get("catalog_row_count", -1)),
                    "actual_row_count": len(products),
                },
            )
        return products, {
            "status": "ready",
            "source": "catalog",
            "schema_version": CANONICAL_PRODUCT_CATALOG_SCHEMA_VERSION,
            "catalog": _uri_evidence(catalog_uri),
            "catalog_bytes": len(content),
            "catalog_sha256": _safe_checksum(_sha256_label(content)),
            "product_row_count": len(products),
            "binding_verified": bound_catalog_uri not in (None, ""),
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
        max_bytes=MAX_CANONICAL_PRODUCT_OBJECT_BYTES,
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


def _provider_destination_path(uri: str, roots: _ProviderRoots) -> tuple[Path, Path | None]:
    parsed = urlparse(uri)
    if parsed.scheme in {"s3", "published"}:
        store = _object_store_for(uri, roots)
        return store.resolve_path(_object_key_for_uri(uri)), Path(store.root)
    if parsed.scheme:
        raise SchedulerFileProviderError("provider_destination_unsupported", field="destination")
    return Path(uri).expanduser(), None


def capture_scheduler_provider_preimage(
    uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    max_bytes: int = MAX_READINESS_INDEX_BYTES,
) -> ProviderPreimage:
    roots = _ProviderRoots(
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
    )
    path, containment_root = _provider_destination_path(str(uri), roots)
    try:
        return capture_provider_preimage(path, containment_root=containment_root, max_bytes=max_bytes)
    except ProviderAtomicError as error:
        raise SchedulerFileProviderError(error.reason, field="destination") from error


def _write_json_bytes(
    uri: str,
    content: bytes,
    *,
    roots: _ProviderRoots,
    max_bytes: int,
    expected_preimage: ProviderPreimage | Mapping[str, object] | None = None,
) -> ProviderPreimage:
    path, containment_root = _provider_destination_path(uri, roots)
    try:
        return atomic_replace_provider_bytes(
            path,
            content,
            containment_root=containment_root,
            max_bytes=max_bytes,
            expected_preimage=expected_preimage,
        )
    except ProviderAtomicError as error:
        raise SchedulerFileProviderError(error.reason, field="destination", evidence={"phase": error.phase}) from error


def load_canonical_readiness_entries_for_renewal(
    index_uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], ProviderPreimage]:
    roots = _ProviderRoots(
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        now=now,
    )
    path, containment_root = _provider_destination_path(str(index_uri), roots)
    try:
        content, preimage = read_provider_snapshot(
            path,
            containment_root=containment_root,
            max_bytes=MAX_READINESS_INDEX_BYTES,
        )
        payload = json.loads(content.decode("utf-8"))
    except ProviderAtomicError as error:
        raise SchedulerFileProviderError(
            error.reason,
            field="index",
            evidence={"phase": error.phase},
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SchedulerFileProviderError("file_manifest_malformed_json", field="index") from error
    if not isinstance(payload, Mapping):
        raise SchedulerFileProviderError("file_manifest_not_object", field="index")
    payload = dict(payload)
    _validate_json_complexity(payload)
    _entries, evidence = _validate_readiness_index(
        payload,
        content=content,
        index_uri=str(index_uri),
        roots=roots,
        enforce_freshness=False,
        require_catalog_binding=True,
    )
    for entry in _entries.values():
        products, _product_evidence = _readiness_entry_products(entry, roots=roots)
        if not products:
            raise SchedulerFileProviderError("readiness_renewal_products_missing", field="entries[].products")
    return [dict(entry) for entry in _entries.values()], evidence, preimage


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
    cache_key = (parsed.scheme, str(root), prefix or "")
    store = roots.object_store_cache.get(cache_key)
    if store is None:
        cache_limit = max(int(MAX_FILE_PROVIDER_OBJECT_STORE_CACHE_ENTRIES), 1)
        if len(roots.object_store_cache) >= cache_limit:
            roots.object_store_cache.pop(next(iter(roots.object_store_cache)), None)
        store = LocalObjectStore(root, object_store_prefix=prefix or "")
        roots.object_store_cache[cache_key] = store
    return store


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
    if not allow_embedded_checksum:
        try:
            store = _object_store_for(uri, roots)
            digest = hashlib.sha256()
            size_bytes = 0
            for chunk in store.iter_bytes(_object_key_for_uri(uri)):
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise SchedulerFileProviderError(
                        f"{reason_prefix}_size_limit_exceeded",
                        field=field,
                        evidence={"max_bytes": max_bytes},
                    )
                digest.update(chunk)
        except SchedulerFileProviderError:
            raise
        except (OSError, SafeFilesystemError, ObjectStoreError, ValueError) as error:
            raise SchedulerFileProviderError(
                f"{reason_prefix}_unreadable",
                field=field,
                evidence={"error_type": type(error).__name__},
            ) from error
        if not _checksum_matches(expected_checksum, digest.hexdigest()):
            raise SchedulerFileProviderError(f"{reason_prefix}_checksum_mismatch", field=field)
        return
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


def _require_fresh_generated_at(
    payload: Mapping[str, Any],
    *,
    field: str,
    roots: _ProviderRoots,
    enforce_freshness: bool = True,
) -> datetime:
    try:
        generated_at = parse_cycle_time(str(payload.get(field) or ""))
    except (TypeError, ValueError) as error:
        raise SchedulerFileProviderError("file_manifest_generated_at_invalid", field=field) from error
    now = _now(roots)
    max_age = timedelta(hours=max(int(roots.max_age_hours), 1))
    if generated_at > now + timedelta(minutes=5):
        raise SchedulerFileProviderError("file_manifest_generated_at_future", field=field)
    if enforce_freshness and now - generated_at > max_age:
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
