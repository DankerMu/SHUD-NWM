"""Fixtures and builders shared by the merged scheduler registry publish suites (#2738).

Two ``tmp_path`` object stores (the compute store holding the worker mirror,
the shared store holding the canonical manifest and the receipts), fake
packages present under both roots, and the real
``publish_scheduler_registry_manifest``.  A suite that uses the ``workspace``
fixture imports it and the autouse ``no_database`` fixture from here.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.node22_publish_merged_scheduler_registry as tool
from packages.common import succession_receipt
from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.object_store import LocalObjectStore, sha256_bytes
from services.orchestrator.scheduler_file_providers import (
    publish_scheduler_registry_manifest,
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFIX = "s3://nhms-test"
SEEDED_AT = datetime(2026, 10, 1, tzinfo=UTC)
SOURCES = ("gfs", "IFS")
Operations = tool.Operations


class Clock:
    """Every reading is seven seconds after the previous one, so two applies never share a stamp."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=7)
        return self.now


@dataclasses.dataclass
class Workspace:
    root: Path
    store: Path  # OBJECT_STORE_ROOT: the compute store, holds the worker mirror
    shared: Path  # NHMS_SCHEDULER_PROVIDER_STORE_ROOT: holds the canonical manifest and the receipts
    canonical: Path
    mirror: Path
    refresh_lock: Path
    rows: list[dict[str, Any]]
    clock: Clock

    @property
    def receipt_root(self) -> Path:
        return self.shared / "scheduler" / "succession"

    def receipt(self, succession_id: str, name: str) -> dict[str, Any]:
        return json.loads((self.receipt_root / succession_id / name).read_text(encoding="utf-8"))

    def failed_receipts(self, succession_id: str) -> list[Path]:
        return sorted((self.receipt_root / succession_id).glob("publish-apply-failed-*.json"))

    def backups(self) -> list[Path]:
        return sorted([*self.canonical.parent.glob("*.bak-*"), *self.mirror.parent.glob("*.bak-*")])

    def models(self, path: Path) -> list[dict[str, Any]]:
        return json.loads(path.read_text(encoding="utf-8"))["models"]

    def row(self, basin: str, source: str, version: str = "v1", **profile: Any) -> dict[str, Any]:
        """A direct-grid registry row whose fake package manifest is present under both roots."""

        model_id = f"dg_{basin}_{source.lower()}_{version}"
        manifest = json.dumps({"model_id": model_id}).encode("utf-8")
        for root in (self.store, self.shared):
            LocalObjectStore(root, PREFIX).write_bytes_atomic(f"models/{model_id}/manifest.json", manifest)
        return {
            "model_id": model_id,
            "basin_id": f"basins_{basin}",
            "basin_version_id": f"basins_{basin}_{version}",
            "river_network_version_id": f"basins_{basin}_rivnet_{version}",
            "model_package_uri": f"{PREFIX}/models/{model_id}/package/",
            "manifest_uri": f"{PREFIX}/models/{model_id}/manifest.json",
            "package_checksum": f"sha256:{sha256_bytes(manifest)}",
            "shud_code_version": "2.0",
            "active_flag": True,
            "lifecycle_state": "active",
            "display_name": f"流域 {basin}",
            "resource_profile": {
                "forcing_mapping_mode": "direct_grid",
                "direct_grid_source_id": source,
                "mesh_area_km2": 1234.5,
                "direct_grid_forcing": {
                    "forcing_mapping_mode": "direct_grid",
                    "binding_uri": f"{PREFIX}/models/{model_id}/package/direct_grid_binding.json",
                    "binding_checksum": "sha256:binding",
                    "model_input_package_id": f"dg-input-{model_id}",
                    "sp_att_path": "input/basin.sp.att",
                    "sp_att_checksum": "sha256:sp-att",
                    "grid_id": "grid-demo",
                    "grid_signature": "grid-signature-demo",
                    "applicable_source_ids": [source],
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
                },
                **profile,
            },
            "display_capabilities": {"tiles": True},
        }

    def basin(self, basin: str, version: str = "v1") -> list[dict[str, Any]]:
        return [self.row(basin, source, version) for source in SOURCES]

    def seed(self, rows: list[dict[str, Any]], *, generated_at: datetime = SEEDED_AT) -> None:
        """Publish ``rows`` to both manifests as one generation, leaving no lock file behind."""

        self.rows = rows
        for path in (self.canonical, self.mirror):
            publish_scheduler_registry_manifest(
                rows,
                path,
                object_store_root=self.store,
                object_store_prefix=PREFIX,
                require_direct_grid=True,
                generated_at=generated_at,
            )
            path.with_name(f".{path.name}.lock").unlink()

    def provision(
        self,
        succession_id: str,
        rows: list[dict[str, Any]],
        /,
        *,
        in_store: bool = True,
        listed: list[str] | None = None,
        **overrides: Any,
    ) -> Path:
        """Write what the provision ``--apply`` leaves: its registry and ``provision-apply.json``."""

        directory = (self.shared if in_store else self.root / "elsewhere") / "candidates" / succession_id
        registry = make_directory_with_explicit_mode(directory) / "direct-grid-registry.json"
        publish_scheduler_registry_manifest(
            rows, registry, object_store_root=self.shared, object_store_prefix=PREFIX, generated_at=SEEDED_AT
        )
        receipt = {
            "schema_version": "nhms.model_succession.provision_receipt.v1",
            "succession_id": succession_id,
            "step": "provision",
            "dry_run": False,
            "outcome": "applied",
            "output_registry": {
                "path": f"/home/ghdc/nwm/object-store/candidates/{succession_id}/direct-grid-registry.json",
                "object_store_key": f"candidates/{succession_id}/direct-grid-registry.json" if in_store else None,
                "sha256": sha256_bytes(registry.read_bytes()),
            },
            "models": [
                {"model_id": model_id} for model_id in (listed or [str(row["model_id"]) for row in rows])
            ],
            **overrides,
        }
        target = self.receipt_root / succession_id / "provision-apply.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(receipt), encoding="utf-8")
        return registry

    def run(self, operations: Operations, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "canonical_manifest": self.canonical,
            "mirror_manifest": self.mirror,
            "object_store_root": self.store,
            "provider_store_root": self.shared,
            "object_store_prefix": PREFIX,
            "operations": operations,
            "operator_id": "operator-1",
            "refresh_lock": self.refresh_lock,
            "clock": self.clock,
            **overrides,
        }
        return tool.publish_merged_scheduler_registry(**arguments)

    def plan_then_apply(self, operations: Operations, succession_id: str, **overrides: Any) -> dict[str, Any]:
        self.run(operations, succession_id=succession_id, **overrides)
        return self.run(operations, succession_id=succession_id, apply=True, **overrides)


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIBPQ_CONNECTION_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(psycopg2, "connect", lambda *_args, **_kwargs: pytest.fail("opened a database connection"))


