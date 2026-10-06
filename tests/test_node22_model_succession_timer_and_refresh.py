"""Model succession on node-22: the scheduler timer, the begin wait, the refresh, failure and abort (#2739).

The full apply, the step order, resume and hard stops are in
``tests/test_node22_model_succession_apply_and_resume.py``; the copyback, the
checks before any step and the dry-run in
``tests/test_node22_model_succession_copyback_and_dry_run.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from packages.common.provider_atomic import provider_destination_lock
from scripts.scheduler_refresh.receipt_validation import _validate_receipt
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
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

STOP = f"--user stop {TIMER}"
START_TIMER = f"--user start {TIMER}"
START_REFRESH = f"--user start {REFRESH}"


# --- begin -----------------------------------------------------------------------------


def test_begin_times_out_while_the_service_runs_and_the_same_command_resumes(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.systemctl.set_units(service="active")

    assert space.main("--apply", "--pass-wait-seconds", "0") == 1

    (failure,) = space.failures()
    assert failure["step"] == "begin" and failure["hard_stop"] is False
    assert "--pass-wait-seconds" in failure["reason"] and "never stops or kills" in failure["reason"]
    assert failure["observed_unit_states"] == {TIMER: "inactive", SERVICE: "active"}
    assert failure["timer_stopped_by_this_tool"] is True and failure["timer_touched_by_this_tool"] is True
    assert space.receipt("timer-before-stop.json")["timer_was_active"] is True
    assert "step-begin.json" not in space.names()
    # The timer was stopped; the service was neither stopped nor killed, and nothing was started.
    assert space.systemctl.mutating() == [STOP]
    assert space.systemctl.state(SERVICE) == "active"
    capsys.readouterr()

    # The pass ended by itself: the same command completes and starts the timer at the end.
    space.systemctl.set_units(service="inactive")
    assert space.main("--apply", "--pass-wait-seconds", "0") == 0
    steps = report(capsys)["steps"]
    assert steps["copyback"] == steps["preflight"] == "skipped" and steps["begin"] == "completed"
    # The record of the first run is read, not re-derived from the now inactive timer.
    assert space.receipt("step-finish.json")["timer_action"] == "started"
    assert space.systemctl.mutating() == [STOP, STOP, START_REFRESH, START_TIMER]
    assert space.systemctl.state(TIMER) == "active"


def test_begin_waits_for_a_running_pass_to_end_by_itself(space: Space) -> None:
    # ``activating`` is what is-active prints for the oneshot scheduler service while a pass runs.
    space.systemctl.script(SERVICE, ["activating", "activating", "deactivating", "inactive"])

    assert space.main("--apply") == 0

    calls = space.systemctl.calls()
    first_stop = calls.index(STOP)
    # After the stop: the timer is read back, then the service is polled until it is not running.
    assert calls[first_stop + 1 : first_stop + 6] == [f"--user is-active {TIMER}", *[f"--user is-active {SERVICE}"] * 4]
    assert space.receipt("step-begin.json")["service_state"] == "inactive"
    assert space.systemctl.mutating() == [STOP, START_REFRESH, START_TIMER]


def test_a_timer_that_was_inactive_at_begin_is_not_started_by_finish(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.systemctl.set_units(timer="inactive")

    assert space.main("--apply") == 0

    out = capsys.readouterr()
    assert space.receipt("timer-before-stop.json")["timer_was_active"] is False
    assert space.receipt("step-finish.json")["timer_action"] == "left_stopped_was_inactive_at_begin"
    # The report and the summary say what was done to the timer, not only the receipt.
    result = json.loads(out.out)
    assert result["timer_action"] == "left_stopped_was_inactive_at_begin"
    assert "left stopped" in result["timer_note"] and "left stopped" in out.err
    assert START_TIMER not in space.systemctl.calls()
    assert space.systemctl.state(TIMER) == "inactive"


def test_a_failed_service_counts_as_not_running(space: Space) -> None:
    space.systemctl.set_units(service="failed")

    assert space.main("--apply") == 0
    assert space.receipt("step-begin.json")["service_state"] == "failed"
    assert space.systemctl.state(TIMER) == "active"


def test_a_failed_timer_stop_is_not_recorded_as_a_stop(space: Space) -> None:
    space.systemctl.configure(fail=[f"stop {TIMER}"])

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    assert failure["step"] == "begin" and "exited 1" in failure["reason"]
    # The record says what the timer was; the failure receipt says the stop did not happen.
    assert space.receipt("timer-before-stop.json")["timer_was_active"] is True
    assert failure["timer_touched_by_this_tool"] is True and failure["timer_stopped_by_this_tool"] is False
    assert failure["observed_unit_states"][TIMER] == "active"


@pytest.mark.parametrize(
    ("printed", "running"),
    [
        ("inactive", False),
        ("failed", False),
        ("active", True),
        ("activating", True),
        ("deactivating", True),
        ("reloading", True),
        ("maintenance", None),
        ("", None),
    ],
)
def test_what_is_active_prints_is_running_not_running_or_unknown(
    space: Space, printed: str, running: bool | None
) -> None:
    from scripts.model_succession import systemd

    space.systemctl.configure(print={SERVICE: printed})
    if running is None:
        with pytest.raises(systemd.UnitStateError, match="is unknown"):
            systemd.unit_state(SERVICE)
        assert systemd.observed_state(SERVICE).startswith("unknown")
    else:
        assert systemd.unit_state(SERVICE) == printed
        assert systemd.is_running(printed) is running


@pytest.mark.parametrize("output", ["maintenance", ""])
def test_an_unknown_is_active_output_is_a_refusal(space: Space, output: str) -> None:
    space.systemctl.configure(print={SERVICE: output})

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    assert failure["step"] == "begin" and repr(output) in failure["reason"]
    assert failure["observed_unit_states"][SERVICE].startswith("unknown")
    assert "step-begin.json" not in space.names() and "clone-apply.json" not in space.names()
    assert START_TIMER not in space.systemctl.calls()


def test_an_unknown_timer_state_at_begin_stops_nothing(space: Space) -> None:
    space.systemctl.configure(print={TIMER: "maintenance"})

    assert space.main("--apply") == 1
    assert space.systemctl.mutating() == []
    assert "timer-before-stop.json" not in space.names()


# --- somebody started the scheduler during the succession ---------------------------------


@pytest.mark.parametrize("state", ["active", "activating"])
@pytest.mark.parametrize("unit", ["timer", "service"])
@pytest.mark.parametrize("step", ["clone", "publish", "refresh", "finish"])
def test_a_running_scheduler_refuses_the_step_which_writes_nothing(
    space: Space, step: str, unit: str, state: str
) -> None:
    space.run_steps(*STEPS[: STEPS.index(step)])
    space.systemctl.set_units(**{unit: state})
    space.systemctl.clear()
    before, names = stores(space), space.names()

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    name = TIMER if unit == "timer" else SERVICE
    assert failure["step"] == step and "Somebody started the scheduler" in failure["reason"]
    assert failure["observed_unit_states"][name] == state
    assert stores(space) == before
    assert [entry for entry in space.names() if not entry.startswith("succession-failed-")] == names
    # Only queries: the refresh was not started and the scheduler was left as it was found.
    assert space.systemctl.mutating() == []
    assert space.systemctl.state(name) == state


# --- another succession holds the timer ---------------------------------------------------

OTHER_ID = "recal-2026100512-b"


def _stop_at_a_failed_clone(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    space.run_steps("copyback", "preflight")
    parked = space.ws.root / "index-parked.json"
    space.canonical_index.rename(parked)
    assert space.main("--apply") == 1
    assert space.failures()[-1]["step"] == "clone"
    parked.rename(space.canonical_index)
    assert space.receipt("timer-before-stop.json")["timer_was_active"] is True
    assert space.systemctl.state(TIMER) == "inactive"
    space.systemctl.clear()
    capsys.readouterr()


def test_a_second_succession_is_refused_while_an_earlier_one_holds_the_timer(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    _stop_at_a_failed_clone(space, capsys)
    other = space.ws.receipt_root / OTHER_ID
    before = tree(space.ws.root)

    # The apply is refused before it writes anything: recording the timer as inactive would lose that it ran.
    assert space.main_as(OTHER_ID, "--apply") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and repr(SUCCESSION_ID) in captured.err
    assert "--abort --confirm-timer-start" in captured.err and "Nothing was written" in captured.err
    assert not other.exists() and changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.mutating() == []

    # The dry-run predicts the same refusal.
    assert space.main_as(OTHER_ID) == 1
    (predicted,) = report(capsys)["would_be_refused"]
    assert repr(SUCCESSION_ID) in predicted and "--abort --confirm-timer-start" in predicted
    assert not other.exists() and space.systemctl.mutating() == []

    # The first succession is aborted, which starts the timer; the second one then finds it active and
    # starts it again at its own finish.
    assert space.main("--abort", "--confirm-timer-start") == 0
    assert space.systemctl.state(TIMER) == "active"
    capsys.readouterr()
    space.systemctl.clear()

    assert space.main_as(OTHER_ID, "--apply") == 0
    result = report(capsys)
    assert result["timer_action"] == "started" and "timer_note" not in result
    assert json.loads((other / "timer-before-stop.json").read_text(encoding="utf-8"))["timer_was_active"] is True
    assert json.loads((other / "step-finish.json").read_text(encoding="utf-8"))["timer_action"] == "started"
    assert space.systemctl.mutating() == [STOP, START_REFRESH, START_TIMER]
    assert space.systemctl.state(TIMER) == "active"


@pytest.mark.parametrize("content", ["{not json", '{"timer_was_active": "yes"}'])
def test_an_unreadable_timer_record_of_another_succession_is_a_refusal(
    space: Space, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    record = space.ws.receipt_root / "recal-earlier" / "timer-before-stop.json"
    record.parent.mkdir()
    record.write_text(content, encoding="utf-8")
    before = tree(space.ws.root)

    assert space.main("--apply") == 1
    error = capsys.readouterr().err
    assert str(record) in error and "Nothing was written" in error
    assert space.main() == 1
    assert any(str(record) in refusal for refusal in report(capsys)["would_be_refused"])
    assert changed(before, tree(space.ws.root)) == set()
    assert "plan.json" not in space.names() and space.systemctl.mutating() == []


def test_a_closed_succession_or_one_whose_timer_was_inactive_does_not_hold_the_timer(space: Space) -> None:
    for name, was_active, closing in (
        ("recal-finished", True, "step-finish.json"),
        ("recal-aborted", True, "abort-20261001T000000Z.json"),
        ("recal-timer-was-inactive", False, None),
        ("recal-provisioned-only", None, None),
    ):
        directory = space.ws.receipt_root / name
        directory.mkdir()
        if was_active is not None:
            (directory / "timer-before-stop.json").write_text(json.dumps({"timer_was_active": was_active}))
        if closing:
            (directory / closing).write_text("{}", encoding="utf-8")

    assert space.main("--apply") == 0
    assert space.systemctl.state(TIMER) == "active"


# --- refresh ----------------------------------------------------------------------------


def test_the_fake_refresh_unit_writes_a_receipt_the_refresh_itself_accepts(space: Space) -> None:
    space.run_steps(*STEPS[:6])
    receipt = json.loads((space.refresh_receipt_root / "latest.json").read_text(encoding="utf-8"))
    assert _validate_receipt(receipt)["outcome"] == "published"
    facts = space.receipt("step-refresh.json")
    assert facts["refresh_run_id"] == receipt["run_id"] and facts["refresh_outcome"] == "published"
    assert facts["registry_classification_totals"] == {"refused": 0, "added": 0, "removed": 0, "package_changed": 0}


REFRESH_FAILURES: dict[str, tuple[dict[str, Any], bool, str]] = {
    # knobs of the fake unit, whether an earlier latest.json exists, what the reason says
    "start_zero_but_latest_not_newer": ({"mode": "skip"}, True, "is not newer than this step"),
    "start_zero_and_no_latest": ({"mode": "skip"}, False, "cannot be read"),
    "started_before_the_step": ({"age_seconds": 60}, False, "is not newer than this step"),
    "outcome_not_published": ({"outcome": "failed", "reason": "provider_invalid"}, False, "not 'published'"),
    "refused_total": ({"totals": {"refused": 1}}, False, "'refused': 1"),
    "added_total": ({"totals": {"added": 2}}, False, "'added': 2"),
    "removed_total": ({"totals": {"removed": 1}}, False, "'removed': 1"),
    "package_changed_total": ({"totals": {"package_changed": 1}}, False, "'package_changed': 1"),
    "classification_missing": ({"drop_classification": True}, False, "no registry_classification"),
    "start_exits_non_zero": ({"rc": 1}, False, "exited 1"),
}


@pytest.mark.parametrize("case", sorted(REFRESH_FAILURES))
def test_a_refresh_that_did_not_publish_a_renewal_is_a_failure(
    space: Space, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    knobs, seeded, expected = REFRESH_FAILURES[case]
    space.run_steps(*STEPS[:5])
    if seeded:
        space.seed_refresh_receipt()
    space.systemctl.configure(refresh=knobs)
    space.systemctl.clear()

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    assert failure["step"] == "refresh" and failure["hard_stop"] is False
    assert expected in failure["reason"], failure["reason"]
    if expected == "is not newer than this step":
        # Why the start returned without a new receipt is not known to this tool: it lists the causes.
        assert "Possible causes" in failure["reason"] and "was already running" in failure["reason"]
        assert "skips the run without an error while" not in failure["reason"]
    assert "step-refresh.json" not in space.names()
    assert space.systemctl.mutating() == [START_REFRESH]
    assert space.systemctl.state(TIMER) == "inactive"
    capsys.readouterr()

    # With the cause gone the same command runs the refresh again and finishes.
    space.systemctl.configure(refresh={})
    assert space.main("--apply") == 0
    steps = report(capsys)["steps"]
    assert steps["publish"] == "skipped" and steps["refresh"] == steps["finish"] == "completed"
    assert space.systemctl.mutating() == [START_REFRESH, START_REFRESH, START_TIMER]


def test_a_refresh_start_that_does_not_return_in_time_is_a_failure(
    space: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.model_succession import model, scheduler

    # A little longer than the unit's own TimeoutStartSec of 7200 s.
    assert scheduler.REFRESH_START_TIMEOUT_SECONDS == model.REFRESH_START_TIMEOUT_SECONDS == 7500
    space.run_steps(*STEPS[:5])
    space.systemctl.configure(refresh={"hang_seconds": 20})
    monkeypatch.setattr(scheduler, "REFRESH_START_TIMEOUT_SECONDS", 0.5)

    assert space.main("--apply") == 1

    (failure,) = space.failures()
    assert failure["step"] == "refresh" and "did not return within" in failure["reason"]
    assert "step-refresh.json" not in space.names() and space.systemctl.state(TIMER) == "inactive"
    assert START_TIMER not in space.systemctl.calls()


def test_the_refresh_unit_skips_while_the_service_is_active_like_the_real_one(space: Space) -> None:
    # The fake has the real unit's start condition: this is what a zero exit without a run looks like.
    space.run_steps(*STEPS[:5])
    from scripts.model_succession import scheduler

    settings = space.settings("--apply")
    space.systemctl.set_units(service="active")
    with pytest.raises(Exception, match="cannot be read"):
        scheduler.refresh(settings, space.inputs(settings))


# --- a failure leaves the timer alone, and the same command resumes ------------------------


def _differing_destination(space: Space) -> Callable[[], None]:
    target = space.package(space.new_rows[0], space.ws.store)
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    return lambda: __import__("shutil").rmtree(target)


def _mirror_differs(space: Space) -> Callable[[], None]:
    content = space.ws.mirror.read_bytes()
    space.ws.mirror.write_bytes(content + b"\n")
    return lambda: space.ws.mirror.write_bytes(content)


def _service_never_ends(space: Space) -> Callable[[], None]:
    space.systemctl.set_units(service="active")
    return lambda: space.systemctl.set_units(service="inactive")


def _state_index_gone(space: Space) -> Callable[[], None]:
    parked = space.ws.root / "index-parked.json"
    space.canonical_index.rename(parked)
    return lambda: parked.rename(space.canonical_index)


def _refresh_lock_held(space: Space) -> Callable[[], None]:
    held = provider_destination_lock(Path(space.ws.refresh_lock), blocking=False)
    held.__enter__()
    return lambda: held.__exit__(None, None, None)


def _refresh_fails(space: Space) -> Callable[[], None]:
    space.systemctl.configure(refresh={"rc": 1})
    return lambda: space.systemctl.configure(refresh={})


def _timer_start_fails(space: Space) -> Callable[[], None]:
    space.systemctl.configure(fail=[f"start {TIMER}"])
    return lambda: space.systemctl.configure(fail=[])


STEP_FAILURES: dict[str, Callable[[Space], Callable[[], None]]] = {
    "copyback": _differing_destination,
    "preflight": _mirror_differs,
    "begin": _service_never_ends,
    "clone": _state_index_gone,
    "publish": _refresh_lock_held,
    "refresh": _refresh_fails,
    "finish": _timer_start_fails,
}


@pytest.mark.parametrize("step", STEPS)
def test_a_failed_step_leaves_the_timer_alone_and_the_same_command_resumes(
    space: Space, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    index = STEPS.index(step)
    space.run_steps(*STEPS[:index])
    remove_cause = STEP_FAILURES[step](space)
    space.systemctl.clear()
    timer_before = space.systemctl.state(TIMER)

    assert space.main("--apply", "--pass-wait-seconds", "0") == 1

    out = capsys.readouterr()
    (failure,) = space.failures()
    assert failure["schema_version"] == "nhms.model_succession.failure_receipt.v1"
    assert failure["step"] == step and failure["outcome"] == "failed" and failure["hard_stop"] is False
    assert failure["completed_steps"] == list(STEPS[:index])
    assert "plan.json" in failure["receipts"] and f"step-{step}.json" not in failure["receipts"]
    assert failure["timer_started_by_this_tool"] is False
    assert any("same command again" in way for way in failure["ways_on"])
    assert any("--abort --confirm-timer-start" in way for way in failure["ways_on"])
    printed = json.loads(out.out)["failure"]
    assert printed["reason"] == failure["reason"]
    assert printed["failure_receipt"] == str(next(space.directory.glob("succession-failed-*.json")))
    assert f"FAILED in step {step}" in out.err and printed["failure_receipt"] in out.err
    # The timer is where it was: active before begin, stopped from begin on. The tool did not start it.
    expected = "active" if index < 2 else "inactive"
    assert space.systemctl.state(TIMER) == expected
    assert failure["observed_unit_states"][TIMER] == expected
    assert failure["timer_touched_by_this_tool"] is (index >= 2)
    assert failure["timer_stopped_by_this_tool"] is (index >= 2)
    if index < 2:
        assert timer_before == "active" and space.systemctl.mutating() == []
        assert "before the timer is touched" in failure["timer_note"]
    elif step != "finish":
        assert START_TIMER not in space.systemctl.calls()
    else:
        # The one start the finish step issued is the injected failure; nothing retried it.
        assert space.systemctl.calls().count(START_TIMER) == 1

    remove_cause()
    assert space.main("--apply", "--pass-wait-seconds", "0") == 0
    steps = report(capsys)["steps"]
    assert steps == {name: ("skipped" if STEPS.index(name) < index else "completed") for name in STEPS}
    assert space.systemctl.state(TIMER) == "active"
    assert len(space.failures()) == 1


def test_a_killed_run_leaves_no_failure_receipt_and_the_same_command_resumes(
    space: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.model_succession import run as succession_run
    from scripts.model_succession import tools

    def killed(_settings: Any, _inputs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setitem(succession_run.STEP_FUNCTIONS, "clone", killed)
    with pytest.raises(KeyboardInterrupt):
        space.main("--apply")
    assert space.failures() == [] and space.systemctl.mutating() == [STOP]
    assert space.systemctl.state(TIMER) == "inactive"

    monkeypatch.setitem(succession_run.STEP_FUNCTIONS, "clone", tools.clone)
    assert space.main("--apply") == 0
    assert space.systemctl.state(TIMER) == "active"


# --- abort ------------------------------------------------------------------------------


def test_abort_without_the_confirmation_only_reports(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    space.systemctl.clear()
    before = tree(space.ws.root)

    assert space.main("--abort") == 0

    result = report(capsys)
    assert result["aborted"] is False and "changes nothing" in result["note"]
    assert result["completed_steps"] == ["copyback", "preflight", "begin", "clone"]
    assert result["timer_action_on_confirm"].startswith(f"start {TIMER}")
    assert result["unit_states_now"] == {TIMER: "inactive", SERVICE: "inactive"}
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "inactive"
    # Not closed: the succession can still be applied.
    assert space.main("--apply") == 0


def test_a_confirmed_abort_starts_the_timer_and_closes_the_succession(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone")
    space.systemctl.clear()
    before = stores(space)

    assert space.main("--abort", "--confirm-timer-start") == 0

    result = report(capsys)
    (path,) = sorted(space.directory.glob("abort-*.json"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert result["abort_receipt"] == str(path) and receipt["aborted"] is True
    assert receipt["schema_version"] == "nhms.model_succession.abort_receipt.v1"
    assert receipt["completed_steps"] == ["copyback", "preflight", "begin", "clone"]
    assert receipt["timer_action"].startswith(f"start {TIMER}")
    assert receipt["publish_state"] == "not_published" and receipt["timer_was_active_at_begin"] is True
    meaning = " ".join(receipt["what_this_state_means"])
    assert "keeps running the old models" in meaning
    assert "stay in both state indexes" in meaning and "earliest clone row" in meaning
    assert "later cutover time would still take effect at this one" in meaning
    assert space.systemctl.mutating() == [START_TIMER] and space.systemctl.state(TIMER) == "active"
    assert stores(space) == before

    # Closed: every later run with this id refuses and touches nothing.
    snapshot = tree(space.ws.root)
    space.systemctl.clear()
    for mode in ((), ("--apply",), ("--abort",), ("--abort", "--confirm-timer-start")):
        assert space.main(*mode) == 1
        assert "was aborted" in capsys.readouterr().err
    assert changed(snapshot, tree(space.ws.root)) == set()
    assert space.systemctl.calls() == []


def test_an_abort_before_begin_and_one_with_an_inactive_timer_start_nothing(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.run_steps("copyback")
    assert space.main("--abort", "--confirm-timer-start") == 0
    result = report(capsys)
    assert result["timer_action"].startswith("none: the begin step never")
    assert "Clone rows" not in " ".join(result["what_this_state_means"])
    assert space.systemctl.mutating() == []


def test_an_abort_leaves_a_timer_that_was_inactive_at_begin(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    space.systemctl.set_units(timer="inactive")
    space.run_steps("copyback", "preflight", "begin")
    space.systemctl.clear()
    assert space.main("--abort", "--confirm-timer-start") == 0
    result = report(capsys)
    assert result["timer_action"].startswith("none: the timer was not active")
    assert result["publish_state"] == "not_published" and result["timer_was_active_at_begin"] is False
    # Nothing here says the scheduler runs: its timer was stopped before this succession and stays so.
    meaning = " ".join(result["what_this_state_means"])
    assert "keeps running" not in meaning
    assert "was not active when the succession began" in meaning and "left as it is" in meaning
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "inactive"


def test_an_abort_after_publish_says_the_new_models_are_live(space: Space, capsys: pytest.CaptureFixture[str]) -> None:
    space.run_steps("copyback", "preflight", "begin", "clone", "publish")
    assert space.main("--abort", "--confirm-timer-start") == 0
    result = report(capsys)
    assert result["publish_state"] == "published"
    meaning = " ".join(result["what_this_state_means"])
    assert "new models are live" in meaning and "finished by hand from the runbook" in meaning
    assert space.systemctl.state(TIMER) == "active"


def test_an_abort_whose_timer_start_fails_does_not_close_the_succession(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    space.run_steps("copyback", "preflight", "begin")
    space.systemctl.configure(fail=[f"start {TIMER}"])
    assert space.main("--abort", "--confirm-timer-start") == 1
    assert "No abort receipt was written" in capsys.readouterr().err
    assert not list(space.directory.glob("abort-*.json"))


def test_abort_refuses_a_succession_that_never_started_or_has_finished(
    space: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    assert space.main("--abort", "--confirm-timer-start") == 1
    assert "nothing to abort" in capsys.readouterr().err
    assert space.systemctl.calls() == []

    assert space.main("--apply") == 0
    space.systemctl.clear()
    assert space.main("--abort", "--confirm-timer-start") == 1
    assert "has finished" in capsys.readouterr().err
    assert space.systemctl.calls() == [] and not list(space.directory.glob("abort-*.json"))
