"""Model succession on node-22: the full apply, the step order, resume and hard stops (#2739).

The timer, the begin wait, the refresh and the abort are in
``tests/test_node22_model_succession_timer_and_refresh.py``; the copyback, the
checks before any step and the dry-run in
``tests/test_node22_model_succession_copyback_and_dry_run.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.node22_clone_direct_grid_cutover_states as clone_tool
import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt
from scripts.model_succession import run as succession_run
from scripts.model_succession.model import HardStop, StepFailure
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
    CUTOVER,
    REFRESH,
    SERVICE,
    STEPS,
    SUCCESSION_ID,
    TIMER,
    Space,
    changed,
    no_database,
    report,
    space_fixture,
    stores,
    tree,
)

IS_ACTIVE = {f"--user is-active {TIMER}", f"--user is-active {SERVICE}"}


def _forbid_tools(monkeypatch: pytest.MonkeyPatch, *, clone: bool = True, publish: bool = True) -> None:
    if clone:
        monkeypatch.setattr(clone_tool, "dispatch", lambda _args: pytest.fail("the clone tool was called again"))
    if publish:
        monkeypatch.setattr(
            publish_tool,
            "publish_merged_scheduler_registry",
            lambda **_kwargs: pytest.fail("the publish tool was called again"),
        )


# --- the full apply ------------------------------------------------------------------


def test_full_apply_runs_the_seven_steps_in_order_and_starts_the_timer(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    old_ids = [row["model_id"] for row in space.old_rows]
    new_ids = [row["model_id"] for row in space.new_rows]

    assert space.main("--apply") == 0

    assert report(capsys)["steps"] == dict.fromkeys(STEPS, "completed")
    assert space.names() == sorted(
        [
            "plan.json",
            "provision-apply.json",
            "timer-before-stop.json",
            "clone-dry-run.json",
            "clone-apply.json",
            "publish-dry-run.json",
            "publish-apply.json",
            *(f"step-{step}.json" for step in STEPS),
        ]
    )
    # The step receipts were written in the step order, each with the common fields.
    receipts = [space.receipt(f"step-{step}.json") for step in STEPS]
    assert [receipt["step"] for receipt in receipts] == list(STEPS)
    assert [receipt["generated_at"] for receipt in receipts] == sorted(receipt["generated_at"] for receipt in receipts)
    for receipt in receipts:
        assert receipt["schema_version"] == "nhms.model_succession.step_receipt.v1"
        assert receipt["succession_id"] == SUCCESSION_ID and receipt["outcome"] == "completed"
        assert receipt["operator_id"] == "operator-1" and receipt["host"]
        assert "git_commit" in receipt

    plan = space.receipt("plan.json")
    assert plan["kind"] == "recalibration" and plan["cutover_time"] == CUTOVER
    assert plan["provision_succession_id"] == SUCCESSION_ID
    assert plan["pairs"] == [{"old_model_id": old, "new_model_id": new} for old, new in space.pairs]
    assert plan["provision_apply_receipt_sha256"] == succession_receipt.file_sha256(
        space.directory / "provision-apply.json"
    )
    assert plan["new_rows_registry_sha256"] == succession_receipt.file_sha256(space.registry)
    assert space.receipt("timer-before-stop.json")["timer_was_active"] is True

    # Both manifests are equal and hold the new ids in place of the old ones.
    assert space.ws.canonical.read_bytes() == space.ws.mirror.read_bytes()
    ids = space.model_ids(space.ws.canonical)
    assert ids == [*new_ids, space.ws.rows[2]["model_id"]]
    assert not set(old_ids) & set(ids)
    # Both state indexes hold the same clone rows, one per pair.
    for index in (space.canonical_index, space.mirror_index):
        rows = space.clone_rows(index)
        assert [(row["cloned_from_model_id"], row["model_id"]) for row in rows] == space.pairs

    def rows(index: Path) -> list[dict[str, Any]]:
        # ``index_generated_at`` is when each index file was published, not part of the row.
        kept = space.clone_rows(index)
        return [{key: value for key, value in row.items() if key != "index_generated_at"} for row in kept]

    assert rows(space.canonical_index) == rows(space.mirror_index)

    # The tools' own receipts, and the step receipts that point at them.
    for step, name in (("clone", "clone-apply.json"), ("publish", "publish-apply.json")):
        record = space.receipt(f"step-{step}.json")[name.removesuffix(".json").replace("-", "_")]
        assert record == {
            "path": str(space.directory / name),
            "sha256": succession_receipt.file_sha256(space.directory / name),
        }
    assert space.receipt("clone-apply.json")["invocation_outcome"] == "complete"
    assert space.receipt("publish-apply.json")["outcome"] == "published"
    assert space.receipt("step-finish.json")["timer_action"] == "started"

    # Every systemctl call: is-active queries, one stop, one refresh start, one timer start, in that order.
    calls = space.systemctl.calls()
    assert set(calls) - IS_ACTIVE == {f"--user stop {TIMER}", f"--user start {REFRESH}", f"--user start {TIMER}"}
    assert space.systemctl.mutating() == [f"--user stop {TIMER}", f"--user start {REFRESH}", f"--user start {TIMER}"]
    assert space.systemctl.state(TIMER) == "active"


def test_nothing_but_is_active_is_issued_before_begin(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    from scripts.model_succession import scheduler

    real_begin = scheduler.begin

    def begin(settings: Any, inputs: Any) -> dict[str, Any]:
        seen.append(space.systemctl.calls())
        assert (space.directory / "step-preflight.json").exists()
        return real_begin(settings, inputs)

    monkeypatch.setitem(succession_run.STEP_FUNCTIONS, "begin", begin)
    assert space.main("--apply") == 0
    (before_begin,) = seen
    assert set(before_begin) <= IS_ACTIVE
    # The copyback and both dry-runs happened while the timer was still active.
    assert space.receipt("timer-before-stop.json")["timer_state"] == "active"


def test_a_second_apply_of_a_finished_succession_skips_every_step(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert space.main("--apply") == 0
    capsys.readouterr()
    before, calls = tree(space.ws.root), len(space.systemctl.mutating())
    _forbid_tools(monkeypatch)

    assert space.main("--apply") == 0
    assert report(capsys)["steps"] == dict.fromkeys(STEPS, "skipped")
    assert changed(before, tree(space.ws.root)) == set()
    assert len(space.systemctl.mutating()) == calls


# --- the order -----------------------------------------------------------------------


@pytest.mark.parametrize("step", STEPS[1:])
def test_a_step_refuses_when_the_receipt_of_the_step_before_it_is_missing(space: Space, step: str) -> None:
    index = STEPS.index(step)
    settings, inputs = space.run_steps(*STEPS[: index - 1])
    # The scheduler is stopped, so that only the missing receipt can be what refuses.
    space.systemctl.set_units(timer="inactive", service="inactive")
    space.systemctl.clear()
    before = tree(space.ws.root)

    with pytest.raises(StepFailure) as raised:
        succession_run.run_step(settings, inputs, step)

    missing = space.directory / f"step-{STEPS[index - 1]}.json"
    assert str(missing) in str(raised.value) and "is missing" in str(raised.value)
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.mutating() == []


def test_publish_before_clone_is_refused_and_neither_manifest_changes(space: Space) -> None:
    settings, inputs = space.run_steps("copyback", "preflight", "begin")
    before = stores(space)

    with pytest.raises(StepFailure) as raised:
        succession_run.run_step(settings, inputs, "publish")

    assert str(space.directory / "step-clone.json") in str(raised.value)
    assert stores(space) == before
    assert not (space.directory / "publish-apply.json").exists()
    assert not space.ws.backups()


# --- the command line is held to plan.json ----------------------------------------------


@pytest.mark.parametrize("mode", [(), ("--apply",), ("--abort", "--confirm-timer-start")])
@pytest.mark.parametrize("difference", ["pairs", "pair_order", "cutover_time"])
def test_a_command_line_that_differs_from_the_plan_is_refused(
    space: Space, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...], difference: str
) -> None:
    space.run_steps("copyback")
    overrides: dict[str, Any] = {
        "pairs": {"pairs": space.pairs[:1]},
        "pair_order": {"pairs": space.pairs[::-1]},
        "cutover_time": {"cutover": "2026100600"},
    }[difference]
    before = tree(space.ws.root)

    assert space.main(*mode, **overrides) == 1

    error = capsys.readouterr().err
    assert "differs from" in error and str(space.directory / "plan.json") in error
    # A new id is not the whole advice: this one may hold the timer, and only its abort starts it again.
    assert "new --succession-id" in error and "--abort --confirm-timer-start" in error
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.mutating() == []


def test_the_operator_is_not_part_of_the_plan(space: Space) -> None:
    space.run_steps("copyback")
    arguments = [value if value != "operator-1" else "operator-2" for value in space.argv("--apply")]
    from scripts import node22_model_succession as tool

    assert tool.main(arguments) == 0
    assert space.receipt("step-copyback.json")["operator_id"] == "operator-1"
    assert space.receipt("step-finish.json")["operator_id"] == "operator-2"


def test_a_changed_provision_registry_is_refused_against_the_plan(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.run_steps("copyback")
    # A second provision of the same id: the receipt and its registry agree with each other, not with the plan.
    (space.directory / "provision-apply.json").unlink()
    space.ws.provision(SUCCESSION_ID, space.new_rows, note="changed")

    assert space.main("--apply") == 1
    assert "provision_apply_receipt_sha256" in capsys.readouterr().err
    assert space.systemctl.mutating() == []


# --- resume --------------------------------------------------------------------------


def test_resume_reuses_the_clone_dry_run_receipt(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    # The publish dry-run refuses (the mirror differs), after the clone dry-run wrote its receipt.
    mirror = space.ws.mirror.read_bytes()
    space.ws.mirror.write_bytes(mirror + b"\n")
    assert space.main("--apply") == 1
    assert "clone-dry-run.json" in space.names() and "step-preflight.json" not in space.names()
    (failure,) = space.failures()
    assert failure["step"] == "preflight" and "differ before the change" in failure["reason"]
    space.ws.mirror.write_bytes(mirror)

    calls: list[bool] = []
    dispatch = clone_tool.dispatch
    monkeypatch.setattr(clone_tool, "dispatch", lambda args: (calls.append(args.apply), dispatch(args))[1])

    assert space.main("--apply") == 0
    # The clone tool ran once more, for the apply; its dry-run was not repeated.
    assert calls == [True]
    assert space.receipt("step-preflight.json")["clone_dry_run"]["reused"] is True


def test_a_clone_dry_run_receipt_of_another_plan_is_refused(space: Space) -> None:
    space.run_steps("copyback")
    target = space.directory / "clone-dry-run.json"
    target.write_text(
        json.dumps({"dry_run": True, "invocation_outcome": "complete", "cutover_time": "2026-10-06T00:00:00Z"}),
        encoding="utf-8",
    )

    assert space.main("--apply") == 1
    (failure,) = space.failures()
    assert failure["step"] == "preflight" and "cutover_time" in failure["reason"]
    assert "new --succession-id" in failure["reason"] and "--abort --confirm-timer-start" in failure["reason"]
    assert space.systemctl.mutating() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operations", "first pair only"),
        ("provision_succession_id", "another-provision"),
        ("succession_id", "another-succession"),
        ("dry_run", False),
        ("outcome", "refused"),
    ],
)
def test_a_publish_dry_run_receipt_of_another_plan_is_refused_in_preflight(
    space: Space, field: str, value: Any
) -> None:
    space.run_steps("copyback")

    def operations(pairs: list[tuple[str, str]]) -> dict[str, Any]:
        replace = [{"old_model_id": old, "new_model_id": new} for old, new in pairs]
        return {"replace": replace, "add": [], "remove": []}

    receipt = {
        "dry_run": True,
        "outcome": "planned",
        "succession_id": SUCCESSION_ID,
        "provision_succession_id": SUCCESSION_ID,
        "operations": operations(space.pairs),
    }
    receipt[field] = operations(space.pairs[:1]) if field == "operations" else value
    target = space.directory / "publish-dry-run.json"
    target.write_text(json.dumps(receipt), encoding="utf-8")

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    assert failure["step"] == "preflight" and failure["hard_stop"] is False
    assert str(target) in failure["reason"] and f"{field}=" in failure["reason"]
    assert "new --succession-id" in failure["reason"] and "--abort --confirm-timer-start" in failure["reason"]
    # Before the timer was touched; the foreign receipt was not replaced.
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "active"
    assert json.loads(target.read_text(encoding="utf-8")) == receipt
    assert "step-preflight.json" not in space.names() and "timer-before-stop.json" not in space.names()


@pytest.mark.parametrize("step", ["clone", "publish"])
def test_resume_adopts_a_successful_tool_receipt_without_calling_the_tool_again(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    # The run died between the tool's own receipt and the step receipt.
    space.run_steps(*STEPS[: STEPS.index(step) + 1])
    (space.directory / f"step-{step}.json").unlink()
    before = stores(space)
    _forbid_tools(monkeypatch, publish=step == "publish")

    assert space.main("--apply") == 0

    steps = report(capsys)["steps"]
    assert steps[step] == "completed" and steps["begin"] == "skipped"
    assert space.receipt(f"step-{step}.json")["adopted_existing_receipt"] is True
    if step == "publish":
        assert stores(space) == before


@pytest.mark.parametrize(
    ("step", "name", "field"),
    [
        ("clone", "clone-apply.json", "mirror_state_index"),
        ("clone", "clone-apply.json", "cutover_time"),
        ("clone", "clone-apply.json", "pairs"),
        ("publish", "publish-apply.json", "operations"),
        ("publish", "publish-apply.json", "succession_id"),
    ],
)
def test_a_tool_receipt_of_another_plan_is_not_adopted(
    space: Space, monkeypatch: pytest.MonkeyPatch, step: str, name: str, field: str
) -> None:
    space.run_steps(*STEPS[: STEPS.index(step) + 1])
    (space.directory / f"step-{step}.json").unlink()
    receipt = space.receipt(name)
    receipt[field] = [] if field == "pairs" else "something-else"
    (space.directory / name).write_text(json.dumps(receipt), encoding="utf-8")
    _forbid_tools(monkeypatch)

    assert space.main("--apply") == 1
    (failure,) = space.failures()
    assert failure["step"] == step and failure["hard_stop"] is True and field in failure["reason"]
    assert not (space.directory / f"step-{step}.json").exists()


def test_a_clone_apply_that_wrote_no_row_is_retried(space: Space, tmp_path: Path) -> None:
    space.run_steps("copyback", "preflight")
    parked = tmp_path / "index-parked.json"
    space.canonical_index.rename(parked)

    assert space.main("--apply") == 1
    (failure,) = space.failures()
    assert failure["step"] == "clone" and failure["hard_stop"] is False
    assert "clone-apply.json" not in space.names()

    parked.rename(space.canonical_index)
    assert space.main("--apply") == 0
    assert space.receipt("step-clone.json")["adopted_existing_receipt"] is False


# --- hard stops ----------------------------------------------------------------------


def _hard_stop(space: Space, step: str) -> dict[str, Any]:
    failure = space.failures()[-1]
    assert failure["step"] == step and failure["hard_stop"] is True
    assert len(failure["ways_on"]) == 1 and "runbook" in failure["ways_on"][0]
    assert "recalibration-and-archive.md" in failure["ways_on"][0]
    assert "--abort" not in " ".join(failure["ways_on"])
    assert not (space.directory / f"step-{step}.json").exists()
    assert space.systemctl.state(TIMER) == "inactive"
    assert f"--user start {TIMER}" not in space.systemctl.calls()
    return failure


def test_an_aborted_clone_apply_is_a_hard_stop_that_is_not_retried(space: Space) -> None:
    space.run_steps("copyback", "preflight")
    # The second pair's target package changed after the dry-run: the real clone tool applies the first
    # pair, refuses the second and leaves its aborted receipt.
    (space.package(space.new_rows[1], space.ws.store) / "huai.cfg.ic").write_bytes(b"cfg.ic\nanother start\n")

    assert space.main("--apply") == 1
    receipt = space.receipt("clone-apply.json")
    assert receipt["invocation_outcome"] == "aborted" and receipt["cloned_pair_count"] == 1
    failure = _hard_stop(space, "clone")
    assert "clone rows" in failure["reason"].lower()
    indexes = (space.canonical_index.read_bytes(), space.mirror_index.read_bytes())

    # The same command does not call the clone tool again.
    assert space.main("--apply") == 1
    assert len(space.failures()) == 2
    _hard_stop(space, "clone")
    assert (space.canonical_index.read_bytes(), space.mirror_index.read_bytes()) == indexes
    assert space.ws.canonical.read_bytes() == space.ws.mirror.read_bytes()
    assert space.model_ids(space.ws.canonical) == [row["model_id"] for row in space.ws.rows]


def test_an_inconsistent_publish_failure_receipt_is_a_hard_stop(
    space: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    planted = space.directory / "publish-apply-failed-20261005T120000Z.json"
    planted.write_text(json.dumps({"outcome": "inconsistent"}), encoding="utf-8")
    before = stores(space)
    _forbid_tools(monkeypatch)

    assert space.main("--apply") == 1
    failure = _hard_stop(space, "publish")
    assert str(planted) in failure["reason"]
    assert stores(space) == before


def test_a_publish_that_ends_inconsistent_is_a_hard_stop(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    receipt = {"outcome": "inconsistent"}

    def inconsistent(**_kwargs: Any) -> dict[str, Any]:
        message = "--apply inconsistent: the two manifests may now DIFFER"
        raise publish_tool.MergedRegistryPublishError(message, receipt=receipt)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", inconsistent)
    assert space.main("--apply") == 1
    assert "DIFFER" in _hard_stop(space, "publish")["reason"]


def _publish_without_its_receipt(space: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    write = succession_receipt.write_receipt

    def unwritable(path: Path, receipt: Any) -> None:
        if path.name == "publish-apply.json":
            raise OSError("no space left on device")
        write(path, receipt)

    monkeypatch.setattr(succession_receipt, "write_receipt", unwritable)
    assert space.main("--apply") == 1
    monkeypatch.setattr(succession_receipt, "write_receipt", write)


def test_a_publish_without_its_receipt_is_a_hard_stop(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_without_its_receipt(space, monkeypatch)

    failure = _hard_stop(space, "publish")
    assert "complete but has no receipt" in failure["reason"] and "unreceipted" in failure["reason"]
    # The real publish tool did publish both manifests.
    assert space.ws.canonical.read_bytes() == space.ws.mirror.read_bytes()
    assert set(row["model_id"] for row in space.new_rows) <= set(space.model_ids(space.ws.canonical))
    # As the publish tool itself says: the publish is in effect and is continued, not undone.
    assert "is in effect" in failure["reason"] and "restore" not in failure["reason"].lower()
    assert "provider refresh" in failure["reason"] and "start the timer" in failure["reason"]

    # Running the command again says the same, before any step and without touching the timer.
    before = stores(space)
    space.systemctl.clear()
    capsys.readouterr()
    assert space.main("--apply") == 1
    error = capsys.readouterr().err
    assert "is in effect" in error and "provider refresh" in error and "start the timer" in error
    assert "restore" not in error.lower() and "recalibration-and-archive.md" in error
    assert stores(space) == before and space.systemctl.mutating() == [] and len(space.failures()) == 1


def test_an_abort_after_a_publish_without_its_receipt_says_the_new_models_are_live(
    space: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_without_its_receipt(space, monkeypatch)
    assert "publish-apply.json" not in space.names()
    capsys.readouterr()

    assert space.main("--abort") == 0

    result = report(capsys)
    assert result["publish_state"] == "published_without_receipt"
    meaning = " ".join(result["what_this_state_means"])
    assert "new models are live" in meaning and "old models" not in meaning
    assert "finished by hand from the runbook" in meaning


def test_hard_stop_is_a_step_failure() -> None:
    assert issubclass(HardStop, StepFailure) and HardStop.hard_stop and not StepFailure.hard_stop
