"""Model succession on node-22: the copyback, the checks before any step, the dry-run and the command (#2739).

The full apply, the step order, resume and hard stops are in
``tests/test_node22_model_succession_apply_and_resume.py``; the timer, the
begin wait, the refresh and the abort in
``tests/test_node22_model_succession_timer_and_refresh.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import scripts.node22_model_succession as tool
from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from scripts.model_succession import copyback
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
    SERVICE,
    STEPS,
    SUCCESSION_ID,
    TIMER,
    Space,
    changed,
    no_database,
    report,
    space_fixture,
    tree,
)

IS_ACTIVE = {f"--user is-active {TIMER}", f"--user is-active {SERVICE}"}


def _files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _copyback_failure(space: Space) -> dict[str, Any]:
    (failure,) = space.failures()
    assert failure["step"] == "copyback" and failure["hard_stop"] is False
    # Before the timer was touched, and the receipt says so.
    assert failure["timer_touched_by_this_tool"] is False and failure["timer_stopped_by_this_tool"] is False
    assert "before the timer is touched" in failure["timer_note"]
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "active"
    assert "step-copyback.json" not in space.names()
    return failure


# --- copyback --------------------------------------------------------------------------


def test_copyback_copies_each_new_package_to_the_same_key_on_the_compute_store(space: Space) -> None:
    odd = space.package(space.new_rows[0], space.ws.shared) / "huai.cfg.para"
    odd.chmod(0o700)
    for row in space.new_rows:
        assert not space.package(row, space.ws.store).exists()

    space.run_steps("copyback")

    packages = space.receipt("step-copyback.json")["packages"]
    assert [package["model_id"] for package in packages] == [row["model_id"] for row in space.new_rows]
    for row, package in zip(space.new_rows, packages, strict=True):
        source, destination = space.package(row, space.ws.shared), space.package(row, space.ws.store)
        expected = _files(source)
        assert _files(destination) == expected and "CALIB/table.csv" in expected
        assert package == {
            "model_id": row["model_id"],
            "source": str(source),
            "destination": str(destination),
            "file_count": len(expected),
            "bytes": sum(len(content) for content in expected.values()),
            "outcome": "copied",
        }
        assert not list(destination.parent.glob(".*"))
    # File modes are not carried over (the compute store does not support it).
    copied = space.package(space.new_rows[0], space.ws.store) / "huai.cfg.para"
    assert not stat.S_IMODE(copied.stat().st_mode) & stat.S_IXUSR
    # The old packages and the shared store were not touched.
    assert space.systemctl.calls() == []


def test_an_identical_destination_is_skipped_and_left_alone(space: Space) -> None:
    row = space.new_rows[0]
    destination = space.package(row, space.ws.store)
    shutil.copytree(space.package(row, space.ws.shared), destination)
    before = tree(destination)

    space.run_steps("copyback")

    outcomes = [package["outcome"] for package in space.receipt("step-copyback.json")["packages"]]
    assert outcomes == ["already_present", "copied"]
    assert tree(destination) == before


@pytest.mark.parametrize("difference", ["changed_file", "extra_file", "missing_file"])
def test_a_differing_destination_is_refused_and_not_touched(space: Space, difference: str) -> None:
    row = space.new_rows[1]
    destination = space.package(row, space.ws.store)
    shutil.copytree(space.package(row, space.ws.shared), destination)
    if difference == "changed_file":
        (destination / "huai.cfg.calib").write_bytes(b"cfg.calib\nksat=9.9e-9\n")
    elif difference == "extra_file":
        (destination / "stray.txt").write_bytes(b"stray")
    else:
        (destination / "CALIB" / "table.csv").unlink()
    before = tree(destination)

    assert space.main("--apply") == 1

    failure = _copyback_failure(space)
    assert str(destination) in failure["reason"] and "Nothing was overwritten" in failure["reason"]
    assert tree(destination) == before
    # The first package was copied before the refusal; the same command finds it present.
    assert space.package(space.new_rows[0], space.ws.store).is_dir()


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "fifo"])
def test_a_symlink_or_a_non_regular_file_in_the_source_is_refused(space: Space, tmp_path: Path, kind: str) -> None:
    source = space.package(space.new_rows[0], space.ws.shared)
    if kind == "file_symlink":
        (source / "link.cfg").symlink_to(source / "huai.cfg.para")
    elif kind == "directory_symlink":
        (source / "CALIB-link").symlink_to(tmp_path)
    else:
        os.mkfifo(source / "pipe")

    assert space.main("--apply") == 1

    failure = _copyback_failure(space)
    assert "symlink or a non-regular file" in failure["reason"]
    destination = space.package(space.new_rows[0], space.ws.store)
    assert not destination.exists() and not list(destination.parent.glob(".*"))


def test_a_leftover_temporary_directory_of_a_killed_run_is_removed(space: Space) -> None:
    destination = space.package(space.new_rows[0], space.ws.store)
    leftover = destination.with_name(f".{destination.name}.model-succession-copy")
    (leftover / "half").mkdir(parents=True)
    (leftover / "half" / "junk").write_bytes(b"from a killed run")

    space.run_steps("copyback")

    assert not leftover.exists()
    assert _files(destination) == _files(space.package(space.new_rows[0], space.ws.shared))


@pytest.mark.parametrize("interruption", [OSError("input/output error"), KeyboardInterrupt()])
def test_a_copy_interrupted_half_way_leaves_no_partial_destination(
    space: Space, monkeypatch: pytest.MonkeyPatch, interruption: BaseException
) -> None:
    copied: list[str] = []
    copyfile = shutil.copyfile

    def interrupted(source: Any, target: Any, **kwargs: Any) -> Any:
        if len(copied) == 3:
            raise interruption
        copied.append(str(target))
        return copyfile(source, target, **kwargs)

    monkeypatch.setattr(copyback.shutil, "copyfile", interrupted)
    if isinstance(interruption, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            space.main("--apply")
        # A killed run leaves no failure receipt.
        assert space.failures() == []
    else:
        assert space.main("--apply") == 1
        assert "input/output error" in _copyback_failure(space)["reason"]
    assert len(copied) == 3
    destination = space.package(space.new_rows[0], space.ws.store)
    assert not destination.exists() and not list(destination.parent.glob(".*"))

    monkeypatch.setattr(copyback.shutil, "copyfile", copyfile)
    assert space.main("--apply") == 0


unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode of 000")


@unprivileged
@pytest.mark.parametrize("side", ["source", "destination"])
def test_an_unreadable_subdirectory_is_a_failure_that_names_it(space: Space, side: str) -> None:
    row = space.new_rows[0]
    source, destination = space.package(row, space.ws.shared), space.package(row, space.ws.store)
    if side == "destination":
        shutil.copytree(source, destination)
    locked = (source if side == "source" else destination) / "CALIB"
    locked.chmod(0)
    try:
        assert space.main("--apply") == 1
        failure = _copyback_failure(space)
    finally:
        locked.chmod(0o755)

    # Not a comparison that passes, or differs, over the part of the tree that could be listed.
    assert str(locked) in failure["reason"] and "cannot be read" in failure["reason"]
    if side == "source":
        assert not destination.exists() and not list(destination.parent.glob(".*"))


@unprivileged
@pytest.mark.parametrize("name", ["CALIB", "huai.cfg.para"], ids=["subdirectory", "file"])
def test_a_dry_run_reports_an_unreadable_source_as_a_predicted_refusal(
    space: Space, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    locked = space.package(space.new_rows[0], space.ws.shared) / name
    mode = stat.S_IMODE(locked.stat().st_mode)
    locked.chmod(0)
    try:
        assert space.main() == 1
        result = report(capsys)
    finally:
        locked.chmod(mode)

    package = result["steps"]["copyback"]["packages"][0]
    assert package["outcome"] == "refused" and str(locked) in package["reason"]
    assert any(str(locked) in refusal for refusal in result["would_be_refused"])
    assert set(space.systemctl.calls()) <= IS_ACTIVE


# --- the checks before any step ----------------------------------------------------------


def _provision_missing(space: Space) -> dict[str, Any]:
    (space.directory / "provision-apply.json").unlink()
    return {"expect": "provision-apply.json"}


def _provision_not_applied(space: Space) -> dict[str, Any]:
    path = space.directory / "provision-apply.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "outcome": "rolled_back"}), encoding="utf-8")
    return {"expect": "not a provision apply receipt with outcome 'applied'"}


def _provision_is_a_dry_run(space: Space) -> dict[str, Any]:
    path = space.directory / "provision-apply.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "dry_run": True}), encoding="utf-8")
    return {"expect": "not a provision apply receipt with outcome 'applied'"}


def _reprovision(space: Space, listed: list[str]) -> None:
    (space.directory / "provision-apply.json").unlink()
    space.ws.provision(SUCCESSION_ID, space.new_rows, listed=listed)


def _new_id_not_provisioned(space: Space) -> dict[str, Any]:
    _reprovision(space, [space.new_rows[0]["model_id"]])
    return {"expect": f"new model_id is not in models[] of {space.directory / 'provision-apply.json'}"}


def _provisioned_id_unaccounted(space: Space) -> dict[str, Any]:
    _reprovision(space, [*(row["model_id"] for row in space.new_rows), "dg_z_gfs_v9"])
    return {"expect": "neither a new id of a --pair nor in the canonical manifest: ['dg_z_gfs_v9']"}


def _old_id_not_canonical(space: Space) -> dict[str, Any]:
    pairs = [("dg_z_gfs_v1", space.pairs[0][1]), space.pairs[1]]
    return {"expect": "old model_id is not in the canonical manifest", "pairs": pairs}


def _new_id_already_canonical(space: Space) -> dict[str, Any]:
    live = space.ws.rows[2]["model_id"]
    _reprovision(space, [space.new_rows[0]["model_id"], live])
    pairs = [space.pairs[0], (space.pairs[1][0], live)]
    return {"expect": "new model_id is already in the canonical manifest", "pairs": pairs}


def _registry_changed(space: Space) -> dict[str, Any]:
    space.registry.write_bytes(space.registry.read_bytes() + b"\n")
    return {"expect": "recorded"}


def _registry_missing(space: Space) -> dict[str, Any]:
    space.registry.unlink()
    return {"expect": "cannot read the new-rows registry"}


INPUT_REFUSALS: dict[str, Callable[[Space], dict[str, Any]]] = {
    "provision_receipt_missing": _provision_missing,
    "provision_not_applied": _provision_not_applied,
    "provision_is_a_dry_run": _provision_is_a_dry_run,
    "new_id_not_provisioned": _new_id_not_provisioned,
    "provisioned_id_unaccounted": _provisioned_id_unaccounted,
    "old_id_not_canonical": _old_id_not_canonical,
    "new_id_already_canonical": _new_id_already_canonical,
    "registry_sha256_differs": _registry_changed,
    "registry_missing": _registry_missing,
}


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
@pytest.mark.parametrize("case", sorted(INPUT_REFUSALS))
def test_an_unsupported_plan_is_refused_before_anything_and_nothing_is_written(
    space: Space, capsys: pytest.CaptureFixture[str], case: str, mode: tuple[str, ...]
) -> None:
    arranged = INPUT_REFUSALS[case](space)
    before = tree(space.ws.root)

    assert space.main(*mode, pairs=arranged.get("pairs")) == 1

    captured = capsys.readouterr()
    assert arranged["expect"] in captured.err and "Nothing was written" in captured.err
    assert captured.out == ""
    assert changed(before, tree(space.ws.root)) == set()
    # No plan, no failure receipt, no copy, and not one systemctl call.
    assert "plan.json" not in space.names() and space.failures() == []
    assert space.systemctl.calls() == []


def test_the_new_rows_registry_can_be_named_and_must_be_the_provisioned_one(
    space: Space, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "copy-of-registry.json"
    shutil.copyfile(space.registry, elsewhere)
    assert space.main("--new-rows-registry", str(elsewhere)) == 0
    assert report(capsys)["new_rows_registry"]["path"] == str(elsewhere)

    elsewhere.write_bytes(elsewhere.read_bytes() + b" ")
    assert space.main("--new-rows-registry", str(elsewhere)) == 1
    assert "recorded" in capsys.readouterr().err


# --- dry-run ----------------------------------------------------------------------------


def test_a_dry_run_before_the_copyback_changes_nothing_and_only_queries(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(space.ws.root)

    assert space.main() == 0

    result = report(capsys)
    assert changed(before, tree(space.ws.root)) == set()
    assert list((space.ws.root / "tmp").iterdir()) == []
    calls = space.systemctl.calls()
    assert calls and set(calls) <= IS_ACTIVE
    assert result["dry_run"] is True and result["would_be_refused"] == []
    assert result["plan_json"].startswith("absent")
    assert set(result["steps"]) == set(STEPS)
    assert [package["outcome"] for package in result["steps"]["copyback"]["packages"]] == ["would_copy", "would_copy"]
    assert result["steps"]["preflight"]["status"] == "needs copyback"
    assert result["steps"]["begin"]["unit_states_now"] == {TIMER: "active", SERVICE: "inactive"}
    assert result["steps"]["refresh"] == result["steps"]["finish"] == {"status": "not predicted"}
    assert "plan.json" not in space.names()


def test_a_dry_run_with_the_packages_present_runs_both_tools_dry_runs_and_leaves_nothing(
    space: Space, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for row in space.new_rows:
        shutil.copytree(space.package(row, space.ws.shared), space.package(row, space.ws.store))
    before = tree(space.ws.root)
    output = tmp_path / "report.json"

    assert space.main("--output", str(output)) == 0

    result = report(capsys)
    # The report asked for, and the directory it was written into.
    assert changed(before, tree(space.ws.root)) == {"report.json", "."}
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert list((space.ws.root / "tmp").iterdir()) == []
    assert set(space.systemctl.calls()) <= IS_ACTIVE
    preflight = result["steps"]["preflight"]
    assert [package["outcome"] for package in result["steps"]["copyback"]["packages"]] == ["already_present"] * 2
    assert preflight["clone_dry_run"]["outcome"] == "would_clone"
    cloned = preflight["clone_dry_run"]["pairs"]
    assert [(pair["source_model_id"], pair["target_model_id"]) for pair in cloned] == space.pairs
    assert preflight["publish_dry_run"]["outcome"] == "would_publish"
    assert preflight["publish_dry_run"]["row_count_before"] == preflight["publish_dry_run"]["row_count_after"] == 3
    # Neither tool left its dry-run receipt: only the provision receipt is there.
    assert space.names() == ["provision-apply.json"]


def test_a_dry_run_reports_what_the_apply_would_refuse(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    for row in space.new_rows:
        shutil.copytree(space.package(row, space.ws.shared), space.package(row, space.ws.store))
    # The second target package declares another starting point: the clone gate refuses it.
    (space.package(space.new_rows[1], space.ws.store) / "huai.cfg.ic").write_bytes(b"cfg.ic\nanother start\n")
    before = tree(space.ws.root)

    assert space.main() == 1

    result = report(capsys)
    assert result["steps"]["copyback"]["packages"][1]["outcome"] == "differs"
    assert result["steps"]["preflight"]["status"] == "needs copyback"
    assert len(result["would_be_refused"]) == 1 and "differs" in result["would_be_refused"][0]
    assert changed(before, tree(space.ws.root)) == set()
    assert set(space.systemctl.calls()) <= IS_ACTIVE


def test_a_dry_run_reports_a_clone_gate_refusal(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    # The second new package declares another starting point on both stores: the clone gate refuses it.
    (space.package(space.new_rows[1], space.ws.shared) / "huai.cfg.ic").write_bytes(b"cfg.ic\nanother start\n")
    for row in space.new_rows:
        shutil.copytree(space.package(row, space.ws.shared), space.package(row, space.ws.store))
    before = tree(space.ws.root)

    assert space.main() == 1

    result = report(capsys)
    clone = result["steps"]["preflight"]["clone_dry_run"]
    assert clone["outcome"] == "refused" and "state clone refused" in clone["reason"]
    assert result["steps"]["preflight"]["publish_dry_run"]["outcome"] == "would_publish"
    assert changed(before, tree(space.ws.root)) == set()
    assert list((space.ws.root / "tmp").iterdir()) == []
    assert set(space.systemctl.calls()) <= IS_ACTIVE


def test_a_dry_run_of_a_started_succession_reports_the_completed_steps(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    space.systemctl.clear()
    before = tree(space.ws.root)

    assert space.main() == 0

    result = report(capsys)
    assert result["plan_json"].startswith("present")
    for step in ("copyback", "preflight", "begin", "clone"):
        assert result["steps"][step] == {"status": "completed"}
    assert result["steps"]["publish"] == {"would": "run the publish apply"}
    assert result["unit_states_now"] == {TIMER: "inactive", SERVICE: "inactive"}
    assert changed(before, tree(space.ws.root)) == set()
    assert set(space.systemctl.calls()) <= IS_ACTIVE


# --- DB-free, and the command ---------------------------------------------------------------


@pytest.mark.parametrize("mode", [(), ("--apply",), ("--abort", "--confirm-timer-start")])
@pytest.mark.parametrize("name", [min(LIBPQ_CONNECTION_ENV_KEYS), max(LIBPQ_CONNECTION_ENV_KEYS)])
def test_a_database_variable_in_the_environment_is_a_refusal(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, name: str, mode: tuple[str, ...]
) -> None:
    monkeypatch.setenv(name, "anything")
    before = tree(space.ws.root)

    assert space.main(*mode) == 1

    error = capsys.readouterr().err
    assert "DB-free" in error and name in error
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.calls() == []


@pytest.mark.parametrize(
    "name",
    [
        "OBJECT_STORE_ROOT",
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SCHEDULER_STATE_INDEX",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT",
    ],
)
def test_every_path_of_the_provider_refresh_environment_is_required(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    assert space.main("--apply") == 1
    assert f"{name} must be set" in capsys.readouterr().err
    assert space.systemctl.calls() == [] and "plan.json" not in space.names()


def test_the_settings_come_from_the_environment_and_the_defaults(space: Space) -> None:
    settings = space.settings()
    assert settings.receipt_root == space.ws.receipt_root
    assert settings.state_index == str(space.canonical_index)
    assert settings.mirror_state_index == str(space.mirror_index)
    assert settings.plan.provision_succession_id == SUCCESSION_ID
    assert tool._parse_args(space.argv()[:-2]).pass_wait_seconds == 14400

    custom = space.settings("--mirror-state-index", "/elsewhere/index.json", "--provision-succession-id", "prov-1")
    assert custom.mirror_state_index == "/elsewhere/index.json"
    assert custom.plan.provision_succession_id == "prov-1"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--pair", "only-one-id"], "expected <old_model_id>:<new_model_id>"),
        (["--confirm-timer-start"], "only valid with --abort"),
        (["--apply", "--abort"], "not allowed with argument"),
        (["--kind", "cold-start"], "invalid choice"),
    ],
)
def test_the_parser_refuses_a_malformed_command_line(
    space: Space, capsys: pytest.CaptureFixture[str], arguments: list[str], expected: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        space.main(*arguments)
    assert raised.value.code == 2 and expected in capsys.readouterr().err
    assert space.systemctl.calls() == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"cutover": "2026-10-05"}, "is not YYYYMMDDHH"),
        ({"pairs": []}, "at least one --pair"),
        ({"pairs": [("dg_a_gfs_v1", "dg_a_gfs_v2"), ("dg_a_gfs_v1", "dg_b_gfs_v2")]}, "one --pair only"),
    ],
)
def test_a_malformed_plan_is_refused(
    space: Space, capsys: pytest.CaptureFixture[str], overrides: dict[str, Any], expected: str
) -> None:
    assert space.main("--apply", **overrides) == 1
    assert expected in capsys.readouterr().err
    assert space.systemctl.calls() == [] and "plan.json" not in space.names()


def test_the_systemctl_binary_comes_from_the_environment(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.model_succession import systemd

    assert systemd.DEFAULT_SYSTEMCTL == "/usr/bin/systemctl"
    assert systemd.unit_state(TIMER) == "active"
    assert space.systemctl.calls() == [f"--user is-active {TIMER}"]
    monkeypatch.setenv(tool.SYSTEMCTL_ENV, str(space.ws.root / "no-such-systemctl"))
    with pytest.raises(systemd.UnitStateError, match="could not be run"):
        systemd.unit_state(TIMER)
    assert systemd.observed_state(TIMER).startswith("unknown")
