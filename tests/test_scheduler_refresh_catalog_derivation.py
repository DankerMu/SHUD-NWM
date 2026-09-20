"""Canonical-catalog derivation and the registry/readiness cross-check.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Owns the catalog-bound
readiness derivation: the exact registry model set, direct-grid source scope,
identity recomputation against a mutated catalog, the fail-closed newest-cycle
and symlink/bounded-scan refusals, the hard object-size bound, the
missing-model cross-check and the legacy-entry non-renewal rule.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator import scheduler_file_providers as scheduler_file_providers_module
from services.orchestrator.scheduler_file_providers import (
    FileCanonicalReadinessProvider,
    SchedulerFileProviderError,
    derive_catalog_bound_readiness_entries,
    publish_canonical_readiness_index,
    validate_catalog_bound_readiness_entries,
    validate_readiness_registry_model_set,
)
from workers.canonical_converter.converter import required_standard_variables_for_source


def _write_canonical_catalog(
    object_root: Path,
    *,
    source_id: str,
    cycle: str,
    policy_identity: dict[str, object] | None = None,
    source_object_identity: dict[str, object] | None = None,
) -> tuple[str, dict[str, object], dict[str, object]]:
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    policy = policy_identity or {"source": source_id, "cycle": cycle}
    source_object = source_object_identity or {
        "source": source_id,
        "manifest_object_key": f"raw/{source_id}/{cycle}/manifest.json",
    }
    products = []
    for variable in required_standard_variables_for_source(source_id):
        key = f"canonical/{source_id}/{cycle}/{variable}/f003.dat"
        content = f"{source_id}:{cycle}:{variable}:3".encode()
        store.write_bytes_atomic(key, content)
        products.append(
            {
                "canonical_product_id": f"{source_id}_{cycle}_{variable}_f003",
                "source_id": source_id,
                "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "valid_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T03:00:00Z",
                "lead_time_hours": 3,
                "variable": variable,
                "object_uri": store.uri_for_key(key),
                "checksum": f"sha256:{sha256_bytes(content)}",
                "quality_flag": "ok",
                "lineage_json": {
                    "policy_identity": policy,
                    "source_object_identity": source_object,
                },
            }
        )
    catalog_key = f"canonical/{source_id}/{cycle}/_catalog/catalog.json"
    content = json.dumps(
        {
            "schema_version": "nhms.canonical.product_catalog.v1",
            "source_id": source_id,
            "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
            "products": products,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return store.write_bytes_atomic(catalog_key, content), policy, source_object


def test_catalog_derivation_builds_two_sources_for_exact_registry_model_set(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [
        {"model_id": f"model-{index:02d}", "basin_id": f"basin-{index:02d}"}
        for index in range(13)
    ]

    entries, evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    validation = validate_catalog_bound_readiness_entries(
        entries,
        models,
        destination_uri=tmp_path / "shared/scheduler/canonical-readiness/index-last.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )

    assert len(entries) == 26
    assert evidence["model_set"]["source_entry_counts"] == {"gfs": 13, "IFS": 13}
    assert validation["model_set"]["status"] == "matched"
    assert all(entry["products"] == [] for entry in entries)
    assert all(entry["catalog_uri"].startswith("s3://nhms/canonical/") for entry in entries)
    assert all(entry["catalog_sha256"].startswith("sha256:") for entry in entries)
    assert all(entry["catalog_row_count"] > 0 for entry in entries)


def test_catalog_derivation_respects_direct_grid_source_scope(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    contract_base = {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": "s3://nhms/models/direct/binding.json",
        "binding_checksum": "sha256:binding",
        "model_input_package_id": "direct-input-v1",
        "sp_att_path": "input/basin.sp.att",
        "sp_att_checksum": "sha256:sp-att",
        "grid_id": "grid-demo",
        "grid_signature": "grid-signature-demo",
        "station_bindings": [
            {
                "station_id": "station-1",
                "shud_forcing_index": 1,
                "forcing_filename": "X100Y30.csv",
                "longitude": 100.0,
                "latitude": 30.0,
                "x": 100.0,
                "y": 30.0,
                "z": 10.0,
                "grid_id": "grid-demo",
                "grid_cell_id": "cell-1",
            }
        ],
    }
    models = [
        {
            "model_id": f"model-{source.lower()}",
            "basin_id": "basin-a",
            "resource_profile": {
                "direct_grid_forcing": {
                    **contract_base,
                    "applicable_source_ids": [source],
                }
            },
        }
        for source in ("GFS", "IFS")
    ]

    entries, evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )

    assert {(entry["model_id"], entry["source_id"]) for entry in entries} == {
        ("model-gfs", "gfs"),
        ("model-ifs", "IFS"),
    }
    assert evidence["entry_count"] == 2
    assert evidence["model_count"] == 2
    assert evidence["model_set"]["source_entry_counts"] == {"gfs": 1, "IFS": 1}


def test_catalog_bound_consumer_recomputes_identity_and_detects_catalog_mutation(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    catalog_uri, policy, source_object = _write_canonical_catalog(
        object_root,
        source_id="gfs",
        cycle="2026071400",
    )
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    entries, _evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    for readiness_entry in entries:
        if readiness_entry["source_id"] == "gfs":
            readiness_entry["policy_identity"] = {"source": "gfs", "cycle": "stale"}
            readiness_entry["source_object_identity"] = {"manifest_object_key": "raw/gfs/stale/manifest.json"}
    destination = tmp_path / "shared/scheduler/canonical-readiness/index-last.json"
    publish_canonical_readiness_index(
        entries,
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        verify_external_references=True,
    )
    provider = FileCanonicalReadinessProvider(
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    entry = next(item for item in entries if item["source_id"] == "gfs")
    recomputed = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        forecast_hours=(3,),
        policy_identity=policy,
        source_object_identity=source_object,
        canonical_product_id=str(entry["canonical_product_id"]),
        model_id="model-a",
        basin_id="basin-a",
    )
    assert recomputed["ready"] is True
    assert recomputed["readiness_index"]["entry_status"] == "identity_mismatch_recomputed"

    catalog_path = LocalObjectStore(object_root, "s3://nhms").resolve_path(catalog_uri)
    catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")
    mutated_provider = FileCanonicalReadinessProvider(
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    blocked = mutated_provider.canonical_readiness(
        source_id="gfs",
        cycle_time=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        forecast_hours=(3,),
        policy_identity=policy,
        source_object_identity=source_object,
        canonical_product_id=str(entry["canonical_product_id"]),
        model_id="model-a",
        basin_id="basin-a",
    )
    assert blocked["ready"] is False
    assert blocked["reason"] == "canonical_readiness_index_identity_mismatch"
    assert blocked["readiness_index"]["catalog"]["reason"] == "readiness_catalog_checksum_mismatch"


def test_catalog_derivation_fails_closed_on_invalid_newest_cycle(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071300")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    newest = object_root / "canonical/gfs/2026071400/_catalog"
    newest.mkdir(parents=True)
    (newest / "catalog.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(SchedulerFileProviderError) as error_info:
        derive_catalog_bound_readiness_entries(
            [{"model_id": "model-a", "basin_id": "basin-a"}],
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "file_manifest_malformed_json"


def test_precommit_validation_rejects_catalog_changed_after_derivation(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    catalog_uri, _policy, _source_object = _write_canonical_catalog(
        object_root,
        source_id="gfs",
        cycle="2026071400",
    )
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    entries, _evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    store = LocalObjectStore(object_root, "s3://nhms")
    catalog_path = store.resolve_path(catalog_uri)
    catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")

    with pytest.raises(SchedulerFileProviderError) as error_info:
        validate_catalog_bound_readiness_entries(
            entries,
            models,
            destination_uri=tmp_path / "shared/scheduler/canonical-readiness/index-last.json",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_catalog_checksum_mismatch"


def test_catalog_derivation_rejects_symlink_and_bounded_cycle_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    (object_root / "canonical/gfs/unsafe").symlink_to(object_root / "canonical/gfs/2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    with pytest.raises(SchedulerFileProviderError) as symlink_error:
        derive_catalog_bound_readiness_entries(
            models,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )
    assert symlink_error.value.reason == "canonical_catalog_scan_unsafe_entry"

    (object_root / "canonical/gfs/unsafe").unlink()
    monkeypatch.setattr(scheduler_file_providers_module, "MAX_CANONICAL_CATALOG_CYCLE_DIRS", 1)
    (object_root / "canonical/gfs/grid").mkdir()
    with pytest.raises(SchedulerFileProviderError) as limit_error:
        derive_catalog_bound_readiness_entries(
            models,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )
    assert limit_error.value.reason == "canonical_catalog_cycle_limit_exceeded"


def test_catalog_derivation_streams_objects_under_hard_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    monkeypatch.setattr(scheduler_file_providers_module, "MAX_CANONICAL_PRODUCT_OBJECT_BYTES", 4)

    with pytest.raises(SchedulerFileProviderError) as error_info:
        derive_catalog_bound_readiness_entries(
            [{"model_id": "model-a", "basin_id": "basin-a"}],
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_product_object_size_limit_exceeded"


def test_registry_readiness_cross_check_rejects_missing_model() -> None:
    with pytest.raises(SchedulerFileProviderError) as error_info:
        validate_readiness_registry_model_set(
            [
                {"source_id": source, "model_id": "model-a", "basin_id": "basin-a"}
                for source in ("gfs", "IFS")
            ],
            [
                {"model_id": "model-a", "basin_id": "basin-a"},
                {"model_id": "model-b", "basin_id": "basin-b"},
            ],
        )
    assert error_info.value.reason == "readiness_registry_model_set_mismatch"


def test_legacy_readiness_entries_are_not_renewed(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    destination = tmp_path / "shared/scheduler/canonical-readiness/index-last.json"
    publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": "2026-07-14T00:00:00Z",
                "model_id": "model-a",
                "basin_id": "basin-a",
                "canonical_product_id": "canon_gfs_2026071400",
                "forecast_hours": [3],
                "policy_identity": {"source": "gfs"},
                "source_object_identity": {"manifest": "legacy"},
                "products": [],
            }
        ],
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )

    with pytest.raises(SchedulerFileProviderError) as error_info:
        scheduler_file_providers_module.load_canonical_readiness_entries_for_renewal(
            destination,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_catalog_binding_required"
