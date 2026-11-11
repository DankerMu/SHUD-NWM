"""#3121: node-22 state-index prune-retention auto-enforce at the scheduler pass boundary.

Seams: the module's ``main`` (exit code, stdout receipt, receipt files, stderr
log) driven through three fakes only at system boundaries:

* a fake ``systemctl`` executable injected via ``NHMS_NODE22_SYSTEMCTL``
  (state files per unit, a busy/queued-job countdown, stop/start failure
  switches, and an optional ``kill -TERM $PPID`` on the first ``stop`` or
  ``start``);
* a fake ``pgrep`` executable injected via ``NHMS_STATE_INDEX_AUTO_PRUNE_PGREP``
  (exit codes popped from a queue, or a hang for the timeout case);
* ``scripts.scheduler_state_index_repair.repair_state_index`` replaced with a
  queue of summaries / exceptions.

All three append to one event log, so call order is one sequence. Expected
values come from design.md (flow 1-7, Required evidence 1-19, the receipt and
systemd sections) and the runbook's anchored wrapper pattern, never from the
module. Case 20 (a signal at the restore boundary or inside main's error
branch) extends design invariant 1 "timer 必恢复"; it is not a numbered design
case. The real-signal cases (11, 15, 16 and the subprocess half of 20) run the
module in a subprocess with a small driver so a broken handler cannot take the
pytest process down.
"""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from packages.common import state_manager
from scripts import node22_state_index_auto_prune as auto
from scripts import scheduler_state_index_repair as repair
from tests.test_state_index_retention import Lanes, _prunable

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHED_TIMER = "nhms-compute-scheduler.timer"
SCHED_SERVICE = "nhms-compute-scheduler.service"
REFRESH_TIMER = "nhms-scheduler-file-provider-refresh.timer"
REFRESH_SERVICE = "nhms-scheduler-file-provider-refresh.service"
SERVICE_UNIT = "infra/systemd/nhms-scheduler-state-index-prune.service"
TIMER_UNIT = "infra/systemd/nhms-scheduler-state-index-prune.timer"
NODE22_ACTIVE = "/scratch/frd_muziyao/NWM"
NODE22_VENV_PY = f"{NODE22_ACTIVE}/.venv/bin/python"
# Verbatim from docs/runbooks/production-ops/known-issues-state-index.md 8.12.
RUNBOOK_WRAPPER_PATTERN = r"^(/[^ ]*/)?bash( -[^ ]+)* [^ ]*node22-run-cycle-once\.sh( |$)"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
D='{d}'
verb="$1"
for unit; do :; done
echo "systemctl $verb $unit" >> "$D/log"
case "$verb" in
  is-active)
    if [ -f "$D/busy/$unit" ]; then
      n=$(cat "$D/busy/$unit")
      if [ "$n" != 0 ]; then
        [ "$n" != forever ] && echo $((n - 1)) > "$D/busy/$unit"
        echo active
        exit 0
      fi
    fi
    if [ -f "$D/busy-after/$unit" ]; then
      n=$(cat "$D/busy-after/$unit")
      if [ "$n" = 0 ]; then
        echo active
        exit 0
      fi
      echo $((n - 1)) > "$D/busy-after/$unit"
    fi
    if [ -f "$D/state/$unit" ]; then state=$(cat "$D/state/$unit"); else state=inactive; fi
    printf '%s\\n' "$state"
    [ "$state" = active ] && exit 0
    exit 3
    ;;
  show)
    if [ -f "$D/jobs/$unit" ]; then
      n=$(cat "$D/jobs/$unit")
      if [ "$n" != 0 ]; then
        echo $((n - 1)) > "$D/jobs/$unit"
        echo 304554
        exit 0
      fi
    fi
    echo
    exit 0
    ;;
  stop)
    if [ -f "$D/kill-on-stop" ]; then
      rm -f "$D/kill-on-stop"
      kill -TERM "$PPID"
    fi
    [ -f "$D/fail-stop/$unit" ] && exit 1
    echo inactive > "$D/state/$unit"
    exit 0
    ;;
  start)
    if [ -f "$D/kill-on-start" ]; then
      rm -f "$D/kill-on-start"
      kill -TERM "$PPID"
    fi
    [ -f "$D/fail-start/$unit" ] && exit 1
    echo active > "$D/state/$unit"
    exit 0
    ;;
esac
exit 64
"""

FAKE_PGREP = """#!/usr/bin/env bash
D='{d}'
echo "pgrep" >> "$D/log"
if [ -s "$D/pgrep-rcs" ]; then
  rc=$(head -n 1 "$D/pgrep-rcs")
  tail -n +2 "$D/pgrep-rcs" > "$D/pgrep-rcs.tmp"
  mv "$D/pgrep-rcs.tmp" "$D/pgrep-rcs"
else
  rc=1
fi
[ "$rc" = timeout ] && exec sleep 5
exit "$rc"
"""

DRIVER = """
import json, os, signal, sys
sys.path.insert(0, {repo!r})
from scripts import node22_state_index_auto_prune as auto
from scripts import scheduler_state_index_repair as repair

cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())
queue = list(cfg["outcomes"])
log = cfg["log"]


def note(line):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(line + "\\n")


dry_runs = [0]


