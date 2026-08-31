from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from packages.common.object_store import LocalObjectStore
from services.production_closure.object_store_validation_contracts import (
    FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS,
    ProductionObjectStoreValidationError,
)
from services.production_closure.object_store_validation_runtime import (
    _runtime_staging_evidence,
    _write_validation_scratch_object,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    import_basins_registry,
    prepare_basins_import_sources,
)


def _forbidden_runtime_source_fragments(values: Sequence[Any]) -> list[str]:
    found: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        for fragment in FORBIDDEN_RUNTIME_SOURCE_FRAGMENTS:
            if fragment in value:
                found.add(fragment)
    return sorted(found)


def _consumption_evidence(
    config: Any,
    writer: Any,
    store: LocalObjectStore,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
    stored_verification: dict[str, Any],
) -> dict[str, Any]:
    registry: dict[str, Any]
    try:
        sources = prepare_basins_import_sources(
            inventory_path=inventory_path,
            package_manifest_path=package_manifest_raw_path,
        )
        registry = _registry_import_evidence(config, inventory_path, package_manifest_raw_path, manifest, sources)
    except BasinsRegistryImportError as error:
        registry = {"status": "blocked", **error.to_payload(), "implicit_activation": False}

    runtime = _runtime_staging_evidence(config, store, manifest, stored_verification, writer)
    api_contract_source = (
        "live_registry_import" if registry.get("live_registry_import") is True else "local_import_source"
    )
    api_fixture_model_package_uri = str(registry.get("model_package_uri") or manifest["model_package_uri"])
    api_fixture_manifest_uri = str(registry.get("manifest_uri") or manifest["manifest_uri"])
    api_fixture_package_checksum = str(registry.get("package_checksum") or manifest["package_checksum"])
    api = {
        "status": "local_contract",
        "api_contract_source": api_contract_source,
        "live_api_status": "not_executed",
        "live_api_reason": "fast lane does not require a running API or registry database.",
        "live_api": False,
        "acceptance_evidence": "registry_import_contract_smoke"
        if api_contract_source == "live_registry_import"
        else "local_contract_smoke",
        "model_response_fixture": {
            "model_id": registry.get("model_id") or manifest["model_id"],
            "active": False,
            "model_package_uri": api_fixture_model_package_uri,
            "manifest_uri": api_fixture_manifest_uri,
            "package_checksum": api_fixture_package_checksum,
        },
    }
    runtime_source_values = [
        manifest.get("model_package_uri"),
        manifest.get("manifest_uri"),
        runtime.get("runtime_manifest", {}).get("model_package_uri"),
        runtime.get("runtime_manifest", {}).get("manifest_uri"),
        api["model_response_fixture"]["model_package_uri"],
        api["model_response_fixture"]["manifest_uri"],
        registry.get("model_package_uri"),
        registry.get("manifest_uri"),
    ]
    forbidden = _forbidden_runtime_source_fragments(runtime_source_values)
    prefix_ok = all(
        isinstance(value, str) and value.startswith(config.object_store_prefix.rstrip("/") + "/")
        for value in runtime_source_values
        if value
    )
    consumption_ready = (
        registry.get("status") != "blocked" and runtime.get("status") == "prepared" and prefix_ok and not forbidden
    )
    acceptance_evidence = _consumption_acceptance_evidence(registry)
    return {
        "schema": "nhms.production_closure.object_store.consumption.v1",
        "status": "ready" if consumption_ready else "blocked",
        "registry": registry,
        "api": api,
        "runtime": runtime,
        "object_uri_prefix": config.object_store_prefix,
        "uses_object_uri_prefix": prefix_ok,
        "forbidden_runtime_source_fragments": forbidden,
        "runtime_dev_path_leak": bool(forbidden),
        "implicit_activation": False,
        "live_registry_import": registry.get("live_registry_import") is True,
        "live_api": api["live_api"] is True,
        "api_contract_source": api_contract_source,
        "acceptance_evidence": acceptance_evidence,
        "acceptance_note": _consumption_acceptance_note(registry),
    }


def _registry_import_evidence(
    config: Any,
    inventory_path: Path,
    package_manifest_raw_path: Path,
    manifest: dict[str, Any],
    sources: Any,
) -> dict[str, Any]:
    local_contract = {
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "segment_count": sources.geometry.segment_count,
        "active": False,
        "implicit_activation": False,
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
    }
    if not config.run_registry_import:
        return {
            "status": "local_contract_prepared",
            "db_import_status": "not_executed",
            "db_import_reason": (
                "fast lane does not require PostgreSQL/PostGIS; geometry and manifest contracts were validated locally."
            ),
            "live_registry_import": False,
            "acceptance_evidence": "local_contract_smoke",
            **local_contract,
        }
    if not config.registry_database_url:
        return {
            "status": "blocked",
            "db_import_status": "blocked",
            "error_code": "PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL_MISSING",
            "message": (
                "NHMS_PRODUCTION_OBJECT_STORE_RUN_REGISTRY_IMPORT=1 requires "
                "NHMS_PRODUCTION_OBJECT_STORE_REGISTRY_DATABASE_URL or DATABASE_URL."
            ),
            "live_registry_import": False,
            "acceptance_evidence": "live_registry_import_blocked",
            **local_contract,
        }
    try:
        report = import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=package_manifest_raw_path,
            database_url=config.registry_database_url,
            trusted_internal=True,
        )
    except BasinsRegistryImportError as error:
        return {
            "status": "blocked",
            "db_import_status": "blocked",
            **error.to_payload(),
            "live_registry_import": False,
            "acceptance_evidence": "live_registry_import_blocked",
            **local_contract,
        }
    row_counts = report.get("row_counts") if isinstance(report.get("row_counts"), dict) else {}
    inserted_row_counts = {str(key): int(value) for key, value in row_counts.items() if isinstance(value, int)}
    inserted_total = sum(inserted_row_counts.values())
    return {
        "status": "imported",
        "db_import_status": report.get("status", "imported"),
        "live_registry_import": True,
        "acceptance_evidence": "live_registry_import",
        "registry_import_report": report,
        "inserted_row_counts": inserted_row_counts,
        "inserted_total": inserted_total,
        "updated_row_counts": {},
        "updated_total": 0,
        "idempotent": report.get("status") == "already_imported" or inserted_total == 0,
        **local_contract,
        "active": report.get("active", False),
        "model_package_uri": report.get("model_package_uri", manifest["model_package_uri"]),
        "manifest_uri": report.get("manifest_uri", manifest["manifest_uri"]),
        "package_checksum": report.get("package_checksum", manifest["package_checksum"]),
    }


