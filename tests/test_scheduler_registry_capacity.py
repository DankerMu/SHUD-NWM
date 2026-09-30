"""Registry capacity must be accepted or rejected before provider publication."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common import state_manager
from packages.common.object_store import LocalObjectStore, sha256_bytes
from scripts.scheduler_refresh.cutover_declaration import _load_previous_canonical_registry
from services.orchestrator import scheduler_file_providers as providers
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination

GENERATED_AT = datetime(2026, 9, 30, 0, tzinfo=UTC)


def _model(tmp_path: Path) -> dict[str, Any]:
    store = LocalObjectStore(tmp_path / "objects", "s3://nhms")
    manifest = b'{"model_id":"model_a"}'
    store.write_bytes_atomic("models/model_a/manifest.json", manifest)
    return {
        "model_id": "model_a",
        "basin_id": "basin_a",
        "basin_version_id": "basin_a_v1",
        "river_network_version_id": "basin_a_rivnet_v1",
        "model_package_uri": "s3://nhms/models/model_a/package/",
        "manifest_uri": "s3://nhms/models/model_a/manifest.json",
        "package_checksum": f"sha256:{sha256_bytes(manifest)}",
        "shud_code_version": "2.0",
        "active_flag": True,
        "resource_profile": {"runnable": True, "station_bindings": []},
        "display_capabilities": {"tiles": True},
    }


def _payload(models: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "schema_version": providers.REGISTRY_MANIFEST_SCHEMA_VERSION,
        "generated_at": "2026-09-30T00:00:00Z",
        "models": models,
        "checksum": "",
    }
    payload["checksum"] = f"sha256:{providers._payload_checksum(payload)}"
    return payload


def _model_with_nodes(tmp_path: Path, nodes: int) -> dict[str, Any]:
    model = _model(tmp_path)
    # This fixture has 20 value nodes before the binding values: outer mapping
    # and its four values, model mapping, nine scalar fields, profile mapping
    # with two values, and display mapping with one value.  Keys are not nodes.
    model["resource_profile"]["station_bindings"] = [0] * (nodes - 20)
    return model


def _destination(tmp_path: Path) -> Path:
    parent = make_directory_with_explicit_mode(tmp_path / "providers")
    return parent / "registry.json"


def _publish(models: list[dict[str, Any]], destination: Path, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    return providers.publish_scheduler_registry_manifest(
        models,
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        generated_at=GENERATED_AT,
        require_direct_grid=False,
        **kwargs,
    )


def _registry(destination: Path, tmp_path: Path) -> providers.FileSchedulerModelRegistry:
    return providers.FileSchedulerModelRegistry(
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=GENERATED_AT,
        require_direct_grid=False,
    )


@pytest.mark.parametrize("nodes", [300_001, 400_000])
def test_registry_expanded_node_budget_publishes_and_reads(tmp_path: Path, nodes: int) -> None:
    model = _model_with_nodes(tmp_path, nodes)
    destination = _destination(tmp_path)

    receipt = _publish([model], destination, tmp_path)
    registry = _registry(destination, tmp_path)
    page = registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)

    assert receipt["status"] == "published"
    assert page["total"] == 1
    assert page["items"][0]["resource_profile"]["station_bindings"] == model["resource_profile"]["station_bindings"]
    assert registry.scheduler_registry_evidence()["status"] == "ready"
    assert destination.stat().st_size < providers.MAX_REGISTRY_MANIFEST_BYTES
    registry.refresh()
    assert registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)["total"] == 1
    # Refresh classification/replay reads the canonical snapshot directly;
    # there must be no independent lower node cap on that path.
    snapshot = _load_previous_canonical_registry(str(destination), containment_root=destination.parent)
    assert snapshot is not None
    assert snapshot[1] == [model]
    assert snapshot[2] == destination.read_bytes()


@pytest.mark.parametrize("has_previous", [False, True])
@pytest.mark.parametrize("case", ["nodes", "depth", "bytes", "models"])
def test_registry_publisher_rejects_capacity_before_any_write(
    tmp_path: Path, has_previous: bool, case: str
) -> None:
    model = _model(tmp_path)
    expected_reason = ""
    if case == "nodes":
        model = _model_with_nodes(tmp_path, 400_001)
        expected_reason = "file_manifest_json_node_limit_exceeded"
    elif case == "depth":
        nested: Any = 0
        for _ in range(providers.MAX_FILE_PROVIDER_JSON_DEPTH - 4):
            nested = [nested]
        model["resource_profile"]["nested"] = nested
        expected_reason = "file_manifest_json_depth_exceeded"
    elif case == "bytes":
        model["resource_profile"]["description"] = "x" * providers.MAX_REGISTRY_MANIFEST_BYTES
        expected_reason = "file_manifest_size_limit_exceeded"
    models = [model]
    if case == "models":
        models = [model] * (providers.MAX_REGISTRY_MODELS + 1)
        expected_reason = "registry_model_limit_exceeded"
    destination = _destination(tmp_path)
    if has_previous:
        _publish([], destination, tmp_path)
    preimage = providers.capture_scheduler_provider_preimage(destination)
    previous = destination.read_bytes() if has_previous else None
    files_before = set(destination.parent.iterdir())
    commits: list[Any] = []

    with pytest.raises(providers.SchedulerFileProviderError) as error_info:
        _publish(models, destination, tmp_path, expected_preimage=preimage, commit_observer=commits.append)

    assert error_info.value.reason == expected_reason
    if case == "nodes":
        assert error_info.value.evidence == {"max_nodes": 400_000}
    assert commits == []
    assert providers.capture_scheduler_provider_preimage(destination) == preimage
    assert set(destination.parent.iterdir()) == files_before
    assert (destination.read_bytes() if destination.exists() else None) == previous


@pytest.mark.parametrize("case", ["nodes", "depth", "bytes", "models"])
def test_registry_reader_still_rejects_over_capacity(tmp_path: Path, case: str) -> None:
    model = _model(tmp_path)
    if case == "nodes":
        model = _model_with_nodes(tmp_path, 400_001)
        reason = "file_manifest_json_node_limit_exceeded"
    elif case == "depth":
        nested: Any = 0
        for _ in range(providers.MAX_FILE_PROVIDER_JSON_DEPTH - 4):
            nested = [nested]
        model["resource_profile"]["nested"] = nested
        reason = "file_manifest_json_depth_exceeded"
    elif case == "bytes":
        model["resource_profile"]["description"] = "x" * providers.MAX_REGISTRY_MANIFEST_BYTES
        reason = "file_manifest_size_limit_exceeded"
    else:
        reason = "registry_model_limit_exceeded"
    models = [model] * (providers.MAX_REGISTRY_MODELS + 1) if case == "models" else [model]
    destination = _destination(tmp_path)
    write_provider_destination(destination, providers._canonical_json_bytes(_payload(models), pretty=True))
    registry = _registry(destination, tmp_path)

    assert registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)["total"] == 0
    blocker = registry.scheduler_registry_evidence()["blockers"][0]
    assert blocker["code"] == reason
    if case == "nodes":
        assert blocker["max_nodes"] == 400_000


def test_registry_model_count_limit_remains_500(tmp_path: Path) -> None:
    base = _model(tmp_path)
    models = [{**copy.deepcopy(base), "model_id": f"model_{index}"} for index in range(500)]
    destination = _destination(tmp_path)

    assert _publish(models, destination, tmp_path)["model_count"] == 500
    assert _registry(destination, tmp_path).list_models(
        basin_version_id=None, active=True, limit=500, offset=0
    )["total"] == 500


def test_registry_accepts_exact_byte_limit(tmp_path: Path) -> None:
    model = _model(tmp_path)
    model["resource_profile"]["description"] = ""
    initial_bytes = len(providers._canonical_json_bytes(_payload([model]), pretty=True))
    model["resource_profile"]["description"] = "x" * (providers.MAX_REGISTRY_MANIFEST_BYTES - initial_bytes)
    destination = _destination(tmp_path)

    _publish([model], destination, tmp_path)

    assert destination.stat().st_size == providers.MAX_REGISTRY_MANIFEST_BYTES
    assert _registry(destination, tmp_path).list_models(
        basin_version_id=None, active=True, limit=10, offset=0
    )["total"] == 1


def test_registry_accepts_exact_depth_limit(tmp_path: Path) -> None:
    model = _model(tmp_path)
    nested: Any = 0
    # root -> models -> model -> resource_profile -> nested starts at depth 5.
    for _ in range(providers.MAX_FILE_PROVIDER_JSON_DEPTH - 5):
        nested = [nested]
    model["resource_profile"]["nested"] = nested
    destination = _destination(tmp_path)

    _publish([model], destination, tmp_path)

    assert _registry(destination, tmp_path).list_models(
        basin_version_id=None, active=True, limit=10, offset=0
    )["total"] == 1


@pytest.mark.parametrize("nodes", [300_000, 300_001])
def test_non_registry_json_reader_retains_original_node_budget(tmp_path: Path, nodes: int) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"values": [0] * (nodes - 2)}), encoding="utf-8")
    if nodes == 300_000:
        providers._read_json_mapping(
            str(path), roots=providers._ProviderRoots(), max_bytes=providers.MAX_CANONICAL_PRODUCT_CATALOG_BYTES
        )
    else:
        with pytest.raises(providers.SchedulerFileProviderError) as error_info:
            providers._read_json_mapping(
                str(path), roots=providers._ProviderRoots(), max_bytes=providers.MAX_CANONICAL_PRODUCT_CATALOG_BYTES
            )
        assert error_info.value.reason == "file_manifest_json_node_limit_exceeded"
        assert error_info.value.evidence == {"max_nodes": 300_000}


def test_readiness_provider_and_renewal_retain_original_node_budget(tmp_path: Path) -> None:
    path = tmp_path / "readiness.json"
    write_provider_destination(path, json.dumps({"values": [0] * 300_000}))
    readiness = providers.FileCanonicalReadinessProvider(path, now=GENERATED_AT)
    readiness._load_once()

    assert readiness._evidence["blockers"][0]["code"] == "file_manifest_json_node_limit_exceeded"
    assert readiness._evidence["blockers"][0]["max_nodes"] == 300_000
    with pytest.raises(providers.SchedulerFileProviderError) as error_info:
        providers.load_canonical_readiness_entries_for_renewal(path, now=GENERATED_AT)
    assert error_info.value.evidence == {"max_nodes": 300_000}


@pytest.mark.parametrize("nodes", [300_000, 300_001])
def test_state_index_retains_original_node_budget(nodes: int) -> None:
    payload = {"values": [0] * (nodes - 2)}
    if nodes == 300_000:
        assert state_manager._validate_state_index_json_complexity(payload) == nodes
    else:
        with pytest.raises(state_manager.StateManagerError) as error_info:
            state_manager._validate_state_index_json_complexity(payload)
        assert str(error_info.value) == "state_snapshot_index_json_node_limit_exceeded"
        assert error_info.value.evidence == {"max_nodes": 300_000}
