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
from packages.scheduler import registry_limits
from scripts import audit_first_cycle_initial_state as first_cycle_audit
from scripts.scheduler_refresh.cutover_declaration import _load_previous_canonical_registry
from services.orchestrator import scheduler_file_providers as providers
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination

GENERATED_AT = datetime(2026, 9, 30, 0, tzinfo=UTC)
MIB = 1024 * 1024
# The bounds the registry had before #2744; a manifest between the old and the
# new bound is exactly what that change exists to admit.
PREVIOUS_MAX_BYTES = 16 * MIB
PREVIOUS_MAX_NODES = 400_000
MAX_BYTES = providers.MAX_REGISTRY_MANIFEST_BYTES
MAX_NODES = providers.MAX_REGISTRY_MANIFEST_JSON_NODES


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


def _model_padded_to(tmp_path: Path, manifest_bytes: int) -> dict[str, Any]:
    """One row whose published manifest is exactly ``manifest_bytes`` long."""
    model = _model(tmp_path)
    model["resource_profile"]["description"] = ""
    initial_bytes = len(providers._canonical_json_bytes(_payload([model]), pretty=True))
    model["resource_profile"]["description"] = "x" * (manifest_bytes - initial_bytes)
    return model


def _mirror_matches(monkeypatch: pytest.MonkeyPatch, control: Path, worker: Path) -> str:
    # Imported here, as the stale-mirror case in tests/test_orchestration_chain.py
    # does: the chain manifest surface is not a module-scope dependency of this suite.
    from services.orchestrator import chain_manifests

    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", str(control))
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", str(worker))
    return chain_manifests._slurm_runtime_scheduler_path(
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        require_generation_match=True,
    )


def _registry(destination: Path, tmp_path: Path) -> providers.FileSchedulerModelRegistry:
    return providers.FileSchedulerModelRegistry(
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=GENERATED_AT,
        require_direct_grid=False,
    )


def test_registry_bounds_are_32_mib_and_800_000_nodes_and_the_other_bounds_are_unchanged() -> None:
    assert providers.MAX_REGISTRY_MANIFEST_BYTES == 33_554_432
    assert providers.MAX_REGISTRY_MANIFEST_JSON_NODES == 800_000
    assert providers.MAX_REGISTRY_MODELS == 500
    assert providers.MAX_READINESS_INDEX_BYTES == 16_777_216
    assert providers.MAX_CANONICAL_PRODUCT_CATALOG_BYTES == 16_777_216
    assert providers.MAX_FILE_PROVIDER_JSON_NODES == 300_000


def test_every_registry_reader_shares_the_one_byte_bound() -> None:
    # Identity, not equality: a module that grows its own literal again -- even
    # one that happens to equal today's value -- is a different object and fails.
    from services.orchestrator import chain_manifests

    shared = registry_limits.MAX_REGISTRY_MANIFEST_BYTES
    assert providers.MAX_REGISTRY_MANIFEST_BYTES is shared
    assert chain_manifests.MAX_REGISTRY_MANIFEST_BYTES is shared
    assert first_cycle_audit.MAX_REGISTRY_MANIFEST_BYTES is shared
    assert providers.MAX_REGISTRY_MANIFEST_JSON_NODES is registry_limits.MAX_REGISTRY_MANIFEST_JSON_NODES


@pytest.mark.parametrize("nodes", [300_001, PREVIOUS_MAX_NODES + 1, MAX_NODES])
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
        model = _model_with_nodes(tmp_path, MAX_NODES + 1)
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
    preimage = providers.capture_scheduler_provider_preimage(destination, max_bytes=MAX_BYTES)
    previous = destination.read_bytes() if has_previous else None
    files_before = set(destination.parent.iterdir())
    commits: list[Any] = []

    with pytest.raises(providers.SchedulerFileProviderError) as error_info:
        _publish(models, destination, tmp_path, expected_preimage=preimage, commit_observer=commits.append)

    assert error_info.value.reason == expected_reason
    if case == "nodes":
        assert error_info.value.evidence == {"max_nodes": 800_000}
    assert commits == []
    assert providers.capture_scheduler_provider_preimage(destination, max_bytes=MAX_BYTES) == preimage
    assert set(destination.parent.iterdir()) == files_before
    assert (destination.read_bytes() if destination.exists() else None) == previous


@pytest.mark.parametrize("case", ["nodes", "depth", "bytes", "models"])
def test_registry_reader_still_rejects_over_capacity(tmp_path: Path, case: str) -> None:
    model = _model(tmp_path)
    if case == "nodes":
        model = _model_with_nodes(tmp_path, MAX_NODES + 1)
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
        assert blocker["max_nodes"] == 800_000


