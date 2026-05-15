from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from apps.api.errors import ApiError
from apps.api.routes.pipeline import _local_log_path
from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import StateManager, StateSnapshot
from workers.forcing_producer.producer import ForcingProducer, ForcingProducerConfig

INFRA_SBATCH_TEMPLATES = (
    "produce_forcing_array.sbatch",
    "run_shud_forecast_array.sbatch",
    "parse_output_array.sbatch",
    "compute_frequency_array.sbatch",
    "publish_tiles.sbatch",
    "download_source_cycle.sbatch",
    "convert_canonical.sbatch",
    "hindcast.sbatch",
)


class FakeStateSnapshotRepository:
    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        return None

    def get_state_snapshot_by_model_time(self, **_: Any) -> StateSnapshot | None:
        return None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        return snapshot

    def set_usable_flag(self, **_: Any) -> StateSnapshot | None:
        return None

    def get_latest_usable_state(self, **_: Any) -> StateSnapshot | None:
        return None

    def list_state_snapshots(self, **_: Any) -> dict[str, Any]:
        return {"items": [], "total": 0}

    def insert_qc_result(self, record: dict[str, Any]) -> dict[str, Any]:
        return record


def test_object_store_uses_object_store_root_not_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    store = LocalObjectStore(object_store_root, "s3://nhms")

    uri = store.write_bytes_atomic("runs/run_001/logs/job.log", b"log-content")

    assert uri == "s3://nhms/runs/run_001/logs/job.log"
    assert (object_store_root / "runs" / "run_001" / "logs" / "job.log").read_bytes() == b"log-content"
    assert not (workspace_root / "runs" / "run_001" / "logs" / "job.log").exists()


def test_object_store_accepts_matching_s3_prefix_and_bare_keys(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "s3://bucket/prefix")

    assert store.normalize_key("s3://bucket/prefix/raw/gfs/2026050700/manifest.json") == (
        "raw/gfs/2026050700/manifest.json"
    )
    assert store.normalize_key("raw/gfs/2026050700/manifest.json") == "raw/gfs/2026050700/manifest.json"


@pytest.mark.parametrize(
    "uri",
    [
        "s3://other/prefix/raw/gfs/2026050700/manifest.json",
        "s3://bucket/prefix-other/raw/gfs/2026050700/manifest.json",
    ],
)
def test_object_store_rejects_mismatched_s3_prefix(tmp_path: Path, uri: str) -> None:
    store = LocalObjectStore(tmp_path, "s3://bucket/prefix")

    with pytest.raises(ValueError, match="configured object store prefix|bucket does not match"):
        store.normalize_key(uri)


def test_object_store_rejects_path_traversal_in_s3_uri(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "s3://bucket/prefix")

    with pytest.raises(ValueError, match="must not contain '..'"):
        store.normalize_key("s3://bucket/prefix/raw/gfs/%2E%2E/manifest.json")


def test_split_root_forcing_uses_object_store_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    config = ForcingProducerConfig(workspace_root=workspace_root, object_store_root=object_store_root)

    producer = ForcingProducer(config=config)

    assert producer.object_store.root == object_store_root.resolve()


@pytest.mark.parametrize("template_name", INFRA_SBATCH_TEMPLATES)
def test_real_templates_export_object_store_root(template_name: str) -> None:
    template_path = Path("infra/sbatch") / template_name
    context = {
        "stage_name": "test_stage",
        "partition": "compute",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "memory_gb": 1,
        "walltime": "00:10:00",
        "workspace_dir": "/tmp/nhms-workspace",
        "run_id": "run_001",
        "model_id": "model_001",
        "source_id": "GFS",
        "cycle_id": "gfs_2026050100",
        "cycle_time": "2026050100",
        "job_type": "test",
        "manifest_index_path": "/tmp/manifest.json",
        "max_concurrent": 1,
        "shud_threads": 1,
    }

    rendered = (
        SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        .from_string(template_path.read_text(encoding="utf-8"))
        .render(**context)
    )

    assert 'export OBJECT_STORE_ROOT="/tmp/nhms-workspace"' in rendered
    assert 'export OBJECT_STORE_PREFIX=""' in rendered


def test_state_manager_uses_object_store_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store_root))
    monkeypatch.setattr(
        "packages.common.state_manager.PsycopgStateSnapshotRepository.from_env",
        lambda: FakeStateSnapshotRepository(),
    )

    manager = StateManager.from_env()

    assert manager.object_store.root == object_store_root.resolve()


def test_log_path_rejects_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    secret_path = tmp_path / "secret.log"
    secret_path.write_text("secret", encoding="utf-8")
    (tmp_path / "job.log").symlink_to(secret_path)

    with pytest.raises(ApiError) as error:
        _local_log_path("job.log")

    assert error.value.status_code == 403
    assert error.value.code == "FORBIDDEN"


def test_log_path_rejects_traversal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))

    with pytest.raises(ApiError) as error:
        _local_log_path("../../../etc/passwd")

    assert error.value.status_code == 403
    assert error.value.code == "FORBIDDEN"
