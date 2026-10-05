"""The merged scheduler registry publish is planned, then applied to both manifests (#2738).

Drives ``scripts.node22_publish_merged_scheduler_registry`` over two ``tmp_path``
object stores (the compute store holding the worker mirror, the shared store
holding the canonical manifest and the receipts), fake packages present under
both roots, and the real ``publish_scheduler_registry_manifest``.  Failures of
a publish are injected at the publisher seam by a wrapper that still calls the
real publisher, or by really holding the lock the publisher needs.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest

import scripts.node22_publish_merged_scheduler_registry as tool
from packages.common import provision_succession_receipt, succession_receipt
from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import provider_destination_lock
from services.orchestrator import scheduler_file_providers as providers
from services.orchestrator.scheduler_file_providers import (
    SchedulerFileProviderError,
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


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
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


# --- a successful publish ----------------------------------------------------


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


@pytest.mark.parametrize("case", [_replace_case, _add_case, _remove_case, _mixed_case])
def test_apply_publishes_the_merged_rows_to_both_manifests_with_one_generated_at_and_backs_up_the_previous_bytes(
    workspace: Workspace,
    case: Callable[[Workspace], tuple[Operations, list[dict[str, Any]], list[dict[str, Any]]]],
) -> None:
    operations, provisioned, expected_rows = case(workspace)
    if provisioned:
        workspace.provision("s-1", provisioned)
    previous = workspace.canonical.read_bytes()
    assert workspace.mirror.read_bytes() == previous

    receipt = workspace.plan_then_apply(operations, "s-1")

    published = workspace.canonical.read_bytes()
    assert workspace.mirror.read_bytes() == published
    # Untouched rows are the objects that were there, in the order they had;
    # the whole file is what the publisher writes for exactly these rows.
    assert workspace.models(workspace.canonical) == expected_rows
    generated_at = json.loads(published)["generated_at"]
    assert published == _expected_bytes(workspace, expected_rows, generated_at)
    assert receipt["manifest_generated_at"] == generated_at
    assert datetime.fromisoformat(generated_at.replace("Z", "+00:00")) > SEEDED_AT

    backups = workspace.backups()
    assert [path.read_bytes() for path in backups] == [previous, previous]
    assert {str(path) for path in backups} == {receipt["canonical"]["backup_path"], receipt["mirror"]["backup_path"]}
    stamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%SZ")
    assert Path(receipt["canonical"]["backup_path"]).name == f"manifest-last.json.bak-s-1-{stamp}"
    assert receipt["outcome"] == "published" and receipt["dry_run"] is False
    assert receipt["canonical"]["sha256_after"] == receipt["mirror"]["sha256_after"] == sha256_bytes(published)
    assert receipt["canonical"]["sha256_before"] == receipt["mirror"]["sha256_before"] == sha256_bytes(previous)
    assert (receipt["row_count_before"], receipt["row_count_after"]) == (6, len(expected_rows))
    assert receipt["manifest_bytes"] == len(published)
    assert receipt == workspace.receipt("s-1", "publish-apply.json")
    assert workspace.failed_receipts("s-1") == []


def test_apply_receipt_records_the_replaced_pair_with_old_and_new_basin_version(workspace: Workspace) -> None:
    operations, provisioned, _expected = _replace_case(workspace)
    workspace.provision("s-1", provisioned)
    old_id, new_id = operations.replace[0]

    receipt = workspace.plan_then_apply(operations, "s-1")

    assert receipt["schema_version"] == "nhms.model_succession.publish_receipt.v1"
    assert (receipt["step"], receipt["succession_id"], receipt["provision_succession_id"]) == ("publish", "s-1", "s-1")
    assert receipt["operator_id"] == "operator-1" and receipt["host"] and receipt["generated_at"].endswith("Z")
    assert receipt["operations"] == {
        "replace": [{"old_model_id": old_id, "new_model_id": new_id}],
        "add": [],
        "remove": [],
    }
    assert receipt["introduced_model_ids"] == [new_id] and receipt["removed_model_ids"] == [old_id]
    assert receipt["already_published_model_ids"] == []
    assert receipt["replaced"] == [
        {
            "old_model_id": old_id,
            "new_model_id": new_id,
            "basin_id": "basins_b",
            "source_id": "gfs",
            "old_basin_version_id": "basins_b_v1",
            "new_basin_version_id": "basins_b_v2",
        }
    ]
    provision_receipt = workspace.receipt_root / "s-1" / "provision-apply.json"
    assert receipt["provision_apply_receipt"] == {
        "path": str(provision_receipt),
        "sha256": sha256_bytes(provision_receipt.read_bytes()),
    }
    dry_run_receipt = workspace.receipt_root / "s-1" / "publish-dry-run.json"
    assert receipt["dry_run_receipt"] == {
        "path": str(dry_run_receipt),
        "sha256": sha256_bytes(dry_run_receipt.read_bytes()),
    }
    assert "reason" not in receipt
    assert (receipt["manifest_bytes_limit"], receipt["manifest_json_nodes_limit"]) == (32 * 1024 * 1024, 800_000)


def test_republishing_rows_read_from_a_manifest_reproduces_its_bytes(workspace: Workspace) -> None:
    # The premise of "rows no operation names are published as the same JSON
    # objects": what is read from the canonical file round-trips byte for byte.
    rows = workspace.models(workspace.canonical)

    assert _expected_bytes(workspace, rows, "2026-10-01T00:00:00Z") == workspace.canonical.read_bytes()


# --- dry-run -----------------------------------------------------------------


def test_dry_run_of_a_replace_changes_no_file_other_than_its_receipt_and_reports_the_plan(
    workspace: Workspace,
) -> None:
    operations, provisioned, expected_rows = _replace_case(workspace)
    workspace.provision("s-1", provisioned)
    before = _tree(workspace.root)
    previous = workspace.canonical.read_bytes()

    report = workspace.run(operations, succession_id="s-1")

    assert _changed(before, _tree(workspace.root)) == {
        "shared-store/scheduler/succession/s-1",
        "shared-store/scheduler/succession/s-1/publish-dry-run.json",
    }
    assert report == workspace.receipt("s-1", "publish-dry-run.json")
    assert (report["dry_run"], report["outcome"]) == (True, "planned")
    assert report["introduced_model_ids"] == ["dg_b_gfs_v2"] and report["removed_model_ids"] == ["dg_b_gfs_v1"]
    assert (report["row_count_before"], report["row_count_after"]) == (6, 6)
    assert report["merged_model_ids"] == [row["model_id"] for row in expected_rows]
    assert report["canonical"] == {
        "path": str(workspace.canonical),
        "sha256_before": sha256_bytes(previous),
        "sha256_after": None,
        "backup_path": None,
    }
    assert report["mirror"]["sha256_before"] == sha256_bytes(previous)
    assert report["manifest_generated_at"] is None
    assert report["manifest_bytes_remaining"] == 32 * 1024 * 1024 - report["manifest_bytes"]
    assert report["manifest_json_nodes_remaining"] == 800_000 - report["manifest_json_nodes"]

    # The prediction is what the apply then publishes.
    applied = workspace.run(operations, succession_id="s-1", apply=True)
    assert report["manifest_bytes"] == len(workspace.canonical.read_bytes()) == applied["manifest_bytes"]


def test_dry_run_takes_no_lock_and_creates_nothing_in_either_manifest_directory(workspace: Workspace) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned)

    workspace.run(operations, succession_id="s-1")

    assert sorted(path.name for path in workspace.canonical.parent.iterdir()) == ["manifest-last.json"]
    assert sorted(path.name for path in workspace.mirror.parent.iterdir()) == ["manifest-last.json"]
    assert list(workspace.refresh_lock.parent.iterdir()) == []


def test_dry_run_without_a_succession_id_writes_nothing_at_all(workspace: Workspace) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    before = _tree(workspace.root)

    report = workspace.run(operations)

    assert _changed(before, _tree(workspace.root)) == set()
    assert report["succession_id"] is None and report["row_count_after"] == len(expected_rows) == 4


def test_dry_run_leaves_nothing_in_the_temporary_directory(
    workspace: Workspace, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path_factory.mktemp("scratch")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned)

    workspace.run(operations, succession_id="s-1")

    assert list(scratch.iterdir()) == []


def test_a_temporary_directory_inside_a_manifest_directory_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(workspace.mirror.parent))

    message = _refused(workspace, _remove_case(workspace)[0])

    assert "TMPDIR" in message and str(workspace.mirror.parent) in message


def test_the_predicted_node_count_is_the_one_the_publisher_bounds(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    nodes = workspace.run(operations)["manifest_json_nodes"]

    monkeypatch.setattr(providers, "MAX_REGISTRY_MANIFEST_JSON_NODES", nodes)
    assert workspace.run(operations)["manifest_json_nodes_remaining"] == 0

    monkeypatch.setattr(providers, "MAX_REGISTRY_MANIFEST_JSON_NODES", nodes - 1)
    assert "file_manifest_json_node_limit_exceeded" in _refused(workspace, operations)


def test_a_second_dry_run_with_the_same_id_is_refused_and_keeps_the_receipt(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    message = _refused(workspace, operations, succession_id="s-1")

    assert "publish-dry-run.json already exists" in message and "new --succession-id" in message


# --- refusals: new rows come from a provisioned registry ----------------------


def test_replace_or_add_without_the_provision_apply_receipt_is_refused(workspace: Workspace) -> None:
    operations, _provisioned, _expected = _add_case(workspace)

    message = _refused(workspace, operations, succession_id="s-1")

    assert str(workspace.receipt_root / "s-1" / "provision-apply.json") in message


@pytest.mark.parametrize(
    "override",
    [
        {"dry_run": True},
        {"outcome": "planned"},
        {"step": "publish"},
        {"succession_id": "other"},
        {"schema_version": "x"},
    ],
)
def test_a_receipt_that_is_not_the_provision_apply_of_the_succession_is_refused(
    workspace: Workspace, override: dict[str, Any]
) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned, **override)

    message = _refused(workspace, operations, succession_id="s-1")

    assert "is not the provision apply receipt of succession 's-1'" in message
    assert next(iter(override)) in message


def test_a_new_rows_registry_whose_sha256_differs_from_the_provision_receipt_is_refused(workspace: Workspace) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    registry = workspace.provision("s-1", provisioned)
    registry.write_bytes(registry.read_bytes() + b"\n")

    message = _refused(workspace, operations, succession_id="s-1")

    assert str(registry) in message and "recorded" in message


def test_a_new_rows_registry_that_does_not_match_its_embedded_checksum_is_refused(workspace: Workspace) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    registry = workspace.provision("s-1", provisioned)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["models"][0]["shud_code_version"] = "edited"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    receipt_path = workspace.receipt_root / "s-1" / "provision-apply.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["output_registry"]["sha256"] = sha256_bytes(registry.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert "embedded checksum" in _refused(workspace, operations, succession_id="s-1")


def test_an_introduced_model_id_the_provision_receipt_does_not_list_is_refused(workspace: Workspace) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned, listed=["dg_d_gfs_v1"])

    message = _refused(workspace, operations, succession_id="s-1")

    assert "not in models[] of the provision apply receipt" in message
    assert "['dg_d_IFS_v1']".lower() in message.lower()


def test_a_provisioned_model_id_that_is_neither_introduced_nor_published_is_refused(workspace: Workspace) -> None:
    provisioned = [*workspace.basin("d"), *workspace.basin("e")]
    workspace.provision("s-1", provisioned)
    operations = Operations(add=("dg_d_gfs_v1", "dg_d_ifs_v1"))

    message = _refused(workspace, operations, succession_id="s-1")

    assert "no operation introduces" in message and "['dg_e_gfs_v1', 'dg_e_ifs_v1']" in message


def test_a_batch_in_which_one_provisioned_model_id_is_already_published_is_accepted_and_reported(
    workspace: Workspace,
) -> None:
    successor = workspace.row("b", "gfs", "v2")
    # The provision batch covered both sources of basin b; its IFS variant kept its identity.
    workspace.provision("s-1", [successor, workspace.rows[3]])
    operations = Operations(replace=(("dg_b_gfs_v1", "dg_b_gfs_v2"),))

    receipt = workspace.plan_then_apply(operations, "s-1")

    assert receipt["already_published_model_ids"] == ["dg_b_ifs_v1"]
    assert receipt["introduced_model_ids"] == ["dg_b_gfs_v2"]
    assert [row["model_id"] for row in workspace.models(workspace.canonical)][2:4] == ["dg_b_gfs_v2", "dg_b_ifs_v1"]


def test_a_provision_registry_outside_the_object_store_must_be_named(workspace: Workspace) -> None:
    operations, provisioned, expected_rows = _add_case(workspace)
    registry = workspace.provision("s-1", provisioned, in_store=False)

    assert "--new-rows-registry" in _refused(workspace, operations, succession_id="s-1")

    workspace.plan_then_apply(operations, "s-1", new_rows_registry=registry)
    assert workspace.models(workspace.mirror) == expected_rows


def test_a_publish_succession_can_name_the_provision_of_another_succession(workspace: Workspace) -> None:
    operations, provisioned, expected_rows = _add_case(workspace)
    workspace.provision("provision-1", provisioned)

    receipt = workspace.plan_then_apply(operations, "publish-2", provision_succession_id="provision-1")

    assert (receipt["succession_id"], receipt["provision_succession_id"]) == ("publish-2", "provision-1")
    assert workspace.models(workspace.canonical) == expected_rows


# --- refusals: the checks before any write ------------------------------------


def test_manifests_that_differ_before_the_change_are_refused_pointing_at_the_provider_refresh(
    workspace: Workspace,
) -> None:
    publish_scheduler_registry_manifest(
        workspace.rows,
        workspace.mirror,
        object_store_root=workspace.store,
        object_store_prefix=PREFIX,
        generated_at=SEEDED_AT + timedelta(hours=1),
    )

    message = _refused(workspace, _remove_case(workspace)[0])

    assert "differ before the change" in message and "Run the provider refresh first" in message


def test_no_operation_is_refused(workspace: Workspace) -> None:
    assert "at least one of --replace, --add or --remove" in _refused(workspace, Operations())


@pytest.mark.parametrize(
    ("operations", "named"),
    [
        (
            Operations(remove=("dg_x_gfs_v1", "dg_a_gfs_v1", "dg_a_ifs_v1")),
            "not in the canonical manifest: ['dg_x_gfs_v1']",
        ),
        (Operations(replace=(("dg_x_gfs_v1", "dg_d_gfs_v1"),)), "not in the canonical manifest: ['dg_x_gfs_v1']"),
        (Operations(add=("dg_a_gfs_v1",)), "already in the canonical manifest: ['dg_a_gfs_v1']"),
        (Operations(replace=(("dg_a_gfs_v1", "dg_b_gfs_v1"),)), "already in the canonical manifest: ['dg_b_gfs_v1']"),
        (
            Operations(replace=(("dg_a_gfs_v1", "dg_d_gfs_v1"),), remove=("dg_a_gfs_v1",)),
            "named more than once: ['dg_a_gfs_v1']",
        ),
        (
            Operations(replace=(("dg_a_gfs_v1", "dg_d_gfs_v1"),), add=("dg_d_gfs_v1",)),
            "named more than once: ['dg_d_gfs_v1']",
        ),
        (Operations(remove=("dg_a_gfs_v1", "dg_a_gfs_v1")), "named more than once: ['dg_a_gfs_v1']"),
    ],
)
def test_an_operation_naming_a_wrong_model_id_is_refused_naming_it(
    workspace: Workspace, operations: Operations, named: str
) -> None:
    workspace.provision("s-1", workspace.basin("d"))

    assert named in _refused(workspace, operations, succession_id="s-1")


def test_a_new_model_id_missing_from_the_new_rows_registry_is_refused(workspace: Workspace) -> None:
    new = workspace.basin("d")
    workspace.provision("s-1", new[:1], listed=["dg_d_gfs_v1", "dg_d_ifs_v1"])

    message = _refused(workspace, Operations(add=("dg_d_gfs_v1", "dg_d_ifs_v1")), succession_id="s-1")

    assert "not in the new-rows registry: ['dg_d_ifs_v1']" in message


@pytest.mark.parametrize(
    ("profile", "why"),
    [
        ({"direct_grid_source_id": None}, "has no resource_profile.direct_grid_source_id"),
        (
            {"direct_grid_source_id": "IFS"},
            "has direct_grid_source_id 'IFS' outside its contract's applicable_source_ids ['gfs']",
        ),
        ({"direct_grid_source_id": "nope"}, "has an unknown resource_profile.direct_grid_source_id 'nope'"),
        ({"forcing_mapping_mode": "idw"}, "is not a direct-grid row"),
    ],
)
@pytest.mark.parametrize("where", ["canonical", "new"])
def test_a_row_whose_source_is_missing_or_outside_its_contract_is_refused_naming_the_model(
    workspace: Workspace, profile: dict[str, Any], why: str, where: str
) -> None:
    bad = workspace.row("d" if where == "new" else "a", "gfs", **profile)
    if where == "canonical":
        # Seeded without the direct-grid requirement, as a manifest written by other means could be.
        for path in (workspace.canonical, workspace.mirror):
            publish_scheduler_registry_manifest(
                [bad, *workspace.rows[1:]],
                path,
                object_store_root=workspace.store,
                object_store_prefix=PREFIX,
                generated_at=SEEDED_AT,
            )
            path.with_name(f".{path.name}.lock").unlink()
        operations = Operations(remove=("dg_c_gfs_v1", "dg_c_ifs_v1"))
    else:
        workspace.provision("s-1", [bad, workspace.row("d", "IFS")])
        operations = Operations(add=("dg_d_gfs_v1", "dg_d_ifs_v1"))

    message = _refused(workspace, operations, succession_id="s-1")

    assert f"{bad['model_id']} {why}" in message


@pytest.mark.parametrize(
    ("successor", "named"),
    [
        (("c", "gfs", "v2"), "dg_b_gfs_v1 (basins_b, gfs) -> dg_c_gfs_v2 (basins_c, gfs)"),
        (("b", "IFS", "v2"), "dg_b_gfs_v1 (basins_b, gfs) -> dg_b_ifs_v2 (basins_b, IFS)"),
    ],
)
def test_a_replace_that_changes_basin_or_source_is_refused(
    workspace: Workspace, successor: tuple[str, str, str], named: str
) -> None:
    new = workspace.row(*successor)
    workspace.provision("s-1", [new])

    message = _refused(workspace, Operations(replace=(("dg_b_gfs_v1", new["model_id"]),)), succession_id="s-1")

    assert "a replace must keep basin_id and source" in message and named in message


@pytest.mark.parametrize(
    ("operations", "named"),
    [
        (Operations(add=("dg_d_gfs_v1",)), "basins_d has ['gfs']"),
        (Operations(remove=("dg_a_ifs_v1",)), "basins_a has ['gfs']"),
        (Operations(add=("dg_a_gfs_v2",)), "basins_a has ['IFS', 'gfs', 'gfs']"),
    ],
)
def test_a_merged_manifest_without_exactly_one_row_per_basin_and_source_is_refused(
    workspace: Workspace, operations: Operations, named: str
) -> None:
    workspace.provision("s-1", [workspace.row("d", "gfs"), workspace.row("a", "gfs", "v2")], listed=operations.add)

    message = _refused(workspace, operations, succession_id="s-1")

    assert "exactly one row for each of the sources ['IFS', 'gfs']" in message and named in message


@pytest.mark.parametrize(
    ("root", "damage", "reason"),
    [
        ("store", "missing", "registry_model_package_manifest_missing"),
        ("shared", "missing", "registry_model_package_manifest_missing"),
        ("store", "edited", "registry_model_package_manifest_checksum_mismatch"),
        ("shared", "edited", "registry_model_package_manifest_checksum_mismatch"),
    ],
)
def test_a_new_row_whose_package_is_missing_or_different_under_either_root_is_refused(
    workspace: Workspace, root: str, damage: str, reason: str
) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned)
    manifest = getattr(workspace, root) / "models" / "dg_d_ifs_v1" / "manifest.json"
    if damage == "missing":
        manifest.unlink()
    else:
        manifest.write_bytes(b'{"model_id": "someone-else"}')

    message = _refused(workspace, operations, succession_id="s-1")

    assert reason in message and "model_id=dg_d_ifs_v1" in message
    assert ("OBJECT_STORE_ROOT" if root == "store" else "NHMS_SCHEDULER_PROVIDER_STORE_ROOT") in message


def test_a_merged_manifest_over_the_byte_bound_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations, provisioned, _expected = _add_case(workspace)
    workspace.provision("s-1", provisioned)
    monkeypatch.setattr(providers, "MAX_REGISTRY_MANIFEST_BYTES", len(workspace.canonical.read_bytes()) + 100)

    assert "file_manifest_size_limit_exceeded" in _refused(workspace, operations, succession_id="s-1")


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"canonical_manifest": Path("/elsewhere/manifest-last.json")}, "NHMS_SCHEDULER_PROVIDER_STORE_ROOT"),
        ({"mirror_manifest": Path("/elsewhere/manifest-last.json")}, "OBJECT_STORE_ROOT"),
        ({"object_store_prefix": ""}, "OBJECT_STORE_PREFIX is required"),
        ({"succession_id": "a/b"}, "Invalid --succession-id"),
        ({"succession_id": "s-1", "provision_succession_id": ".."}, "Invalid --succession-id"),
    ],
)
def test_inputs_that_cannot_be_right_are_refused(workspace: Workspace, arguments: dict[str, Any], named: str) -> None:
    assert named in _refused(workspace, _remove_case(workspace)[0], **arguments)


def test_a_manifest_the_publisher_would_refuse_to_replace_is_refused_before_anything_is_written(
    workspace: Workspace,
) -> None:
    os.chmod(workspace.mirror, 0o600)

    message = _refused(workspace, _remove_case(workspace)[0])

    assert f"the mirror manifest {workspace.mirror} must be owned" in message and "mode 0644" in message


# --- apply requires its dry-run ------------------------------------------------


def test_apply_without_a_dry_run_receipt_is_refused_naming_the_expected_path(workspace: Workspace) -> None:
    message = _refused(workspace, _remove_case(workspace)[0], succession_id="s-1", apply=True)

    assert str(workspace.receipt_root / "s-1" / "publish-dry-run.json") in message


def test_apply_without_a_succession_id_is_refused(workspace: Workspace) -> None:
    assert "--apply requires --succession-id" in _refused(workspace, _remove_case(workspace)[0], apply=True)


def test_apply_with_operations_other_than_the_dry_run_recorded_is_refused(workspace: Workspace) -> None:
    workspace.run(Operations(remove=("dg_a_gfs_v1", "dg_a_ifs_v1")), succession_id="s-1")

    message = _refused(
        workspace, Operations(remove=("dg_b_gfs_v1", "dg_b_ifs_v1")), succession_id="s-1", apply=True
    )

    assert "operations differs from the dry-run receipt" in message and "dg_b_gfs_v1" in message


def test_apply_after_the_canonical_models_changed_since_the_dry_run_is_refused(workspace: Workspace) -> None:
    operations = Operations(remove=("dg_a_gfs_v1", "dg_a_ifs_v1"))
    workspace.run(operations, succession_id="s-1")
    # Another succession retired basin c in between: both manifests hold the new generation.
    workspace.seed(workspace.rows[:4], generated_at=SEEDED_AT + timedelta(hours=1))

    message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "canonical_models_sha256_before differs from the dry-run receipt" in message


def test_a_renewal_that_only_changes_generated_at_between_dry_run_and_apply_does_not_block_the_apply(
    workspace: Workspace,
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    planned = workspace.run(operations, succession_id="s-1")
    workspace.seed(workspace.rows, generated_at=SEEDED_AT + timedelta(hours=1))
    assert sha256_bytes(workspace.canonical.read_bytes()) != planned["canonical"]["sha256_before"]

    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert receipt["outcome"] == "published" and workspace.models(workspace.mirror) == expected_rows
    assert receipt["canonical_models_sha256_before"] == planned["canonical_models_sha256_before"]


def test_a_second_apply_of_a_published_succession_is_refused_before_any_write(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.plan_then_apply(operations, "s-1")

    message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "publish-apply.json already exists" in message


def test_apply_while_the_provider_refresh_lock_is_held_is_refused_before_any_backup(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    with provider_destination_lock(workspace.refresh_lock, blocking=False):
        message = _refused(workspace, operations, succession_id="s-1", apply=True)

    assert "provider refresh lock" in message and "provider_already_running" in message
    assert workspace.backups() == [] and workspace.failed_receipts("s-1") == []
    # Released again: the same succession then publishes.
    assert workspace.run(operations, succession_id="s-1", apply=True)["outcome"] == "published"


def test_apply_without_a_configured_provider_refresh_lock_is_refused(workspace: Workspace) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")

    message = _refused(workspace, operations, succession_id="s-1", apply=True, refresh_lock=None)

    assert "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK is required" in message


# --- a failed apply puts back what it committed ---------------------------------


def _publisher(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Workspace,
    *,
    before: Callable[[Path, dict[str, Any]], None] | None = None,
    after: Callable[[Path], None] | None = None,
) -> None:
    """Wrap the real publisher: ``before`` / ``after`` run around a publish to a real manifest only."""

    def wrapped(models: Any, destination: Any, **kwargs: Any) -> dict[str, Any]:
        path = Path(destination)
        real = path in {workspace.canonical, workspace.mirror}
        if real and before is not None:
            before(path, kwargs)
        result = publish_scheduler_registry_manifest(models, destination, **kwargs)
        if real and after is not None:
            after(path)
        return result

    monkeypatch.setattr(tool, "publish_scheduler_registry_manifest", wrapped)


def _another_writer(workspace: Workspace, path: Path) -> bytes:
    """Rewrite ``path`` as a concurrent writer would: the same rows under a later ``generated_at``."""

    publish_scheduler_registry_manifest(
        workspace.rows,
        path,
        object_store_root=workspace.store,
        object_store_prefix=PREFIX,
        generated_at=SEEDED_AT + timedelta(hours=2),
    )
    return path.read_bytes()


def test_a_canonical_manifest_changed_between_read_and_publish_is_refused_and_neither_is_written(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.mirror.read_bytes()
    concurrent: list[bytes] = []

    def change_canonical(path: Path, _kwargs: dict[str, Any]) -> None:
        if path == workspace.canonical:
            concurrent.append(_another_writer(workspace, path))

    _publisher(monkeypatch, workspace, before=change_canonical)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "refused" and "provider_preimage_changed" in error.receipt["reason"]
    # The compare-and-swap kept the other writer's bytes; this run wrote neither manifest.
    assert workspace.canonical.read_bytes() == concurrent[0] != previous
    assert workspace.mirror.read_bytes() == previous
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(concurrent[0])
    assert "changed neither manifest" in str(error)


def test_a_mirror_publish_that_fails_after_the_canonical_write_restores_the_canonical_manifest_and_can_be_retried(
    workspace: Workspace,
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    # Somebody holds the mirror's own destination lock: the canonical publish
    # commits, then the mirror publish is really refused by the atomic writer.
    with provider_destination_lock(workspace.mirror, blocking=False):
        error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "rolled_back" and "provider_already_running" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(previous)
    assert [path.read_bytes() for path in workspace.backups()] == [previous, previous]
    assert "--apply rolled_back" in str(error) and "can be retried" in str(error)

    receipt = workspace.run(operations, succession_id="s-1", apply=True)

    assert receipt["outcome"] == "published" and workspace.models(workspace.canonical) == expected_rows
    assert workspace.mirror.read_bytes() == workspace.canonical.read_bytes()
    assert len(workspace.failed_receipts("s-1")) == 1 and len(workspace.backups()) == 4


def test_a_read_back_that_differs_restores_both_manifests(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    def other_generation(path: Path, kwargs: dict[str, Any]) -> None:
        if path == workspace.mirror:
            kwargs["generated_at"] += timedelta(seconds=1)

    _publisher(monkeypatch, workspace, before=other_generation)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "rolled_back" and "read-back" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()


def test_a_mirror_publish_that_raises_after_its_commit_restores_the_mirror_too(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()
    written: list[bytes] = []

    def raise_after_commit(path: Path) -> None:
        if path == workspace.mirror:
            written.append(path.read_bytes())
            raise RuntimeError("after the mirror commit")

    _publisher(monkeypatch, workspace, after=raise_after_commit)

    error = _apply_failed(workspace, operations, "s-1")

    assert written and written[0] != previous
    assert error.receipt["outcome"] == "rolled_back" and "after the mirror commit" in error.receipt["reason"]
    assert workspace.canonical.read_bytes() == previous == workspace.mirror.read_bytes()


def test_a_canonical_manifest_rewritten_by_someone_else_is_not_overwritten_at_restore_time(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.mirror.read_bytes()
    concurrent: list[bytes] = []

    def rewrite_canonical_then_fail(path: Path, _kwargs: dict[str, Any]) -> None:
        if path == workspace.mirror:
            concurrent.append(_another_writer(workspace, workspace.canonical))
            raise SchedulerFileProviderError("provider_replace_failed", field="destination")

    _publisher(monkeypatch, workspace, before=rewrite_canonical_then_fail)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "inconsistent"
    assert workspace.canonical.read_bytes() == concurrent[0]
    assert workspace.mirror.read_bytes() == previous
    assert error.receipt["canonical"]["sha256_after"] == sha256_bytes(concurrent[0])
    assert error.receipt["mirror"]["sha256_after"] == sha256_bytes(previous)
    backups = [str(path) for path in workspace.backups()]
    assert len(backups) == 2 and all(path in str(error) for path in backups)
    assert sorted(backups) == sorted(error.receipt[name]["backup_path"] for name in ("canonical", "mirror"))
    assert "restore both from this run's backups" in str(error) and "publish the mirror" in str(error)


def test_a_manifest_changed_without_a_commit_this_run_observed_is_left_alone_and_reported_inconsistent(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = _remove_case(workspace)[0]
    workspace.run(operations, succession_id="s-1")
    previous = workspace.canonical.read_bytes()

    def lose_the_commit_token(path: Path, kwargs: dict[str, Any]) -> None:
        # What a failure between the writer's commit and the publisher's return looks like.
        if path == workspace.mirror:
            kwargs["commit_observer"] = lambda _preimage: None

    def fail(path: Path) -> None:
        if path == workspace.mirror:
            raise SchedulerFileProviderError("provider_lock_release_failed", field="destination")

    _publisher(monkeypatch, workspace, before=lose_the_commit_token, after=fail)

    error = _apply_failed(workspace, operations, "s-1")

    assert error.receipt["outcome"] == "inconsistent"
    # The canonical manifest, which this run did commit, is restored; the mirror is never guessed at.
    assert workspace.canonical.read_bytes() == previous
    assert workspace.mirror.read_bytes() != previous
    assert "not by a commit this run observed" in error.receipt["reason"]


def test_a_published_apply_whose_receipt_cannot_be_written_rolls_nothing_back_and_exits_non_zero(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    operations, _provisioned, expected_rows = _remove_case(workspace)
    workspace.run(operations, succession_id="s-1")
    apply_receipt = workspace.receipt_root / "s-1" / "publish-apply.json"

    def occupy_the_receipt(path: Path) -> None:
        if path == workspace.mirror:
            apply_receipt.mkdir()

    _publisher(monkeypatch, workspace, after=occupy_the_receipt)
    _cli_environment(monkeypatch, workspace)

    removes = ["--remove", "dg_a_ifs_v1", "--remove", "dg_a_gfs_v1"]
    status = tool.main([*removes, "--operator-id", "op", "--succession-id", "s-1", "--apply"])

    message = capsys.readouterr().err
    assert status == 1
    assert workspace.models(workspace.canonical) == expected_rows
    published = workspace.canonical.read_bytes()
    assert workspace.mirror.read_bytes() == published
    assert "complete but unreceipted" in message and "Nothing was rolled back" in message
    assert sha256_bytes(published) in message and json.loads(published)["generated_at"] in message
    assert len(workspace.backups()) == 2 and all(str(path) in message for path in workspace.backups())
    assert workspace.failed_receipts("s-1") == []


# --- the command line ------------------------------------------------------------


def _cli_environment(monkeypatch: pytest.MonkeyPatch, workspace: Workspace) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", str(workspace.canonical))
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", str(workspace.mirror))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(workspace.store))
    monkeypatch.setenv("NHMS_SCHEDULER_PROVIDER_STORE_ROOT", str(workspace.shared))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", PREFIX)
    monkeypatch.setenv("NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK", str(workspace.refresh_lock))


def test_the_command_reads_its_environment_says_it_is_a_dry_run_and_then_applies(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)
    new = workspace.basin("d")
    workspace.provision("s-1", new)
    arguments = ["--add", "dg_d_gfs_v1", "--add", "dg_d_ifs_v1", "--operator-id", "op", "--succession-id", "s-1"]

    assert tool.main(arguments) == 0
    output = capsys.readouterr()
    assert output.out.startswith("DRY-RUN (no --apply): neither manifest is written")
    assert json.loads(output.out[output.out.index("{") :])["outcome"] == "planned"
    assert str(workspace.receipt_root / "s-1" / "publish-dry-run.json") in output.err
    assert workspace.models(workspace.canonical) == workspace.rows

    assert tool.main([*arguments, "--apply"]) == 0
    output = capsys.readouterr()
    assert "DRY-RUN" not in output.out and json.loads(output.out)["outcome"] == "published"
    assert workspace.models(workspace.canonical) == workspace.models(workspace.mirror) == [*workspace.rows, *new]


def test_the_command_exits_non_zero_with_the_refusal_on_stderr(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)

    assert tool.main(["--remove", "dg_x_gfs_v1", "--operator-id", "op"]) == 1
    assert "not in the canonical manifest: ['dg_x_gfs_v1']" in capsys.readouterr().err

    monkeypatch.delenv("NHMS_SCHEDULER_PROVIDER_STORE_ROOT")
    assert tool.main(["--remove", "dg_a_gfs_v1", "--operator-id", "op"]) == 1
    assert "NHMS_SCHEDULER_PROVIDER_STORE_ROOT must be set" in capsys.readouterr().err


def test_the_manifest_options_override_the_environment(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_environment(monkeypatch, workspace)
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", "/nowhere/canonical.json")
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", "/nowhere/mirror.json")
    arguments = ["--remove", "dg_a_gfs_v1", "--remove", "dg_a_ifs_v1", "--operator-id", "op"]

    assert tool.main(arguments) == 1
    assert "/nowhere/canonical.json" in capsys.readouterr().err
    assert tool.main(
        [*arguments, "--canonical-manifest", str(workspace.canonical), "--mirror-manifest", str(workspace.mirror)]
    ) == 0


@pytest.mark.parametrize("value", ["old", "old:", ":new", "a:b:c"])
def test_a_replace_value_that_is_not_a_pair_is_a_usage_error(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        tool.main(["--replace", value, "--operator-id", "op"])
    assert raised.value.code == 2


# --- DB-free ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["DATABASE_URL", "PIPELINE_DATABASE_URL", "PGHOST", "PGSERVICE"])
def test_a_database_variable_in_the_environment_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "postgresql://somewhere/nhms")

    message = _refused(workspace, _remove_case(workspace)[0])

    assert "DB-free" in message and name in message and "somewhere" not in message


_DATABASE_DRIVERS = {"psycopg", "psycopg2", "psycopg_pool", "asyncpg", "sqlalchemy", "pg8000"}


def test_the_tool_and_the_shared_receipt_helpers_import_no_database_driver_themselves() -> None:
    for relative in ("scripts/node22_publish_merged_scheduler_registry.py", "packages/common/succession_receipt.py"):
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }
        assert imported & _DATABASE_DRIVERS == set(), relative


def test_importing_the_tool_with_no_database_variable_loads_no_driver_beyond_the_publisher_stack() -> None:
    # The publisher this tool must call lives in ``services.orchestrator``, whose
    # package import already loads psycopg2 (for DSN redaction, not a
    # connection); the tool must add no driver on top of that.
    program = (
        "import sys\n"
        "import services.orchestrator.scheduler_file_providers\n"
        "before = set(sys.modules)\n"
        "import scripts.node22_publish_merged_scheduler_registry\n"
        "print(sorted({name.split('.')[0] for name in set(sys.modules) - before}))\n"
    )
    environment = {name: value for name, value in os.environ.items() if name not in LIBPQ_CONNECTION_ENV_KEYS}
    completed = subprocess.run(
        [sys.executable, "-c", program], cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert set(ast.literal_eval(completed.stdout)) & _DATABASE_DRIVERS == set()


# --- the receipt helpers are shared, not copied ---------------------------------------


def test_the_provision_receipt_module_still_exports_the_shared_helpers() -> None:
    for name in (
        "SuccessionReceiptError",
        "SUCCESSION_ID_PATTERN",
        "DEFAULT_RECEIPT_ROOT_KEY",
        "validate_succession_id",
        "default_receipt_root",
        "file_sha256",
        "object_store_key",
        "path_record",
        "git_commit",
        "prepare_receipt_target",
        "write_receipt",
    ):
        assert getattr(provision_succession_receipt, name) is getattr(succession_receipt, name), name


@pytest.fixture
def restrictive_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def test_receipts_and_backups_stay_readable_by_other_users_under_a_restrictive_umask(
    workspace: Workspace, restrictive_umask: None
) -> None:
    receipt = workspace.plan_then_apply(_remove_case(workspace)[0], "s-1")

    written = [
        workspace.receipt_root / "s-1" / "publish-dry-run.json",
        workspace.receipt_root / "s-1" / "publish-apply.json",
        Path(receipt["canonical"]["backup_path"]),
        Path(receipt["mirror"]["backup_path"]),
        workspace.canonical,
        workspace.mirror,
    ]
    assert [path.stat().st_mode & 0o777 for path in written] == [0o644] * 6