def fake_repair(**kwargs):
    note("repair enforce=%s" % kwargs["enforce"])
    outcome = queue.pop(0) if len(queue) > 1 else queue[0]
    if not kwargs["enforce"]:
        dry_runs[0] += 1
        if cfg.get("kill_on_dry_run") == dry_runs[0]:
            os.kill(os.getpid(), signal.SIGTERM)
    if kwargs["enforce"] and cfg.get("kill_during_enforce"):
        os.kill(os.getpid(), signal.SIGTERM)
        note("repair enforce returned")
    return outcome


sleeps = [0]


def fake_sleep(seconds):
    sleeps[0] += 1
    note("sleep")
    if cfg.get("kill_on_sleep") == sleeps[0]:
        os.kill(os.getpid(), signal.SIGTERM)


repair.repair_state_index = fake_repair
auto._sleep = fake_sleep
raise SystemExit(auto.main([]))
"""


class Harness:
    def __init__(self, root: Path) -> None:
        self.dir = root / "fake"
        for sub in ("state", "busy", "busy-after", "jobs", "fail-stop", "fail-start"):
            (self.dir / sub).mkdir(parents=True)
        self.log = self.dir / "log"
        self.log.write_text("", encoding="utf-8")
        self.systemctl = self._script("systemctl", FAKE_SYSTEMCTL)
        self.pgrep = self._script("pgrep", FAKE_PGREP)
        self.receipt_root = root / "auto-prune"
        self.receipt_root.mkdir()
        os.chmod(self.receipt_root, 0o700)
        self.set_state(SCHED_TIMER, "active")
        self.set_state(REFRESH_TIMER, "active")

    def _script(self, name: str, template: str) -> Path:
        path = self.dir / name
        path.write_text(template.replace("{d}", str(self.dir)), encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def set_state(self, unit: str, value: str) -> None:
        (self.dir / "state" / unit).write_text(value + "\n" if value else "", encoding="utf-8")

    def busy(self, unit: str, times: int | str) -> None:
        (self.dir / "busy" / unit).write_text(str(times), encoding="utf-8")

    def busy_after(self, unit: str, idle_answers: int) -> None:
        (self.dir / "busy-after" / unit).write_text(str(idle_answers), encoding="utf-8")

    def queued(self, unit: str, times: int) -> None:
        (self.dir / "jobs" / unit).write_text(str(times), encoding="utf-8")

    def fail(self, verb: str, unit: str) -> None:
        (self.dir / f"fail-{verb}" / unit).write_text("", encoding="utf-8")

    def pgrep_codes(self, *codes: int | str) -> None:
        (self.dir / "pgrep-rcs").write_text("".join(f"{code}\n" for code in codes), encoding="utf-8")

    def note(self, line: str) -> None:
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def events(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def key_events(self) -> list[str]:
        keep = ("systemctl stop", "systemctl start", "repair", "pgrep", "sleep")
        return [event for event in self.events() if event.startswith(keep)]

    def verbs(self, verb: str) -> list[str]:
        prefix = f"systemctl {verb} "
        return [event[len(prefix) :] for event in self.events() if event.startswith(prefix)]

    def latest(self) -> dict[str, Any]:
        return json.loads((self.receipt_root / "latest.json").read_text(encoding="utf-8"))


@pytest.fixture(name="h")
def harness_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    harness = Harness(tmp_path)
    monkeypatch.setenv("NHMS_NODE22_SYSTEMCTL", str(harness.systemctl))
    monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_PGREP", str(harness.pgrep))
    monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT", str(harness.receipt_root))
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", "16")
    monkeypatch.delenv("NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS", raising=False)
    clock = [1000.0]

    def fake_sleep(seconds: float) -> None:
        harness.note("sleep")
        clock[0] += seconds

    monkeypatch.setattr(auto, "_sleep", fake_sleep)
    monkeypatch.setattr(auto, "_monotonic", lambda: clock[0])
    return harness


# --- summary builders ---------------------------------------------------------


def _capacity(warning: bool) -> dict[str, Any]:
    return {"entry_count": 100, "utilization_ratio": 0.75 if warning else 0.3, "warning": warning}


def _lane(
    name: str,
    *,
    warning: bool = False,
    removed: int = 5,
    checksum_valid: Any = True,
    entries: int = 100,
    warning_after: bool = False,
    status: str = "preview",
    digest: str | None = None,
) -> dict[str, Any]:
    return {
        "root": f"/roots/{name}",
        "checksum_valid": checksum_valid,
        "action": "prune-retention" if removed else "skip",
        "status": status,
        "entry_count_before": entries,
        "retention": {
            "entry_count_before": entries,
            "removed_count": removed,
            "removed_state_ids": [f"{name}-{i}" for i in range(removed)],
            "removed_state_ids_sha256": digest or f"sha256:{name}",
            "retention_days": 21,
            "cycle_lag_hours": 16,
            "capacity_before": _capacity(warning),
            "capacity_after": _capacity(warning_after),
            "groups": [{"model_id": "m", "removed_count": removed}],
        },
    }


def _summary(mode: str = "dry_run", status: str = "preview", **lanes: dict[str, Any]) -> dict[str, Any]:
    base = {"reference": _lane("reference", warning=True), "destination": _lane("destination", warning=True)}
    base.update(lanes)
    return {"mode": mode, "status": status, "lanes": base}


def _below() -> dict[str, Any]:
    return _summary(reference=_lane("reference"), destination=_lane("destination"))


def _enforced(**lanes: dict[str, Any]) -> dict[str, Any]:
    base = {
        "reference": _lane("reference", warning=True, status="repaired"),
        "destination": _lane("destination", warning=True, status="repaired"),
    }
    base.update(lanes)
    for lane in base.values():
        lane["retention"].pop("removed_state_ids", None)
    return {"mode": "enforce", "status": "repaired", "lanes": base}


def _fake_repair(monkeypatch: pytest.MonkeyPatch, h: Harness, *outcomes: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(outcomes)

    def fake(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        h.note(f"repair enforce={kwargs['enforce']}")
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(repair, "repair_state_index", fake)
    return calls


def _run(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str]:
    code = auto.main([])
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured.err


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


FULL_ORDER = [
    "repair enforce=False",
    "pgrep",
    f"systemctl stop {SCHED_TIMER}",
    f"systemctl stop {REFRESH_TIMER}",
    "sleep",
    "sleep",
    "pgrep",
    "repair enforce=False",
    "repair enforce=True",
    f"systemctl start {SCHED_TIMER}",
    f"systemctl start {REFRESH_TIMER}",
]


# --- 1. below threshold -------------------------------------------------------


def test_e1_below_threshold_touches_no_timer_and_skips(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, h, _below())

    code, receipt, _ = _run(capsys)

    assert code == 0
    assert receipt["action"] == "skip" and receipt["exit_code"] == 0
    assert [event for event in h.events() if event.startswith("systemctl")] == []
    assert [call["enforce"] for call in calls] == [False]
    assert receipt["timers_stopped"] == [] and receipt["timers_restored"] == []
    assert h.latest() == receipt


# --- 2. over threshold: full order, window from lag ---------------------------


@pytest.mark.parametrize(("lag", "days", "source"), [("294", 29, "derived_from_cycle_lag"), ("16", 21, "default")])
def test_e2_over_threshold_freezes_waits_gates_enforces_and_restores_in_order(
    lag: str,
    days: int,
    source: str,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NHMS_SCHEDULER_CYCLE_LAG_HOURS", lag)
    h.busy(SCHED_SERVICE, 2)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced" and receipt["exit_code"] == 0
    assert h.key_events() == FULL_ORDER
    assert [call["enforce"] for call in calls] == [False, False, True]
    assert all(call["operation"] == "prune-retention" for call in calls)
    assert all(call["retention_days"] == days and call["cycle_lag_hours"] is None for call in calls)
    assert receipt["retention_days"] == days and receipt["retention_days_source"] == source
    assert receipt["cycle_lag_hours"] == int(lag)
    assert receipt["timers_stopped"] == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["waited_seconds"] == 2 * auto.DEFAULT_POLL_SECONDS
    assert receipt["signal"] is None and receipt["error"] is None
    assert receipt["schema_version"] == "nhms.scheduler.state_index_auto_prune_receipt.v1"
    gate_lanes = receipt["gate"]["lanes"]
    assert gate_lanes["reference"]["removed_state_ids_sha256"] == "sha256:reference"
    assert gate_lanes["destination"]["removed_state_ids_sha256"] == "sha256:destination"
    assert receipt["enforce"]["lanes"]["reference"]["status"] == "repaired"
    assert receipt["enforce"]["lanes"]["destination"]["digest_matches_gate"] is True
    assert h.verbs("is-active").count(SCHED_TIMER) == 1


def test_e2_enforce_digest_drift_is_recorded_not_failed(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted = _enforced(destination=_lane("destination", warning=True, status="repaired", digest="sha256:other"))
    _fake_repair(monkeypatch, h, _summary(), _summary(), drifted)

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced"
    assert receipt["enforce"]["lanes"]["destination"]["digest_matches_gate"] is False
    assert receipt["enforce"]["lanes"]["reference"]["digest_matches_gate"] is True


# --- 3. wrapper running -------------------------------------------------------


def test_e3_wrapper_running_at_first_check_touches_no_timer(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.pgrep_codes(0)
    calls = _fake_repair(monkeypatch, h, _summary())

    code, receipt, _ = _run(capsys)

    assert code == 1 and receipt["action"] == "deferred_wrapper_running"
    assert h.verbs("stop") == [] and h.verbs("start") == []
    assert [call["enforce"] for call in calls] == [False]


def test_e3_wrapper_appearing_at_gate_aborts_enforce_and_restores(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.pgrep_codes(1, 0)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 1 and receipt["action"] == "deferred_wrapper_running"
    assert [call["enforce"] for call in calls] == [False]
    assert h.verbs("stop") == [SCHED_TIMER, REFRESH_TIMER]
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]


# --- 4. wait timeout ----------------------------------------------------------


def test_e4_wait_timeout_restores_and_exits_2(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS", "60")
    monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS", "30")
    h.busy(SCHED_SERVICE, "forever")
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["action"] == "wait_timeout"
    assert [call["enforce"] for call in calls] == [False]
    assert h.key_events().count("sleep") == 2
    assert receipt["waited_seconds"] == 60
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


# --- 5. final gate fails after freezing ---------------------------------------


@pytest.mark.parametrize(
    ("lane", "reason"),
    [
        (_lane("reference", warning=True, checksum_valid=False), "checksum_invalid"),
        (_lane("reference", warning=True, entries=0), "lane_empty"),
        (_lane("reference", warning=True, removed=0), "over_threshold_nothing_to_prune"),
    ],
    ids=["checksum_invalid", "entries_zero", "over_threshold_removed_zero"],
)
def test_e5_final_gate_failure_restores_without_enforce(
    lane: dict[str, Any],
    reason: str,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(reference=lane), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 1 and receipt["action"] == "gate_failed"
    assert [call["enforce"] for call in calls] == [False, False]
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["gate"]["phase"] == "final" and receipt["gate"]["passed"] is False
    assert {"lane": "reference", "reason": reason} in receipt["gate"]["failures"]


def test_e5_service_busy_again_at_the_gate_restores_without_enforce(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Idle for the wait's single poll, active again at the gate re-check.
    h.busy_after(SCHED_SERVICE, 1)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "service_busy_at_gate"
    assert [call["enforce"] for call in calls] == [False]
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


# --- 6. enforce errors --------------------------------------------------------


def test_e6_enforce_incomplete_restores_and_exits_3(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    partial = _enforced(destination=_lane("destination", warning=True, status="planned"))
    error = repair.RepairIncompleteError("provider_preimage_changed", {"field": "destination"}, summary=partial)
    _fake_repair(monkeypatch, h, _summary(), _summary(), error)

    code, receipt, _ = _run(capsys)

    assert code == 3 and receipt["action"] == "enforce_incomplete"
    assert receipt["error"]["reason"] == "provider_preimage_changed"
    assert receipt["enforce"]["lanes"]["reference"]["status"] == "repaired"
    assert receipt["enforce"]["lanes"]["destination"]["status"] == "planned"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


def test_e6_enforce_refusal_restores_and_exits_2(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = repair.RepairCliError("archive_root_unset", {"env": repair.ARCHIVE_ROOT_ENV})
    _fake_repair(monkeypatch, h, _summary(), _summary(), error)

    code, receipt, _ = _run(capsys)

    assert code == 2
    assert receipt["error"]["reason"] == "archive_root_unset"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


# --- 7. still over threshold after pruning ------------------------------------


def test_e7_capacity_still_warning_after_enforce_is_unprunable_exit_1(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    after = _enforced(reference=_lane("reference", warning=True, warning_after=True, status="repaired"))
    _fake_repair(monkeypatch, h, _summary(), _summary(), after)

    code, receipt, _ = _run(capsys)

    assert code == 1 and receipt["action"] == "enforced_unprunable"
    assert receipt["enforce"]["lanes"]["reference"]["capacity_after"]["warning"] is True
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


# --- 8. refresh timer already inactive ----------------------------------------


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_e8_inactive_refresh_timer_is_never_stopped_or_started(
    state: str, h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.set_state(REFRESH_TIMER, state)
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced"
    assert h.verbs("stop") == [SCHED_TIMER] and h.verbs("start") == [SCHED_TIMER]
    assert receipt["timers_stopped"] == [SCHED_TIMER] and receipt["timers_restored"] == [SCHED_TIMER]
    # The refresh service is still awaited even though its timer was left alone.
    assert REFRESH_SERVICE in h.verbs("is-active")


# --- 9. timer stop failure ----------------------------------------------------


@pytest.mark.parametrize(
    ("failing", "stops", "starts"),
    [
        (SCHED_TIMER, [SCHED_TIMER], [SCHED_TIMER]),
        (REFRESH_TIMER, [SCHED_TIMER, REFRESH_TIMER], [SCHED_TIMER, REFRESH_TIMER]),
    ],
    ids=["scheduler_stop_fails", "refresh_stop_fails"],
)
def test_e9_stop_failure_restores_what_was_registered_and_exits_2(
    failing: str,
    stops: list[str],
    starts: list[str],
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    h.fail("stop", failing)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "timer_stop_failed"
    assert h.verbs("stop") == stops and h.verbs("start") == starts
    assert [call["enforce"] for call in calls] == [False]


def test_e9_undeterminable_timer_state_touches_nothing_further(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.set_state(REFRESH_TIMER, "maintenance")
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "timer_state_undeterminable"
    assert h.verbs("stop") == [SCHED_TIMER] and h.verbs("start") == [SCHED_TIMER]


# --- 10. timer start failure --------------------------------------------------


def test_e10_restore_failure_exits_5_and_logs_the_manual_command(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.fail("start", SCHED_TIMER)
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, err = _run(capsys)

    assert code == 5 and receipt["exit_code"] == 5
    assert receipt["status"] == "timer_restore_failed"
    assert receipt["action"] == "enforced"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["timers_restored"] == [REFRESH_TIMER]
    assert f"systemctl --user start {SCHED_TIMER}" in err
    assert f"systemctl --user start {REFRESH_TIMER}" not in err


def test_e10_restore_failure_overrides_a_failure_exit_code(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.fail("start", REFRESH_TIMER)
    h.pgrep_codes(1, 0)
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 5 and receipt["action"] == "deferred_wrapper_running"


# --- 11 / 15 / 16. real signals in a subprocess -------------------------------


def _subprocess_run(h: Harness, tmp_path: Path, cfg: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(DRIVER.format(repo=str(REPO_ROOT))), encoding="utf-8")
    config = tmp_path / "driver.json"
    config.write_text(json.dumps({**cfg, "log": str(h.log)}), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "NHMS_NODE22_SYSTEMCTL": str(h.systemctl),
        "NHMS_STATE_INDEX_AUTO_PRUNE_PGREP": str(h.pgrep),
        "NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT": str(h.receipt_root),
        "NHMS_SCHEDULER_CYCLE_LAG_HOURS": "16",
    }
    return subprocess.run(
        [sys.executable, str(driver), str(config)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_e11_sigterm_while_waiting_restores_timers_and_exits_143(h: Harness, tmp_path: Path) -> None:
    h.busy(SCHED_SERVICE, "forever")

    result = _subprocess_run(h, tmp_path, {"outcomes": [_summary(), _summary(), _enforced()], "kill_on_sleep": 1})

    assert result.returncode == 128 + signal.SIGTERM, result.stderr
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert "repair enforce=True" not in h.events()
    receipt = h.latest()
    assert receipt["exit_code"] == 143 and receipt["signal"] == "SIGTERM"
    assert receipt["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]


def test_e15_sigterm_during_enforce_is_deferred_until_enforce_returns(h: Harness, tmp_path: Path) -> None:
    result = _subprocess_run(
        h, tmp_path, {"outcomes": [_summary(), _summary(), _enforced()], "kill_during_enforce": True}
    )

    assert result.returncode == 143, result.stderr
    events = h.events()
    assert events.index("repair enforce returned") < events.index(f"systemctl start {SCHED_TIMER}")
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    receipt = h.latest()
    assert receipt["action"] == "enforced" and receipt["signal"] == "SIGTERM"
    assert receipt["exit_code"] == 143
    assert receipt["enforce"]["lanes"]["reference"]["status"] == "repaired"


def test_e16_second_sigterm_during_restore_cannot_skip_a_timer(h: Harness, tmp_path: Path) -> None:
    h.busy(SCHED_SERVICE, "forever")
    (h.dir / "kill-on-start").write_text("", encoding="utf-8")

    result = _subprocess_run(h, tmp_path, {"outcomes": [_summary(), _summary(), _enforced()], "kill_on_sleep": 1})

    assert result.returncode == 143, result.stderr
    assert not (h.dir / "kill-on-start").exists(), "the fake never sent the second signal"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert h.latest()["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]


def test_e16_first_sigterm_during_restore_after_a_normal_run_cannot_skip_a_timer(h: Harness, tmp_path: Path) -> None:
    # No earlier signal. `run.deferring` is still True after enforce, so even
    # without the restore's SIG_IGN the handler would not raise here; what this
    # pins is that the signal is ignored (not recorded) once the restore began.
    # The failing-gate variant below has `deferring` False.
    (h.dir / "kill-on-start").write_text("", encoding="utf-8")

    result = _subprocess_run(h, tmp_path, {"outcomes": [_summary(), _summary(), _enforced()]})

    assert not (h.dir / "kill-on-start").exists(), "the fake never sent the signal"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    receipt = h.latest()
    assert receipt["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]
    # Ignored by design while restoring: the run's own outcome stands.
    assert result.returncode == 0 and receipt["action"] == "enforced" and receipt["signal"] is None, result.stderr


def test_e16_first_sigterm_during_restore_after_a_failed_gate_cannot_skip_a_timer(h: Harness, tmp_path: Path) -> None:
    # Enforce never started, so `run.deferring` is False. What this pins is
    # that a signal during the restore is ignored, not recorded: the run's own
    # outcome (exit 1, no `signal`) stands and both timers are started.
    (h.dir / "kill-on-start").write_text("", encoding="utf-8")
    bad = _summary(reference=_lane("reference", warning=True, checksum_valid=False))

    result = _subprocess_run(h, tmp_path, {"outcomes": [_summary(), bad, _enforced()]})

    assert not (h.dir / "kill-on-start").exists(), "the fake never sent the signal"
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert "repair enforce=True" not in h.events()
    receipt = h.latest()
    assert receipt["timers_restored"] == [SCHED_TIMER, REFRESH_TIMER]
    assert result.returncode == 1 and receipt["action"] == "gate_failed" and receipt["signal"] is None, result.stderr


# --- 20. a signal at the restore boundary -------------------------------------


def _kill_once_in(monkeypatch: pytest.MonkeyPatch, name: str) -> list[bool]:
    """Wrap ``auto.<name>`` so its first call sends SIGTERM to this process first.

    ``os.kill`` runs the Python-level handler before it returns, so the signal
    lands exactly inside that call. The one-shot flag is set before the kill:
    the handler may call the same function again.
    """
    real = getattr(auto, name)
    fired = [False]

    def wrapped(*args: Any) -> Any:
        if not fired[0]:
            fired[0] = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real(*args)

    monkeypatch.setattr(auto, name, wrapped)
    return fired


@pytest.mark.parametrize(("fail_start", "rc"), [(None, 143), (SCHED_TIMER, 5)], ids=["restored", "start_fails"])
def test_e20_sigterm_entering_the_restore_still_restores_and_writes_the_receipt(
    fail_start: str | None,
    rc: int,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Post-freeze gate failure: the flow returned, the restore is next.
    if fail_start:
        h.fail("start", fail_start)
    bad = _summary(reference=_lane("reference", warning=True, checksum_valid=False))
    _fake_repair(monkeypatch, h, _summary(), bad, _enforced())
    fired = _kill_once_in(monkeypatch, "_ignore_signals")

    code, receipt, _ = _run(capsys)

    assert fired == [True]
    assert code == rc and receipt["exit_code"] == rc
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["signal"] == "SIGTERM" and receipt["action"] == "gate_failed"
    assert h.latest() == receipt


@pytest.mark.parametrize(("fail_start", "rc"), [(None, 143), (SCHED_TIMER, 5)], ids=["restored", "start_fails"])
def test_e20_sigterm_inside_an_error_branch_still_restores_and_writes_the_receipt(
    fail_start: str | None,
    rc: int,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Post-freeze unexpected error: the signal lands while main classifies it.
    if fail_start:
        h.fail("start", fail_start)
    _fake_repair(monkeypatch, h, _summary(), RuntimeError("boom"), _enforced())
    fired = _kill_once_in(monkeypatch, "_unexpected")

    code, receipt, _ = _run(capsys)

    assert fired == [True]
    assert code == rc and receipt["exit_code"] == rc
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert receipt["signal"] == "SIGTERM" and receipt["action"] == "interrupted"
    assert h.latest() == receipt


@pytest.mark.parametrize(
    ("cfg", "kill_on_stop", "stops"),
    [
        ({"kill_on_dry_run": 2}, False, [SCHED_TIMER, REFRESH_TIMER]),
        ({}, True, [SCHED_TIMER]),
    ],
    ids=["final_gate_dry_run", "first_stop"],
)
def test_e20_real_sigterm_after_freezing_restores_exactly_the_registered_timers(
    cfg: dict[str, Any], kill_on_stop: bool, stops: list[str], h: Harness, tmp_path: Path
) -> None:
    if kill_on_stop:
        (h.dir / "kill-on-stop").write_text("", encoding="utf-8")

    result = _subprocess_run(h, tmp_path, {"outcomes": [_summary(), _summary(), _enforced()], **cfg})

    assert result.returncode == 143, result.stderr
    assert not (h.dir / "kill-on-stop").exists(), "the fake never sent the signal"
    assert h.verbs("stop") == stops and h.verbs("start") == stops
    assert "repair enforce=True" not in h.events()
    receipt = h.latest()
    assert receipt["exit_code"] == 143 and receipt["signal"] == "SIGTERM"
    assert receipt["timers_stopped"] == stops and receipt["timers_restored"] == stops


# --- 12. undeterminable service state -----------------------------------------


@pytest.mark.parametrize("state", ["", "bogus"], ids=["empty", "unknown_value"])
def test_e12_undeterminable_service_state_restores_and_exits_2(
    state: str, h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.set_state(SCHED_SERVICE, state)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "service_state_undeterminable"
    assert [call["enforce"] for call in calls] == [False]
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]


# --- 13. receipt files --------------------------------------------------------


def test_e13_receipt_is_private_bounded_to_30_and_has_no_id_lists(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = [f"2026010{day}T000000Z.json" for day in range(1, 10)]
    stale += [f"202601{day:02d}T000000Z.json" for day in range(10, 32)]
    assert len(stale) == 31  # plus this run = 32 -> the two oldest go
    for name in stale:
        (h.receipt_root / name).write_text("{}\n", encoding="utf-8")
    (h.receipt_root / "operator-note.txt").write_text("keep\n", encoding="utf-8")
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 0
    stamped = sorted(path.name for path in h.receipt_root.iterdir() if re.match(r"^\d{8}T\d{6}Z\.json$", path.name))
    assert len(stamped) == 30
    assert stamped[0] == stale[2] and stale[0] not in stamped and stale[1] not in stamped
    assert (h.receipt_root / "operator-note.txt").exists()
    assert json.loads((h.receipt_root / stamped[-1]).read_text(encoding="utf-8")) == receipt == h.latest()
    assert stat.S_IMODE(h.receipt_root.stat().st_mode) == 0o700
    for name in (stamped[-1], "latest.json"):
        assert stat.S_IMODE((h.receipt_root / name).stat().st_mode) == 0o600
    assert "removed_state_ids" not in _keys(receipt) and "groups" not in _keys(receipt)
    assert len((h.receipt_root / "latest.json").read_bytes()) <= 1024 * 1024


@pytest.mark.parametrize("mode", [0o755, 0o750])
def test_e13_non_private_receipt_root_refuses_before_any_timer_or_repair(
    mode: int, h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chmod(h.receipt_root, mode)
    calls = _fake_repair(monkeypatch, h, _summary())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "receipt_root_not_private"
    assert calls == [] and h.events() == []
    assert list(h.receipt_root.iterdir()) == []


def test_e13_missing_receipt_root_is_never_created(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = h.receipt_root.parent / "absent"
    monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT", str(missing))
    calls = _fake_repair(monkeypatch, h, _summary())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "receipt_root_missing"
    assert calls == [] and not missing.exists()


def test_e13_receipt_write_failure_turns_only_a_zero_exit_into_2(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auto, "_write_receipt", broken)
    _fake_repair(monkeypatch, h, _below())
    code, receipt, err = _run(capsys)
    assert code == 2 and receipt["exit_code"] == 0 and receipt["action"] == "skip"
    assert "receipt_write_failed" in err

    h.pgrep_codes(0)
    _fake_repair(monkeypatch, h, _summary())
    code, receipt, _ = _run(capsys)
    assert code == 1 and receipt["action"] == "deferred_wrapper_running"


# --- 14. unit entrypoint invariant and budgets --------------------------------


def _directives(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))]


def _value(lines: list[str], key: str) -> str:
    values = [line.split("=", 1)[1] for line in lines if line.split("=", 1)[0] == key]
    assert len(values) == 1, (key, values)
    return values[0]


def test_e14_unit_pins_entrypoint_schedule_and_timeout_budgets() -> None:
    service = _directives((REPO_ROOT / SERVICE_UNIT).read_text(encoding="utf-8"))
    timer = _directives((REPO_ROOT / TIMER_UNIT).read_text(encoding="utf-8"))

    assert [line for line in service if line.startswith("ExecStart=")] == [
        f"ExecStart={NODE22_VENV_PY} -m scripts.node22_state_index_auto_prune"
    ]
    assert _value(service, "Type") == "oneshot"
    assert _value(service, "WorkingDirectory") == NODE22_ACTIVE
    assert _value(service, "EnvironmentFile") == f"{NODE22_ACTIVE}/infra/env/compute.scheduler-dbfree.env"
    assert _value(service, "KillSignal") == "SIGTERM"
    assert _value(service, "OnFailure") == "nhms-node22-unit-failure-alert@%n.service"
    environment = {line.split("=", 2)[1]: line.split("=", 2)[2] for line in service if line.startswith("Environment=")}
    prefix = "/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair"
    assert environment["NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT"] == f"{prefix}/auto-prune"
    assert environment[repair.ARCHIVE_ROOT_ENV] == f"{prefix}/archives"
    assert environment[repair.RECEIPT_ROOT_ENV] == f"{prefix}/receipts"
    assert re.search(r"(?<![./])python3?(?:\s|$)", "\n".join(service)) is None
    assert "uv" not in "\n".join(service).lower()

    assert _value(timer, "OnCalendar") == "*-*-* 06:30:00 UTC"
    assert _value(timer, "RandomizedDelaySec") == "15m"
    assert _value(timer, "Persistent") == "true"
    assert _value(timer, "Unit") == "nhms-scheduler-state-index-prune.service"
    assert _value(timer, "WantedBy") == "default.target"

    assert auto.SUBPROCESS_TIMEOUT_SECONDS == 60 and auto.ENFORCE_STOP_BUDGET_SECONDS == 60
    assert auto.DEFAULT_WAIT_SECONDS == 18000 and auto.DEFAULT_POLL_SECONDS == 30
    from scripts import node22_refresh_timer_health as timer_health

    assert auto.DEFAULT_WAIT_SECONDS < timer_health.DEFAULT_STOPPED_DWELL_HOURS * 3600
    assert int(_value(service, "TimeoutStartSec")) > auto.DEFAULT_WAIT_SECONDS
    budget = auto.ENFORCE_STOP_BUDGET_SECONDS + 2 * auto.SUBPROCESS_TIMEOUT_SECONDS + 10
    assert int(_value(service, "TimeoutStopSec")) >= budget


def test_e14_module_help_runs_through_the_import_chain() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.node22_state_index_auto_prune", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "prune-retention" in result.stdout


def test_wrapper_pattern_is_the_runbook_anchor_and_matches_only_the_wrapper() -> None:
    runbook = (REPO_ROOT / "docs/runbooks/production-ops/known-issues-state-index.md").read_text(encoding="utf-8")

    assert auto.WRAPPER_PGREP_PATTERN == RUNBOOK_WRAPPER_PATTERN
    assert RUNBOOK_WRAPPER_PATTERN in runbook
    pattern = re.compile(auto.WRAPPER_PGREP_PATTERN)
    assert pattern.search("/usr/bin/bash scripts/ops/node22-run-cycle-once.sh --cycle-time 2026-11-11T00:00:00Z")
    assert pattern.search("bash -x /scratch/frd_muziyao/NWM/scripts/ops/node22-run-cycle-once.sh")
    assert not pattern.search("vim scripts/ops/node22-run-cycle-once.sh")
    assert not pattern.search(f"{NODE22_VENV_PY} -m scripts.node22_state_index_auto_prune")


# --- 17. pgrep undeterminable -------------------------------------------------


@pytest.mark.parametrize("outcome", [2, 3, "timeout", "missing"])
def test_e17_undeterminable_wrapper_check_fails_closed_without_touching_timers(
    outcome: int | str,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if outcome == "missing":
        monkeypatch.setenv("NHMS_STATE_INDEX_AUTO_PRUNE_PGREP", str(h.dir / "no-such-pgrep"))
    else:
        h.pgrep_codes(outcome)
    monkeypatch.setattr(auto, "SUBPROCESS_TIMEOUT_SECONDS", 0.5)
    _fake_repair(monkeypatch, h, _summary())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["reason"] == "wrapper_state_undeterminable"
    assert [event for event in h.events() if event.startswith("systemctl")] == []


# --- 18. queued job keeps the wait going --------------------------------------


@pytest.mark.parametrize("service", [SCHED_SERVICE, REFRESH_SERVICE])
def test_e18_queued_job_on_an_inactive_service_keeps_waiting(
    service: str, h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h.queued(service, 2)
    calls = _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced"
    assert h.key_events() == FULL_ORDER
    assert [call["enforce"] for call in calls] == [False, False, True]
    assert receipt["waited_seconds"] == 2 * auto.DEFAULT_POLL_SECONDS


# --- 19. precheck gate --------------------------------------------------------


def test_e19_over_threshold_lane_with_nothing_to_prune_fails_precheck_without_timers(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_repair(monkeypatch, h, _summary(destination=_lane("destination", warning=True, removed=0)))

    code, receipt, _ = _run(capsys)

    assert code == 1 and receipt["action"] == "gate_failed_precheck"
    assert receipt["gate"]["phase"] == "precheck"
    assert {"lane": "destination", "reason": "over_threshold_nothing_to_prune"} in receipt["gate"]["failures"]
    assert [call["enforce"] for call in calls] == [False]
    assert h.events() == ["repair enforce=False"]


def test_e19_lane_under_threshold_may_have_nothing_to_prune(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = _summary(destination=_lane("destination", warning=False, removed=0))
    calls = _fake_repair(monkeypatch, h, summary, summary, _enforced())

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced"
    assert [call["enforce"] for call in calls] == [False, False, True]
    assert receipt["gate"]["passed"] is True


# --- end to end over the real repair -----------------------------------------


def test_real_repair_dry_run_gate_and_enforce_prune_both_lanes(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only systemctl/pgrep are faked: the summary shape the gate reads is the repair's own.

    The #2548 fixture prunes reference 81 -> 24 and destination 71 -> 24. Real
    indexes this small are far below 0.70, so the warning threshold is moved
    between the measured before/after ratios to put both lanes over it.
    """
    lanes = Lanes(tmp_path / "lanes")
    entries = _prunable(lanes)
    lanes.publish(entries, destination=entries[10:])
    lanes.apply_env(monkeypatch)
    assert repair.main(["prune-retention"]) == 0
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["lanes"]
    before = min(preview[name]["retention"]["capacity_before"]["utilization_ratio"] for name in preview)
    after = max(preview[name]["retention"]["capacity_after"]["utilization_ratio"] for name in preview)
    assert after < before
    threshold = (before + after) / 2
    monkeypatch.setattr(state_manager, "STATE_INDEX_CAPACITY_WARNING_THRESHOLD", threshold)

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "enforced", receipt
    assert receipt["warning_threshold"] == threshold
    assert receipt["gate"]["passed"] is True
    assert receipt["gate"]["lanes"]["reference"]["removed_count"] == 57
    assert receipt["gate"]["lanes"]["destination"]["removed_count"] == 47
    for name in ("reference", "destination"):
        lane = receipt["enforce"]["lanes"][name]
        assert lane["status"] == "repaired" and lane["digest_matches_gate"] is True
        assert lane["capacity_after"]["entry_count"] == 24 and lane["capacity_after"]["warning"] is False
        assert lane["removed_state_ids_sha256"] == preview[name]["retention"]["removed_state_ids_sha256"]
    assert len(lanes.entries("reference")) == 24 and len(lanes.entries("destination")) == 24
    assert list(lanes.archive_root.iterdir()) and list(lanes.receipt_root.iterdir())
    assert h.verbs("stop") == [SCHED_TIMER, REFRESH_TIMER]
    assert h.verbs("start") == [SCHED_TIMER, REFRESH_TIMER]
    assert "removed_state_ids" not in _keys(receipt) and "groups" not in _keys(receipt)