@pytest.fixture(name="workspace")
def workspace_fixture(tmp_path: Path) -> Workspace:
    store, shared = tmp_path / "compute-store", tmp_path / "shared-store"
    canonical = shared / "scheduler" / "registry" / "manifest-last.json"
    mirror = store / "scheduler" / "registry" / "manifest-last.json"
    for directory in (canonical.parent, mirror.parent, tmp_path / "provider-refresh"):
        make_directory_with_explicit_mode(directory)
    ws = Workspace(
        root=tmp_path,
        store=store,
        shared=shared,
        canonical=canonical,
        mirror=mirror,
        refresh_lock=tmp_path / "provider-refresh" / "refresh",
        rows=[],
        clock=Clock(),
    )
    ws.seed([*ws.basin("a"), *ws.basin("b"), *ws.basin("c")])
    return ws


def _tree(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under ``root`` with its mode, mtime and size."""

    entries: dict[str, tuple[int, int, int]] = {}
    for directory, _names, files in os.walk(root):
        for path in (Path(directory), *(Path(directory) / name for name in files)):
            stat = path.lstat()
            entries[str(path.relative_to(root))] = (stat.st_mode, stat.st_mtime_ns, stat.st_size)
    return entries


def _changed(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def _refused(workspace: Workspace, operations: Operations, **overrides: Any) -> str:
    """Run, expect a refusal that changed no file at all, and return its message.

    An apply first takes the provider refresh lock, which creates the lock file
    beside ``refresh_lock`` exactly as the refresh runner does; that file, in a
    directory holding nothing else, is the one thing an apply refusal may add.
    """

    before = _tree(workspace.root)
    with pytest.raises((tool.MergedRegistryPublishError, succession_receipt.SuccessionReceiptError)) as raised:
        workspace.run(operations, **overrides)
    lock_file = {"provider-refresh", "provider-refresh/.refresh.lock"} if overrides.get("apply") else set()
    assert _changed(before, _tree(workspace.root)) - lock_file == set()
    return str(raised.value)


def _apply_failed(workspace: Workspace, operations: Operations, succession_id: str) -> tool.MergedRegistryPublishError:
    with pytest.raises(tool.MergedRegistryPublishError) as raised:
        workspace.run(operations, succession_id=succession_id, apply=True)
    assert not (workspace.receipt_root / succession_id / "publish-apply.json").exists()
    (failed,) = workspace.failed_receipts(succession_id)
    assert raised.value.receipt_path == failed
    assert json.loads(failed.read_text(encoding="utf-8")) == raised.value.receipt
    return raised.value


def _expected_bytes(workspace: Workspace, rows: list[dict[str, Any]], generated_at: str) -> bytes:
    """What the real publisher writes for ``rows`` at ``generated_at``, published by the test itself."""

    target = make_directory_with_explicit_mode(workspace.root / "expected") / "manifest.json"
    publish_scheduler_registry_manifest(
        rows,
        target,
        object_store_root=workspace.store,
        object_store_prefix=PREFIX,
        generated_at=datetime.fromisoformat(generated_at.replace("Z", "+00:00")),
    )
    return target.read_bytes()


# --- the operations the suites publish ------------------------------------------


def _replace_case(ws: Workspace) -> tuple[Operations, list[dict[str, Any]], list[dict[str, Any]]]:
    new = ws.row("b", "gfs", "v2")
    rows = ws.rows
    return Operations(replace=((rows[2]["model_id"], new["model_id"]),)), [new], [*rows[:2], new, *rows[3:]]


def _add_case(ws: Workspace) -> tuple[Operations, list[dict[str, Any]], list[dict[str, Any]]]:
    new = ws.basin("d")
    return Operations(add=tuple(row["model_id"] for row in new)), new, [*ws.rows, *new]


def _remove_case(ws: Workspace) -> tuple[Operations, list[dict[str, Any]], list[dict[str, Any]]]:
    rows = ws.rows
    return Operations(remove=(rows[1]["model_id"], rows[0]["model_id"])), [], rows[2:]


def _mixed_case(ws: Workspace) -> tuple[Operations, list[dict[str, Any]], list[dict[str, Any]]]:
    rows = ws.rows
    successor, added = ws.row("c", "IFS", "v2"), ws.basin("d")
    operations = Operations(
        replace=((rows[5]["model_id"], successor["model_id"]),),
        # Added rows are appended in the order given, here IFS before gfs.
        add=(added[1]["model_id"], added[0]["model_id"]),
        remove=(rows[0]["model_id"], rows[1]["model_id"]),
    )
    return operations, [successor, *added], [*rows[2:5], successor, added[1], added[0]]


# --- the environment the command reads ---------------------------------------------


def _cli_environment(monkeypatch: pytest.MonkeyPatch, workspace: Workspace) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", str(workspace.canonical))
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", str(workspace.mirror))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(workspace.store))
    monkeypatch.setenv("NHMS_SCHEDULER_PROVIDER_STORE_ROOT", str(workspace.shared))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", PREFIX)
    monkeypatch.setenv("NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK", str(workspace.refresh_lock))
