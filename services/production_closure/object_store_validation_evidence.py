from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Sequence

from services.production_closure.object_store_validation_contracts import (
    FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS,
    ProductionObjectStoreValidationError,
)


def _preflight_payload(config: Any) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.preflight.v1",
        "run_id": config.run_id,
        "target": config.target,
        "endpoint": config.endpoint,
        "object_store_root": str(config.object_store_root),
        "object_store_prefix": config.configured_object_store_prefix,
        "operational_object_store_prefix": config.object_store_prefix,
        "credential_source": config.credential_source,
        "cleanup_policy": config.cleanup_policy,
        "copied_basins_root": str(config.basins_root) if config.basins_root else "synthetic-local-fixture",
        "source_uri": config.source_uri,
        "selected_model": config.model_id or "first-valid-model",
        "version": config.version,
        "run_registry_import": config.run_registry_import,
        "registry_database_url_configured": config.registry_database_url is not None,
        "evidence_root": str(config.evidence_root),
    }


def _environment_payload(config: Any) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.environment.v1",
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "run_production_closure": os.getenv("NHMS_RUN_PRODUCTION_CLOSURE", ""),
        "target": config.target,
        "object_store_prefix": config.object_store_prefix,
    }


def _summary(
    config: Any,
    *,
    status: str,
    blockers: list[dict[str, Any]],
    files: list[str],
    selected_model_id: str | None = None,
    version: str | None = None,
    migration_report: dict[str, Any] | None = None,
    package_manifest: dict[str, Any] | None = None,
    consumption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    live_registry_import = consumption.get("live_registry_import") is True if consumption is not None else False
    live_api = consumption.get("live_api") is True if consumption is not None else False
    api_contract_source = str(consumption.get("api_contract_source", "")) if consumption is not None else ""
    live_api_status = "not_executed"
    if consumption is not None and isinstance(consumption.get("api"), dict):
        live_api_status = str(consumption["api"].get("live_api_status", "not_executed"))
    deterministic_fixture = not (live_registry_import and live_api)
    payload: dict[str, Any] = {
        "schema": "nhms.production_closure.object_store.v1",
        "issue": 148,
        "run_id": config.run_id,
        "status": status,
        "evidence_dir": str(config.lane_dir),
        "target": config.target,
        "object_store_prefix": config.object_store_prefix,
        "execution_mode": "live_registry_import_and_live_api"
        if not deterministic_fixture
        else (
            "live_registry_import_with_deterministic_api_contract" if live_registry_import else "deterministic_fixture"
        ),
        "deterministic_fixture": deterministic_fixture,
        "live_registry_import": live_registry_import,
        "live_api": live_api,
        "live_api_status": live_api_status,
        "api_contract_source": api_contract_source or "not_executed",
        "final_production_readiness_claimed": False,
        "blockers": blockers,
        "files": [*files, "summary.json"],
    }
    if selected_model_id is not None:
        payload["model_id"] = selected_model_id
    if version is not None:
        payload["version"] = version
    if migration_report is not None:
        payload["migration_production_ready"] = migration_report.get("production_ready")
        payload["migration_inventory_checksum"] = migration_report.get("inventory_checksum")
    if package_manifest is not None:
        payload["manifest_uri"] = package_manifest.get("manifest_uri")
        payload["model_package_uri"] = package_manifest.get("model_package_uri")
        payload["package_checksum"] = package_manifest.get("package_checksum")
    return payload


def _result_blockers(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    blockers = []
    for payload in payloads:
        if payload.get("status") not in {"ready", "verified"}:
            blockers.append(
                {
                    "error_code": "PRODUCTION_OBJECT_STORE_VALIDATION_BLOCKED",
                    "schema": payload.get("schema"),
                    "status": payload.get("status"),
                }
            )
    return blockers


def _default_model_id(inventory: dict[str, Any]) -> str:
    for model in inventory.get("models", []):
        if isinstance(model, dict) and model.get("status") == "valid" and model.get("default_publish_eligible") is True:
            return str(model["model_id"])
    raise ProductionObjectStoreValidationError(
        "PRODUCTION_OBJECT_STORE_NO_PUBLISHABLE_MODEL",
        "Basins inventory does not contain a valid publishable model.",
    )


def _truthy_env(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _forbidden_runtime_source_fragments(values: Sequence[Any]) -> list[str]:
    found: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        for fragment in FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS:
            if fragment in value:
                found.add(fragment)
    return sorted(found)