def test_registry_model_count_limit_remains_500(tmp_path: Path) -> None:
    base = _model(tmp_path)
    models = [{**copy.deepcopy(base), "model_id": f"model_{index}"} for index in range(500)]
    destination = _destination(tmp_path)

    assert _publish(models, destination, tmp_path)["model_count"] == 500
    assert _registry(destination, tmp_path).list_models(
        basin_version_id=None, active=True, limit=500, offset=0
    )["total"] == 500


def test_registry_accepts_exact_byte_limit(tmp_path: Path) -> None:
    model = _model_padded_to(tmp_path, MAX_BYTES)
    destination = _destination(tmp_path)

    _publish([model], destination, tmp_path)

    assert destination.stat().st_size == 33_554_432
    assert _registry(destination, tmp_path).list_models(
        basin_version_id=None, active=True, limit=10, offset=0
    )["total"] == 1


def test_registry_one_byte_over_the_limit_is_refused_by_publisher_and_reader(tmp_path: Path) -> None:
    model = _model_padded_to(tmp_path, MAX_BYTES + 1)
    destination = _destination(tmp_path)

    with pytest.raises(providers.SchedulerFileProviderError) as error_info:
        _publish([model], destination, tmp_path)

    assert error_info.value.reason == "file_manifest_size_limit_exceeded"
    assert error_info.value.evidence == {"max_bytes": 33_554_432}
    assert not destination.exists()

    content = providers._canonical_json_bytes(_payload([model]), pretty=True)
    assert len(content) == 33_554_433
    write_provider_destination(destination, content)
    registry = _registry(destination, tmp_path)

    assert registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)["total"] == 0
    assert registry.scheduler_registry_evidence()["blockers"][0]["code"] == "file_manifest_size_limit_exceeded"


def test_manifest_between_the_previous_and_current_byte_bound_is_usable_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published, loaded by the scheduler registry, mirror-compared and audited."""
    manifest_bytes = 20_000_000
    assert PREVIOUS_MAX_BYTES < manifest_bytes < MAX_BYTES
    model = _model_padded_to(tmp_path, manifest_bytes)
    destination = _destination(tmp_path)

    receipt = _publish([model], destination, tmp_path)

    assert receipt["status"] == "published"
    assert destination.stat().st_size == manifest_bytes
    registry = _registry(destination, tmp_path)
    assert registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)["total"] == 1
    assert registry.scheduler_registry_evidence()["status"] == "ready"
    snapshot = _load_previous_canonical_registry(str(destination), containment_root=destination.parent)
    assert snapshot is not None and snapshot[2] == destination.read_bytes()
    assert [row["model_id"] for row in first_cycle_audit.load_registered_models(destination)] == ["model_a"]

    from services.orchestrator.chain_types import OrchestratorError

    mirror = make_directory_with_explicit_mode(tmp_path / "worker") / "registry.json"
    content = destination.read_bytes()
    mirror.write_bytes(content)
    assert _mirror_matches(monkeypatch, destination, mirror) == str(mirror)

    # A mirror that differs only past the previous bound is still a different
    # generation: the comparison must read the whole manifest, not a prefix.
    tail = content.rindex(b"x")
    assert tail > PREVIOUS_MAX_BYTES + 1
    mirror.write_bytes(content[:tail] + b"y" + content[tail + 1 :])
    with pytest.raises(OrchestratorError) as error_info:
        _mirror_matches(monkeypatch, destination, mirror)
    assert error_info.value.error_code == "SCHEDULER_REGISTRY_MIRROR_MISMATCH"
    assert error_info.value.details == {"provider": "registry", "reason": "generation_mismatch"}


def test_first_cycle_audit_refuses_a_registry_one_byte_over_the_shared_bound(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    body = b'{"models":[],"pad":"'
    registry.write_bytes(body + b"x" * (MAX_BYTES - len(body) - 2) + b'"}')
    assert registry.stat().st_size == MAX_BYTES
    assert first_cycle_audit.load_registered_models(registry) == []

    registry.write_bytes(body + b"x" * (MAX_BYTES - len(body) - 1) + b'"}')
    assert registry.stat().st_size == MAX_BYTES + 1
    with pytest.raises(first_cycle_audit.AuditBlocked) as error_info:
        first_cycle_audit.load_registered_models(registry)
    assert error_info.value.reason == "RESOURCE_BOUND_EXCEEDED"


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