# --- config and dry-run refusals (no timer touched) ---------------------------


def test_dry_run_refusal_exits_2_without_touching_timers(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_repair(monkeypatch, h, repair.RepairCliError("repair_cycle_lag_unset", {"env": repair.CYCLE_LAG_HOURS_ENV}))

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"] == {"reason": "repair_cycle_lag_unset", "field": repair.CYCLE_LAG_HOURS_ENV}
    assert h.events() == ["repair enforce=False"]


def test_preimage_race_is_retried_then_proceeds(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    race = repair.RepairCliError("provider_preimage_changed", {"field": "destination"})
    calls = _fake_repair(monkeypatch, h, race, _below())

    code, receipt, _ = _run(capsys)

    assert code == 0 and receipt["action"] == "skip"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS", "5h"),
        ("NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS", "-1"),
        ("NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS", "0"),
        ("NHMS_STATE_INDEX_AUTO_PRUNE_POLL_SECONDS", "x"),
    ],
)
def test_invalid_wait_config_refuses_before_any_timer_or_repair(
    env: str, value: str, h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(env, value)
    calls = _fake_repair(monkeypatch, h, _summary())

    code, receipt, _ = _run(capsys)

    assert code == 2 and receipt["error"]["field"] == env
    assert calls == [] and h.events() == []


def test_main_leaves_the_callers_signal_handlers_in_place(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    _fake_repair(monkeypatch, h, _summary(), _summary(), _enforced())

    code, _, _ = _run(capsys)

    assert code == 0
    assert {sig: signal.getsignal(sig) for sig in before} == before
