"""The remove-basin kind of the model succession tool, and the argument rules it adds to every kind (#2757).

A removed basin has no new model and no provision: nothing is copied, compared,
audited or cloned.  Its ``preflight`` is the publish tool's dry-run of the
removal, its ``publish`` takes the rows out of both manifests, and the plan,
the receipts and the reports say in ``continuity`` that the scheduler no longer
plans the basin and that the node-27 retirement tool is the next step.  The
canonical manifest of these tests has two sources, as the publish tool requires
every basin to have a row for each of them or for none; the manifests and the
state indexes are real files and the publish tool actually runs.  The fixtures,
the fake ``systemctl`` and the builders are in
``tests/model_succession_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt
from packages.common.object_store import sha256_bytes
from scripts.model_succession import run as succession_run
from scripts.model_succession.model import StepFailure
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
    ADD_BASIN_PACKAGE,
    COLD_START_PACKAGE,
    CUTOVER,
    REFRESH,
    SERVICE,
    SUCCESSION_ID,
    TIMER,
    Space,
    build_space,
    changed,
    no_database,
    report,
    stores,
    tree,
)

IS_ACTIVE = {f"--user is-active {TIMER}", f"--user is-active {SERVICE}"}
STOP = f"--user stop {TIMER}"
START_TIMER = f"--user start {TIMER}"
START_REFRESH = f"--user start {REFRESH}"
REMOVE_BASIN_STEPS = ("preflight", "begin", "publish", "refresh", "finish")
SOURCES = ("gfs", "IFS")
# The two models of basin ``a``, one per source, and the six rows the canonical manifest holds before.
REMOVED = ["dg_a_gfs_v1", "dg_a_ifs_v1"]
BEFORE = ["dg_a_gfs_v1", "dg_b_gfs_v1", "dg_c_gfs_v1", "dg_a_ifs_v1", "dg_b_ifs_v1", "dg_c_ifs_v1"]
KEPT = [model_id for model_id in BEFORE if model_id not in REMOVED]
# What no text of a removal may say: it has no new model and no pair.
FOREIGN_WORDS = ("new model", "--pair")


@pytest.fixture(name="removed")
def removed_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Space:
    """A remove-basin succession of the two models of basin ``a``, out of a canonical manifest of three basins
    with two sources each.  Nothing was provisioned: the receipt root is empty."""

    return build_space(tmp_path, monkeypatch, kind="remove_basin", sources=SOURCES, removed_basin="a")


def _assert_continuity(record: dict[str, Any]) -> None:
    continuity = dict(record["continuity"])
    notice = continuity.pop("notice")
    assert continuity == {"mode": "basin_removed", "state_carried": False}
    assert "the scheduler no longer plans runs for the removed models" in notice
    assert "already submitted may still finish and be ingested" in notice
    # The next step, and the id that ties the two halves of the retirement together.
    assert "the next step is the node-27 retirement tool" in notice
    assert f"takes the same --succession-id {SUCCESSION_ID}" in notice


def _assert_no_foreign_word(*texts: Any) -> None:
    text = " ".join(value if isinstance(value, str) else json.dumps(value) for value in texts).lower()
    assert not [word for word in FOREIGN_WORDS if word in text], text


def _provision_receipts(space: Space) -> list[Path]:
    return sorted(space.ws.receipt_root.rglob("provision-apply.json")) if space.ws.receipt_root.exists() else []


# --- the remove-basin apply -----------------------------------------------------------------


def test_a_remove_basin_apply_runs_its_five_steps_and_removes_two_rows_leaving_every_other_row_as_it_was(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    index_paths = (removed.canonical_index, removed.mirror_index)
    indexes = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in index_paths}
    rows_before = {
        str(row["model_id"]): json.dumps(row, sort_keys=True) for row in removed.ws.models(removed.ws.canonical)
    }
    assert list(rows_before) == BEFORE and removed.removes == REMOVED
    # Nothing was provisioned for a removal, and nothing is.
    assert removed.registry is None and removed.new_rows == [] and _provision_receipts(removed) == []

    assert removed.main("--apply") == 0

    result = report(capsys)
    assert removed.settings().plan.steps == REMOVE_BASIN_STEPS
    assert result["steps"] == dict.fromkeys(REMOVE_BASIN_STEPS, "completed")
    assert result["outcome"] == "completed" and result["timer_action"] == "started"
    _assert_continuity(result)
    # No copyback step, no audit, no clone receipt of either sort, and no provision receipt anywhere.
    assert removed.names() == sorted(
        [
            "plan.json",
            "timer-before-stop.json",
            "publish-dry-run.json",
            "publish-apply.json",
            *(f"step-{step}.json" for step in REMOVE_BASIN_STEPS),
        ]
    )
    assert "step-copyback.json" not in removed.names() and _provision_receipts(removed) == []
    receipts = [removed.receipt(f"step-{step}.json") for step in REMOVE_BASIN_STEPS]
    assert [receipt["step"] for receipt in receipts] == list(REMOVE_BASIN_STEPS)
    assert [receipt["generated_at"] for receipt in receipts] == sorted(receipt["generated_at"] for receipt in receipts)

    # Neither state index was written: both are the bytes they were, state rows of the removed basin included.
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in indexes} == indexes
    assert REMOVED[0] in removed.canonical_index.read_text(encoding="utf-8")

    # Both manifests are equal and lost exactly the two rows; every other row is what it was, in its order.
    assert removed.ws.canonical.read_bytes() == removed.ws.mirror.read_bytes()
    rows_after = {
        str(row["model_id"]): json.dumps(row, sort_keys=True) for row in removed.ws.models(removed.ws.canonical)
    }
    assert list(rows_after) == KEPT == removed.model_ids(removed.ws.mirror)
    assert rows_after == {model_id: rows_before[model_id] for model_id in KEPT}
    publish = removed.receipt("publish-apply.json")
    assert publish["operations"] == {"replace": [], "add": [], "remove": REMOVED}
    assert (publish["row_count_before"], publish["row_count_after"]) == (6, 4)
    assert publish["removed_model_ids"] == REMOVED and publish["introduced_model_ids"] == []
    assert "provision_apply_receipt" not in publish

    # The preflight of a removal is the publish dry-run alone.
    preflight = removed.receipt("step-preflight.json")
    assert preflight["publish_dry_run"] == {
        "path": str(removed.directory / "publish-dry-run.json"),
        "sha256": succession_receipt.file_sha256(removed.directory / "publish-dry-run.json"),
        "reused": False,
    }
    assert not {"kind_check", "ic_audit", "clone_dry_run"} & set(preflight)

    plan = removed.receipt("plan.json")
    header = {name: plan.pop(name) for name in ("generated_at", "host", "git_commit")}
    assert all(isinstance(value, str) and value for value in header.values())
    _assert_continuity(plan)
    # The keys of a provision are left out, not written as null.
    assert {key: value for key, value in plan.items() if key != "continuity"} == {
        "schema_version": "nhms.model_succession.plan.v1",
        "succession_id": SUCCESSION_ID,
        "operator_id": "operator-1",
        "kind": "remove_basin",
        "removes": REMOVED,
    }
    for record in (removed.receipt("step-publish.json"), removed.receipt("step-finish.json")):
        _assert_continuity(record)

    # One stop, one refresh start, one timer start; everything before the stop was a query.
    assert removed.systemctl.mutating() == [STOP, START_REFRESH, START_TIMER]
    assert set(removed.systemctl.calls()[: removed.systemctl.calls().index(STOP)]) <= IS_ACTIVE
    assert removed.systemctl.state(TIMER) == "active"


def test_a_second_apply_of_a_finished_removal_skips_every_step(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    assert removed.main("--apply") == 0
    capsys.readouterr()
    before = stores(removed)
    removed.systemctl.clear()

    # The removed ids are gone from the manifest: the finished succession is not refused for that.
    assert removed.main("--apply") == 0

    assert report(capsys)["steps"] == dict.fromkeys(REMOVE_BASIN_STEPS, "skipped")
    assert stores(removed) == before and removed.systemctl.mutating() == []


# --- the arguments must fit the kind --------------------------------------------------------

# kind -> the options of the command line -> what the refusal says.
MISFITS: dict[str, tuple[str, dict[str, Any], tuple[str, ...], str]] = {
    "a_remove_with_recalibration": (
        "recalibration",
        {},
        ("--remove", "dg_c_gfs_v1"),
        "--remove is not valid with --kind recalibration",
    ),
    "a_remove_with_cold_start": (
        "cold_start",
        {},
        ("--remove", "dg_c_gfs_v1"),
        "--remove is not valid with --kind cold_start",
    ),
    "a_remove_with_add_basin": (
        "add_basin",
        {},
        ("--remove", "dg_c_gfs_v1"),
        "--remove is not valid with --kind add_basin",
    ),
    "a_pair_with_remove_basin": (
        "remove_basin",
        {},
        ("--pair", "dg_b_gfs_v1:dg_b_gfs_v2"),
        "--pair is not valid with --kind remove_basin",
    ),
    "an_add_with_remove_basin": (
        "remove_basin",
        {},
        ("--add", "dg_d_gfs_v1"),
        "--add is not valid with --kind remove_basin",
    ),
    "a_cutover_time_with_remove_basin": (
        "remove_basin",
        {},
        ("--cutover-time", CUTOVER),
        "--cutover-time is not valid with --kind remove_basin",
    ),
    "a_provision_succession_id_with_remove_basin": (
        "remove_basin",
        {},
        ("--provision-succession-id", "another-succession"),
        "--provision-succession-id is not valid with --kind remove_basin",
    ),
    "a_new_rows_registry_with_remove_basin": (
        "remove_basin",
        {},
        ("--new-rows-registry", "/nonexistent/direct-grid-registry.json"),
        "--new-rows-registry is not valid with --kind remove_basin",
    ),
    "remove_basin_without_a_remove": (
        "remove_basin",
        {"removes": []},
        (),
        "at least one --remove <model_id> is required with --kind remove_basin",
    ),
    "a_removed_id_named_twice": (
        "remove_basin",
        {"removes": [*REMOVED, REMOVED[0]]},
        (),
        f"may be named with --remove once only: ['{REMOVED[0]}']",
    ),
}


def _space_of_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> Space:
    if kind == "remove_basin":
        return build_space(tmp_path, monkeypatch, kind=kind, sources=SOURCES, removed_basin="a")
    if kind == "add_basin":
        packages = (ADD_BASIN_PACKAGE, ADD_BASIN_PACKAGE)
        return build_space(tmp_path, monkeypatch, kind=kind, sources=SOURCES, added_basin="d", new_packages=packages)
    packages = (COLD_START_PACKAGE, COLD_START_PACKAGE) if kind == "cold_start" else ({}, {})
    return build_space(tmp_path, monkeypatch, kind=kind, new_packages=packages)


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
@pytest.mark.parametrize("case", sorted(MISFITS))
def test_arguments_that_do_not_fit_the_kind_are_refused_and_nothing_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    mode: tuple[str, ...],
) -> None:
    kind, arguments, extra, expected = MISFITS[case]
    space = _space_of_kind(tmp_path, monkeypatch, kind)
    before = tree(space.ws.root)

    assert space.main(*mode, *extra, **arguments) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Refused: ") and expected in captured.err
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.calls() == []


def test_every_misfitting_option_of_a_removal_is_named_in_one_refusal(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    assert removed.main("--cutover-time", CUTOVER, "--add", "dg_d_gfs_v1") == 1

    assert capsys.readouterr().err.startswith(
        "Refused: --add, --cutover-time is not valid with --kind remove_basin: a removal has no successor"
    )


def test_the_refusals_of_an_add_basin_keep_their_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    space = _space_of_kind(tmp_path, monkeypatch, "add_basin")
    added = [str(row["model_id"]) for row in space.new_rows]

    assert space.main("--pair", "dg_a_gfs_v1:dg_d_gfs_v1") == 1
    assert capsys.readouterr().err == (
        "Refused: --pair is not valid with --kind add_basin: a new basin has no old model to replace. "
        "Name each of its models with --add <new_model_id>, or run the replacement under its own kind and "
        "--succession-id.\n"
    )
    assert space.main("--cutover-time", CUTOVER) == 1
    assert capsys.readouterr().err == (
        "Refused: --cutover-time is not valid with --kind add_basin: a new basin has no history and "
        "so no cutover. Leave the option out.\n"
    )
    assert space.main(adds=[]) == 1
    assert capsys.readouterr().err == (
        "Refused: at least one --add <new_model_id> is required with --kind add_basin: one per source "
        "of each new basin.\n"
    )
    assert space.main(adds=[*added, added[0]]) == 1
    assert capsys.readouterr().err == f"Refused: a model_id may be named with --add once only: ['{added[0]}'].\n"


@pytest.mark.parametrize("kind", ["recalibration", "cold_start"])
def test_the_refusals_of_the_replacing_kinds_keep_their_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    space = _space_of_kind(tmp_path, monkeypatch, kind)
    old, new = space.pairs[0]

    assert space.main("--add", "dg_d_gfs_v1") == 1
    assert capsys.readouterr().err == (
        f"Refused: --add is not valid with --kind {kind}, which replaces models: name each replaced model "
        "and its successor with --pair, or add the models of a new basin with --kind add_basin.\n"
    )
    assert space.main(pairs=[]) == 1
    assert capsys.readouterr().err == "Refused: at least one --pair <old_model_id>:<new_model_id> is required.\n"
    assert space.main(pairs=[(old, new), (old, "another")]) == 1
    assert capsys.readouterr().err == f"Refused: a model_id may be named in one --pair only, and once: ['{old}'].\n"
    assert space.main(cutover=None) == 1
    assert capsys.readouterr().err == f"Refused: --cutover-time <YYYYMMDDHH> is required with --kind {kind}.\n"
    assert space.main(cutover="20261005") == 1
    assert capsys.readouterr().err == "Refused: --cutover-time '20261005' is not YYYYMMDDHH.\n"


# --- the plan must name rows the scheduler holds ----------------------------------------------


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
@pytest.mark.parametrize(
    "removes", [["dg_e_gfs_v1", "dg_e_ifs_v1"], [*REMOVED, "dg_e_gfs_v1"]], ids=["every_id_absent", "one_id_absent"]
)
def test_a_removed_id_that_is_not_in_the_manifest_is_refused_before_any_step_and_not_as_a_publish_in_effect(
    removed: Space, capsys: pytest.CaptureFixture[str], removes: list[str], mode: tuple[str, ...]
) -> None:
    # With every id absent both manifests "hold no old id": before begin that is still not a publish of this plan.
    absent = [model_id for model_id in removes if model_id not in BEFORE]
    before = tree(removed.ws.root)

    assert removed.main(*mode, removes=removes) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"is not in the canonical manifest {removed.ws.canonical}: {absent}. Nothing was written." in captured.err
    assert "is in effect" not in captured.err and "Do not undo it" not in captured.err
    _assert_no_foreign_word(captured.err)
    assert changed(before, tree(removed.ws.root)) == set() and removed.failures() == []
    assert removed.systemctl.mutating() == [] and not removed.directory.exists()


# --- preflight: the publish tool's dry-run -------------------------------------------------------


def test_one_source_of_a_basin_alone_is_refused_by_the_publish_dry_run_in_preflight(removed: Space) -> None:
    before = stores(removed)

    assert removed.main("--apply", removes=REMOVED[:1]) == 1

    (failure,) = removed.failures()
    assert failure["step"] == "preflight" and failure["hard_stop"] is False and failure["completed_steps"] == []
    assert failure["timer_touched_by_this_tool"] is False and failure["timer_stopped_by_this_tool"] is False
    reason = str(failure["reason"])
    assert reason.startswith("The publish dry-run refused: ")
    assert "every basin_id must have exactly one row for each of the sources ['IFS', 'gfs']" in reason
    assert "basins_a has ['IFS']" in reason and "Add or remove every source of a basin together" in reason
    # The timer was never stopped and nothing was published.
    assert removed.systemctl.mutating() == [] and removed.systemctl.state(TIMER) == "active"
    assert not {"timer-before-stop.json", "step-preflight.json", "publish-dry-run.json"} & set(removed.names())
    assert stores(removed) == before


# --- publish: after begin --------------------------------------------------------------------------


def test_a_remove_basin_publish_before_begin_is_refused_and_neither_manifest_changes(removed: Space) -> None:
    settings, inputs = removed.run_steps("preflight")
    before = stores(removed)

    with pytest.raises(StepFailure, match="requires the receipt of the begin step") as raised:
        succession_run.run_step(settings, inputs, "publish")

    assert str(removed.directory / "step-begin.json") in str(raised.value)
    assert stores(removed) == before and "publish-apply.json" not in removed.names()
    assert removed.model_ids(removed.ws.canonical) == BEFORE


def test_a_resume_after_a_failed_remove_basin_publish_completes(
    removed: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = publish_tool.publish_merged_scheduler_registry

    def refuse_the_apply(**arguments: Any) -> dict[str, Any]:
        if arguments["apply"]:
            raise publish_tool.MergedRegistryPublishError("refused: injected")
        return real(**arguments)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", refuse_the_apply)
    before = stores(removed)
    assert removed.main("--apply") == 1
    (failure,) = removed.failures()
    assert failure["step"] == "publish" and failure["completed_steps"] == ["preflight", "begin"]
    assert failure["reason"] == "The publish apply did not publish: refused: injected"
    assert failure["hard_stop"] is False and failure["timer_stopped_by_this_tool"] is True
    assert stores(removed) == before and removed.systemctl.state(TIMER) == "inactive"
    _assert_no_foreign_word(failure, capsys.readouterr().err)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", real)
    assert removed.main("--apply") == 0

    steps = report(capsys)["steps"]
    assert steps == {
        step: ("skipped" if step in ("preflight", "begin") else "completed") for step in REMOVE_BASIN_STEPS
    }
    assert removed.model_ids(removed.ws.canonical) == KEPT == removed.model_ids(removed.ws.mirror)
    assert removed.systemctl.state(TIMER) == "active" and len(removed.failures()) == 1


def test_a_remove_basin_publish_without_its_receipt_is_a_hard_stop_and_aborts_as_published(
    removed: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    removed.run_steps("preflight", "begin")
    write = succession_receipt.write_receipt

    def unwritable(path: Path, receipt: Any) -> None:
        if path.name == "publish-apply.json":
            raise OSError("no space left on device")
        write(path, receipt)

    monkeypatch.setattr(succession_receipt, "write_receipt", unwritable)
    assert removed.main("--apply") == 1
    monkeypatch.setattr(succession_receipt, "write_receipt", write)

    (failure,) = removed.failures()
    assert failure["step"] == "publish" and failure["hard_stop"] is True
    assert "complete but has no receipt" in failure["reason"] and "is in effect" in failure["reason"]
    # The runbook section of this kind.
    assert "section 5.7.4" in failure["reason"] and "section 5.7.3" not in failure["reason"]
    assert removed.model_ids(removed.ws.canonical) == KEPT
    assert removed.ws.canonical.read_bytes() == removed.ws.mirror.read_bytes()
    _assert_no_foreign_word(failure, capsys.readouterr().err)

    # The rerun is stopped before any step: "reached publish" is the begin receipt of this kind.
    before = stores(removed)
    removed.systemctl.clear()
    assert removed.main("--apply") == 1
    error = capsys.readouterr().err
    assert "neither manifest holds any model_id this plan removes any more" in error
    assert "a publish of this plan is in effect" in error and "provider refresh" in error
    assert "section 5.7.4" in error and "Nothing was written" in error
    _assert_no_foreign_word(error)
    assert stores(removed) == before and removed.systemctl.mutating() == [] and len(removed.failures()) == 1

    assert removed.main("--abort") == 0
    result = report(capsys)
    assert result["publish_state"] == "published_without_receipt" and result["aborted"] is False
    meaning = " ".join(result["what_this_state_means"])
    assert "The publish is in effect although its receipt" in meaning
    assert "the removed models are out of both manifests" in meaning and "section 5.7.4" in meaning
    _assert_no_foreign_word(result)


def test_an_abort_of_a_remove_basin_after_its_publish_reports_it_as_published(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    removed.run_steps("preflight", "begin", "publish")

    assert removed.main("--abort") == 0

    result = report(capsys)
    assert result["publish_state"] == "published" and result["completed_steps"] == ["preflight", "begin", "publish"]
    meaning = " ".join(result["what_this_state_means"])
    assert meaning.startswith("The publish completed: the removed models are out of both manifests.")
    assert "must be finished by hand" in meaning and "section 5.7.4" in meaning
    _assert_no_foreign_word(result)


def test_an_abort_of_a_remove_basin_after_begin_starts_the_timer_and_says_the_basin_keeps_being_scheduled(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    removed.run_steps("preflight", "begin")
    removed.systemctl.clear()
    before = stores(removed)
    assert removed.systemctl.state(TIMER) == "inactive"

    assert removed.main("--abort", "--confirm-timer-start") == 0

    result = report(capsys)
    (path,) = sorted(removed.directory.glob("abort-*.json"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert result["abort_receipt"] == str(path) and receipt["aborted"] is True
    assert receipt["completed_steps"] == ["preflight", "begin"]
    assert receipt["publish_state"] == "not_published" and receipt["timer_was_active_at_begin"] is True
    assert receipt["what_this_state_means"] == [
        "The publish did not complete: the basin of the removed models keeps being scheduled."
    ]
    _assert_no_foreign_word(receipt)
    assert removed.systemctl.mutating() == [START_TIMER] and removed.systemctl.state(TIMER) == "active"
    assert stores(removed) == before and removed.model_ids(removed.ws.canonical) == BEFORE


def test_a_remove_basin_past_begin_with_manifests_that_differ_is_refused(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    removed.run_steps("preflight", "begin")
    # What a publish killed between its two writes leaves: one manifest changed, no receipt.
    removed.ws.mirror.write_bytes(removed.ws.mirror.read_bytes() + b"\n")
    before = tree(removed.ws.root)
    removed.systemctl.clear()
    capsys.readouterr()

    for mode in ((), ("--apply",)):
        assert removed.main(*mode) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and "Refused: The two registry manifests differ" in captured.err
        assert "section 5.7.4" in captured.err and "--abort --confirm-timer-start" in captured.err
        _assert_no_foreign_word(captured.err)
    assert changed(before, tree(removed.ws.root)) == set() and removed.failures() == []
    assert removed.systemctl.mutating() == [] and removed.systemctl.state(TIMER) == "inactive"


def test_another_removal_is_refused_while_this_one_holds_the_timer(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    removed.run_steps("preflight", "begin")
    before = tree(removed.ws.root)
    removed.systemctl.clear()

    # A removal has no provision: the other id is given no --provision-succession-id.
    assert removed.main_as("retire-other", "--apply", provisioned=False) == 1

    error = capsys.readouterr().err
    assert f"succession {SUCCESSION_ID!r} stopped the scheduler timer" in error
    assert changed(before, tree(removed.ws.root)) == set() and removed.systemctl.mutating() == []


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
def test_a_removal_is_refused_when_the_receipt_root_does_not_exist(
    removed: Space, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...]
) -> None:
    # No provision receipt is read for a removal, so a receipt root that is not there is first met by the check
    # of the other successions: whether one of them holds the timer is unknown, and that is a refusal.
    removed.ws.receipt_root.rmdir()
    before = tree(removed.ws.root)

    assert removed.main(*mode) == 1

    captured = capsys.readouterr()
    expected = f"Refused: cannot list the receipt root {removed.ws.receipt_root} "
    if mode:
        assert captured.out == "" and captured.err.startswith(expected)
    else:
        (refusal,) = json.loads(captured.out)["would_be_refused"]
        assert refusal.startswith(expected)
    assert changed(before, tree(removed.ws.root)) == set() and removed.systemctl.mutating() == []


# --- the dry-run -----------------------------------------------------------------------------


def test_a_remove_basin_dry_run_changes_nothing_and_reports_its_five_steps_and_the_ids_it_would_remove(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(removed.ws.root)

    assert removed.main() == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    notice = captured.err.lower()
    assert "dry-run" in notice and "publishes both manifests without the removed rows" in notice
    assert not [word for word in ("pair", "new model", "clone", "kind", "audit", "cop") if word in notice]
    assert result["dry_run"] is True and result["would_be_refused"] == []
    assert result["plan"] == {"kind": "remove_basin", "removes": REMOVED}
    assert result["plan_json"] == "absent: the first --apply writes it"
    assert sorted(result["steps"]) == sorted(REMOVE_BASIN_STEPS) and len(result["steps"]) == 5
    # The preflight of a removal is the publish dry-run, and it runs at once: there is no package to wait for.
    preflight = result["steps"]["preflight"]
    assert list(preflight) == ["publish_dry_run"]
    publish = preflight["publish_dry_run"]
    assert sorted(publish) == [
        "manifest_bytes_remaining",
        "manifest_json_nodes_remaining",
        "outcome",
        "removed_model_ids",
        "row_count_after",
        "row_count_before",
    ]
    assert publish["outcome"] == "would_publish" and publish["removed_model_ids"] == REMOVED
    assert (publish["row_count_before"], publish["row_count_after"]) == (6, 4)
    assert result["steps"]["publish"] == {"would": "run the publish apply"}
    assert result["steps"]["refresh"] == result["steps"]["finish"] == {"status": "not predicted"}
    # Neither a copyback, an audit, a clone nor a kind check, and no record of a provision.
    text = json.dumps(result)
    assert not [word for word in ("copyback", "audit", "clone", "kind_check", "needs") if word in text]
    assert "provision_apply_receipt" not in result and "new_rows_registry" not in result
    _assert_continuity(result)
    # Not one file changed, no receipt directory was made, and only queries were issued.
    assert changed(before, tree(removed.ws.root)) == set()
    assert not removed.directory.exists() and _provision_receipts(removed) == []
    assert list((removed.ws.root / "tmp").iterdir()) == []
    assert set(removed.systemctl.calls()) <= IS_ACTIVE


def test_a_remove_basin_dry_run_reports_what_the_publish_dry_run_would_refuse(
    removed: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(removed.ws.root)

    assert removed.main(removes=REMOVED[:1]) == 1

    result = report(capsys)
    publish = result["steps"]["preflight"]["publish_dry_run"]
    assert publish["outcome"] == "refused" and "basins_a has ['IFS']" in publish["reason"]
    assert result["would_be_refused"] == [publish["reason"]]
    assert changed(before, tree(removed.ws.root)) == set()


# --- the plans of the three earlier kinds are what they were ---------------------------------

# ``continuity.notice`` of a cold start and of an added basin, as written before this kind existed.
COLD_START_NOTICE = (
    "Cold start: no state is carried from the old models. Each new model starts from the calibrated initial "
    "condition in its package at the first cycle the scheduler plans after the timer is started, and the "
    "hydrograph of these basins is discontinuous there. The cutover time is recorded as the operator declared "
    "it; this tool does not enforce it."
)
NEW_BASIN_NOTICE = (
    "New basin: there are no earlier forecasts of it and no state to carry. Each model starts from the calibrated "
    "initial condition in its package at the first cycle the scheduler plans for it after the timer is started. "
    "That is the earliest cycle of the scheduler's lookback window, not the current one: the basin then catches "
    "up cycle by cycle."
)
PAIRS = [
    {"old_model_id": "dg_a_gfs_v1", "new_model_id": "dg_a_gfs_v2"},
    {"old_model_id": "dg_b_gfs_v1", "new_model_id": "dg_b_gfs_v2"},
]
# kind -> the plan record of the command line, and its ``continuity``.
EARLIER_PLANS: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {
    "recalibration": ({"kind": "recalibration", "pairs": PAIRS, "cutover_time": "2026100512"}, None),
    "cold_start": (
        {"kind": "cold_start", "pairs": PAIRS, "cutover_time": "2026100512"},
        {
            "mode": "cold_start",
            "state_carried": False,
            "declared_cutover_time": "2026100512",
            "notice": COLD_START_NOTICE,
        },
    ),
    "add_basin": (
        {"kind": "add_basin", "adds": ["dg_d_gfs_v1", "dg_d_ifs_v1"]},
        {"mode": "new_basin", "state_carried": False, "notice": NEW_BASIN_NOTICE},
    ),
}


@pytest.mark.parametrize("kind", sorted(EARLIER_PLANS))
def test_the_plan_json_of_an_earlier_kind_holds_exactly_what_it_held_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    space = _space_of_kind(tmp_path, monkeypatch, kind)
    assert space.registry is not None
    receipt_path = space.directory / "provision-apply.json"
    provisioned = {"path": str(receipt_path), "sha256": sha256_bytes(receipt_path.read_bytes())}
    registry = {"path": str(space.registry), "sha256": sha256_bytes(space.registry.read_bytes())}
    record, continuity = EARLIER_PLANS[kind]

    space.run_steps()

    plan = space.receipt("plan.json")
    header = {name: plan.pop(name) for name in ("generated_at", "host", "git_commit")}
    assert all(isinstance(value, str) and value for value in header.values())
    expected: dict[str, Any] = {
        "schema_version": "nhms.model_succession.plan.v1",
        "succession_id": SUCCESSION_ID,
        "operator_id": "operator-1",
        **record,
        "provision_succession_id": SUCCESSION_ID,
        "provision_apply_receipt_sha256": provisioned["sha256"],
        "new_rows_registry_sha256": registry["sha256"],
        "provision_apply_receipt": provisioned,
        "new_rows_registry": registry,
    }
    if continuity is not None:
        expected["continuity"] = continuity
    assert plan == expected
    # The record a resume compares, and what the dry-run reports of the provision, are unchanged too.
    assert space.main() == 0
    result = report(capsys)
    assert result["plan"] == {**record, "provision_succession_id": SUCCESSION_ID}
    assert result["plan_json"] == "present and equal to this command line"
    assert result["provision_apply_receipt"] == provisioned and result["new_rows_registry"] == registry
