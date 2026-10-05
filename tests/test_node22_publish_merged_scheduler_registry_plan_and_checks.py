"""The merged scheduler registry publish is planned, then applied to both manifests (#2738).

Drives ``scripts.node22_publish_merged_scheduler_registry`` through the shared
workspace of ``tests/merged_registry_publish_helpers.py``: a successful
publish, the dry-run, and every refusal decided before anything is written.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import sha256_bytes
from services.orchestrator import scheduler_file_providers as providers
from services.orchestrator.scheduler_file_providers import (
    publish_scheduler_registry_manifest,
)
from tests.merged_registry_publish_helpers import (
    PREFIX,
    SEEDED_AT,
    Operations,
    Workspace,
    _add_case,
    _changed,
    _expected_bytes,
    _mixed_case,
    _refused,
    _remove_case,
    _replace_case,
    _tree,
    no_database,  # noqa: F401  (registers the autouse fixture on this module)
    workspace_fixture,  # noqa: F401  (registers the `workspace` fixture on this module)
)

# --- a successful publish ----------------------------------------------------

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