def _consumption_acceptance_note(registry: dict[str, Any]) -> str:
    if registry.get("live_registry_import") is True:
        return (
            "Live registry DB import evidence ran by explicit opt-in. The API contract smoke is deterministic "
            "and sourced from that live registry import report; live API execution remains explicitly not executed."
        )
    if registry.get("status") == "blocked":
        return (
            "Registry/API/runtime consumption is blocked because live registry import was requested but did not "
            "produce successful DB import evidence."
        )
    return (
        "Default fast validation prepares local registry import sources and proves the API/runtime object-URI "
        "contract locally. Live DB import and live API execution are explicitly not executed in this lane."
    )


def _consumption_acceptance_evidence(registry: dict[str, Any]) -> str:
    if registry.get("live_registry_import") is True:
        return "live_registry_import_contract_smoke"
    if registry.get("status") == "blocked":
        return "live_registry_import_blocked"
    return "local_contract_smoke"


def _cleanup_rollback_evidence(
    config: Any,
    store: LocalObjectStore,
    model_id: str,
) -> dict[str, Any]:
    scratch_prefix = f"runs/{config.run_id}/input/scratch/cleanup-rollback/{model_id}/{config.version}-failed-import"
    partial_key = f"{scratch_prefix}/partial-package.bin"
    created_keys: set[str] = set()
    _write_validation_run_scratch_object(
        config,
        store,
        partial_key,
        b"partial object written before simulated import failure\n",
    )
    created_keys.add(store.normalize_key(partial_key))
    written_keys = [partial_key]
    rows = [
        {
            "table": "core.model_instance",
            "natural_key": f"{model_id}:{config.version}-failed-import",
            "status": "simulated_not_committed",
        }
    ]
    cleanup_status = "retained"
    quarantine_key = None
    if config.cleanup_policy == "delete":
        _delete_validation_run_object(config, store, partial_key, created_keys)
        cleanup_status = "deleted"
    elif config.cleanup_policy == "quarantine":
        quarantine_key = f"runs/{config.run_id}/logs/quarantine/{partial_key}"
        content = store.read_bytes(partial_key)
        _write_validation_run_scratch_object(config, store, quarantine_key, content)
        created_keys.add(store.normalize_key(quarantine_key))
        _delete_validation_run_object(config, store, partial_key, created_keys)
        cleanup_status = "quarantined"
    partial_exists_after = store.exists(partial_key)
    return {
        "schema": "nhms.production_closure.object_store.cleanup_rollback.v1",
        "status": "ready",
        "simulated_failure": {
            "stage": "registry_import",
            "error_code": "SIMULATED_REGISTRY_IMPORT_FAILURE",
            "message": (
                "Synthetic failure after partial object write exercises rollback evidence "
                "without touching a live database."
            ),
        },
        "written_object_keys": written_keys,
        "written_db_rows": rows,
        "cleanup_policy": config.cleanup_policy,
        "cleanup_status": cleanup_status,
        "quarantine_key": quarantine_key,
        "partial_objects_remaining": [partial_key] if partial_exists_after else [],
        "implicit_model_activation": False,
        "active_model_state": "unchanged",
    }


def _write_validation_run_scratch_object(
    config: Any,
    store: LocalObjectStore,
    key: str,
    content: bytes,
) -> str:
    normalized_key = store.normalize_key(key)
    if not _is_validation_run_object(config, normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            f"Validation-created cleanup objects must stay under runs/{config.run_id}/.",
        )
    return _write_validation_scratch_object(store, normalized_key, content)


def _delete_validation_run_object(
    config: Any,
    store: LocalObjectStore,
    key: str,
    created_keys: set[str],
) -> None:
    normalized_key = store.normalize_key(key)
    if not _is_validation_run_object(config, normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            f"Validation cleanup may only delete objects under runs/{config.run_id}/.",
        )
    if normalized_key not in created_keys:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            "Validation cleanup may only delete objects created by the current validation run.",
        )
    store.delete(normalized_key)


def _is_validation_run_object(config: Any, key: str) -> bool:
    return key == f"runs/{config.run_id}" or key.startswith(f"runs/{config.run_id}/")
