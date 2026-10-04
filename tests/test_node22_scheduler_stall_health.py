"""Behaviour pins for the node-22 DB-free scheduler stall probe (issue #2570).

Every test drives the probe through a fake ``systemctl`` shim that records each
invocation, a pinned clock (``--now``), a real on-disk evidence tree under
``tmp_path`` and a temporary receipt root.  No real systemd, no Slurm, no
database, no production path.

Honest oracle statement: this module is new, so "red on master" would only mean
"the file does not exist".  The oracle is the behaviour table below --- the
eleven non-healthy verdicts, the precedence between them, the four pass shapes,
the ordering rules and the fail-closed rules --- each of which is falsifiable
against a plausible wrong implementation and several of which were built to red
on a specific one (lexical ordering, truncate-before-exclude, a status
allowlist for neutral, ``ActiveState == "active"`` as the liveness test).

Test anchors are the task numbers of
``openspec/changes/node22-scheduler-stall-probe/tasks.md``.

Issue #2662 adds the ``i``-series at the end of the module (OpenSpec change
``scheduler-held-exits-and-stall-probe``, tasks 3.1-3.4): the in-flight-held
pass shape, the in-flight time gate and the frontier suppression bypass.  Its
freeze fixture is a RECONSTRUCTION, not a replay.  The real pass artifacts of
the #2655 freeze (2026-09-25T16:45Z onwards) and the tracker of that moment
were deleted by evidence retention before this change was written, so the
fixture is rebuilt from the values measured on node-22 and recorded in the
issue body: 96 candidates per pass, 94 skipped as ``active_duplicate_pipeline``
and 2 as ``terminal_hydro_success``, all at cycle 2026-09-25T00Z for sources
``gfs`` and ``IFS``, zero blocked, zero submitted, ``status=planned`` with a
progress guard; the last submitting pass ``scheduler_2026092516_1436f9714b94``
started 2026-09-25T16:45:42Z; 340 zero-submission passes followed until the
probe receipt of 2026-09-27T03:18:31Z; and the four tracker entries with their
measured counts.  The row shape is the one the real writer produces
(``{**SchedulerCandidate.to_dict(), "reason": ...}``), reduced to the keys the
writer's own summary tier keeps.  Individual pass start times between the two
measured instants are evenly spaced, which is an assumption.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node22_scheduler_stall_health as probe

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
PROBE_SOURCE = Path(probe.__file__)
UNIT_DIR = Path(__file__).resolve().parents[1] / "infra" / "systemd"
SERVICE_UNIT = UNIT_DIR / "nhms-node22-scheduler-stall-health.service"
TIMER_UNIT = UNIT_DIR / "nhms-node22-scheduler-stall-health.timer"
RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "runbooks" / "production-ops" / "stuck-detection.md"

# The verbs the probe must never contain, matched on word boundaries so the
# verdict names (`timer_stopped`, `timer_not_enabled`) and the artifact field
# `started_at` are not false positives.
MUTATION_VERBS = re.compile(
    r"\b(start|stop|enable|disable|restart|reload|daemon-reload)\b", re.IGNORECASE
)

# The libpq selector set the node-22 units clear.  Written out literally rather
# than read from the precedent probe's unit file: reading that path from this
# suite would create a reader edge `infra/systemd/nhms-node22-refresh-timer-
# health.service` -> this suite that `scripts/select_ci_tests.py` does not
# route, and that file's rule is pinned to an exact target set.
PG_ENVIRONMENT_VARIABLES = (
    "DATABASE_URL",
    "PIPELINE_DATABASE_URL",
    "PGAPPNAME",
    "PGCHANNELBINDING",
    "PGCLIENTENCODING",
    "PGCONNECT_TIMEOUT",
    "PGDATABASE",
    "PGDATESTYLE",
    "PGGEQO",
    "PGGSSDELEGATION",
    "PGGSSENCMODE",
    "PGGSSLIB",
    "PGHOST",
    "PGHOSTADDR",
    "PGKRBSRVNAME",
    "PGLOADBALANCEHOSTS",
    "PGLOCALEDIR",
    "PGMAXPROTOCOLVERSION",
    "PGMINPROTOCOLVERSION",
    "PGOPTIONS",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGREQUIREAUTH",
    "PGREQUIREPEER",
    "PGREQUIRESSL",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSSLCERT",
    "PGSSLCERTMODE",
    "PGSSLCOMPRESSION",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLKEY",
    "PGSSLMAXPROTOCOLVERSION",
    "PGSSLMINPROTOCOLVERSION",
    "PGSSLMODE",
    "PGSSLNEGOTIATION",
    "PGSSLROOTCERT",
    "PGSSLSNI",
    "PGSSL_CERT_FILE",
    "PGSSL_KEY_FILE",
    "PGSSL_ROOT_CERT_FILE",
    "PGSYSCONFDIR",
    "PGTARGETSESSIONATTRS",
    "PGTZ",
    "PGUSER",
)

# Spelled as a literal rather than read from the probe: the name is part of
# the operator contract (a drop-in spells it), so a rename must red here.
ENV_IN_FLIGHT_MINUTES = "NHMS_SCHEDULER_STALL_IN_FLIGHT_MINUTES"

# Small thresholds keep the fixtures readable.  `SCAN_LIMIT` obeys the probe's
# own config rule, `SCAN_LIMIT - HOUR_BUCKET_MARGIN > max(no_submission, lock)`:
# 16 - 12 = 4 > 3, the smallest scan the rule admits for these thresholds.
BASE_CONFIG = {
    probe.ENV_NO_SUBMISSION_PASSES: "3",
    probe.ENV_LOCK_PASSES: "2",
    probe.ENV_CIRCUIT_PASSES: "3",
    probe.ENV_SCAN_LIMIT: "16",
    probe.ENV_LIMIT_LOOKBACK_MINUTES: "120",
    probe.ENV_MAX_TRIGGER_AGE_MINUTES: "360",
    probe.ENV_MAX_PASS_AGE_MINUTES: "360",
    # #2662: the in-flight time gate.  It is bounded by the scan window ---
    # (16 - 12 - 1) x 5 = 15 minutes is the largest gate this scan admits ---
    # and the pre-#2662 fixtures in this module space their passes ten
    # minutes apart in runs short enough to stay under it.
    ENV_IN_FLIGHT_MINUTES: "15",
}

ALL_ENV_NAMES = (
    probe.ENV_TIMER_UNIT,
    probe.ENV_SERVICE_UNIT,
    probe.ENV_SYSTEMCTL,
    probe.ENV_EVIDENCE_ROOT,
    probe.ENV_RECEIPT_ROOT,
    probe.ENV_MAX_TRIGGER_AGE_MINUTES,
    probe.ENV_MAX_PASS_AGE_MINUTES,
    probe.ENV_LIMIT_LOOKBACK_MINUTES,
    probe.ENV_LOCK_PASSES,
    probe.ENV_NO_SUBMISSION_PASSES,
    probe.ENV_CIRCUIT_PASSES,
    ENV_IN_FLIGHT_MINUTES,
    probe.ENV_SUPPRESSED_REASONS,
    probe.ENV_SCAN_LIMIT,
    probe.ENV_MAX_ENTRIES_SCANNED,
    probe.ENV_JSON,
)

SUPPRESSED_REASON = "ambiguous_fallback_match:comment_accounting_unproven"


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _systemd_timestamp(moment: datetime) -> str:
    """Render an instant the way ``systemctl show`` does.

    Built in the *local* zone so the string round-trips through the probe's
    parser on any host, exactly as it does on node-22.
    """

    return moment.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")


def _render(properties: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n"


def _timer_properties(
    *,
    unit_file_state: str = "enabled",
    active_state: str = "active",
    sub_state: str = "waiting",
    last_trigger: str | None = None,
) -> dict[str, str]:
    return {
        "UnitFileState": unit_file_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "LastTriggerUSec": (
            _systemd_timestamp(NOW - timedelta(minutes=6)) if last_trigger is None else last_trigger
        ),
    }


def _service_properties(
    *,
    unit_file_state: str = "static",
    active_state: str = "inactive",
    sub_state: str = "dead",
    result: str = "success",
) -> dict[str, str]:
    """The idle geometry measured on node-22 between passes."""

    return {
        "UnitFileState": unit_file_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "Result": result,
    }


def _in_flight_service_properties() -> dict[str, str]:
    """The MEASURED in-flight geometry of the `Type=oneshot` scheduler.

    Not `active`: a oneshot never reaches that state while its ExecStart runs.
    Building a fixture with `ActiveState=active` here would rubber-stamp the
    exact bug the liveness gate exists to prevent.
    """

    return _service_properties(active_state="activating", sub_state="start")


def _write_fake_systemctl(
    tmp_path: Path,
    *,
    timer_properties: dict[str, str],
    service_properties: dict[str, str],
    exit_code: int = 0,
    timer_text: str | None = None,
    service_text: str | None = None,
) -> tuple[Path, Path]:
    """Create a fake ``systemctl`` that logs every invocation it receives.

    It dispatches on the unit-name argument, so the timer query and the service
    query get their own payloads, and it records the full argument vector so a
    test can prove only read-only subcommands were ever used.
    """

    log = tmp_path / "systemctl.log"
    timer_payload = tmp_path / "timer-show.txt"
    service_payload = tmp_path / "service-show.txt"
    timer_payload.write_text(timer_text if timer_text is not None else _render(timer_properties))
    service_payload.write_text(
        service_text if service_text is not None else _render(service_properties)
    )
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"if [ {exit_code} -ne 0 ]; then exit {exit_code}; fi\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        f"    *.timer) cat {timer_payload}; exit 0 ;;\n"
        f"    *.service) cat {service_payload}; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _pass_payload(
    *,
    started_at: datetime,
    status: str = "planned",
    submitted: int = 0,
    blocked: int = 0,
    guard: bool = True,
    counts: bool = True,
    pass_id: str = "scheduler_2026092312_000000000000",
    skipped: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a terminal pass artifact with the shape the writer produces.

    ``skipped`` are ``skipped_candidates`` rows (see ``_skip_row``); they are
    disjoint from the blocked candidates and counted in ``candidate_count``,
    exactly as ``scheduler_runtime`` totals the three lists.
    """

    payload: dict[str, object] = {
        "schema_version": "nhms.production_scheduler.pass_evidence.v1",
        "pass_id": pass_id,
        "status": status,
        "started_at": started_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    if counts:
        payload["counts"] = {
            "candidate_count": submitted + blocked + len(skipped or []),
            "blocked_candidate_count": blocked,
            "submitted_count": submitted,
            "skipped_candidate_count": len(skipped or []),
        }
    else:
        # The measured degraded shape: the `counts` block exists but the two
        # graded keys are absent, which is exactly the resource-limit fallback.
        payload["counts"] = {"candidate_count": 0}
    if guard:
        payload["progress_guard"] = {"status": "passed", "max_no_progress_steps": 256}
    if skipped is not None:
        payload["candidates"] = []
        payload["blocked_candidates"] = []
        payload["skipped_candidates"] = skipped
    return payload


def _skip_row(reason: str, source: str, cycle: datetime, *, basin: str = "basins_qhh") -> dict[str, object]:
    """One ``skipped_candidates`` row: ``{**SchedulerCandidate.to_dict(), "reason": reason}``.

    Reduced to the keys the writer's summary tier keeps
    (``scheduler_evidence_payload._BOUNDED_CANDIDATE_SUMMARY_KEYS``), which is
    also every key the probe reads.  ``source`` and ``source_id`` are the same
    configured id and both cycle keys carry the same ``...Z`` string.
    """

    cycle_text = cycle.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "candidate_id": f"{source}:{cycle_text}:{basin}",
        "source": source,
        "source_id": source,
        "cycle_time": cycle_text,
        "cycle_time_utc": cycle_text,
        "basin_id": basin,
        "model_id": f"{basin}_shud",
        "status": "skipped",
        "reason": reason,
    }


def _pass_name(minutes_ago: int, suffix: str, *, pre_execution: bool = False) -> str:
    cycle = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y%m%d%H")
    extension = ".pre_execution.json" if pre_execution else ".json"
    return f"scheduler_{cycle}_{suffix}{extension}"


def _write_pass(root: Path, name: str, payload: dict[str, object] | None, *, raw: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(raw if raw is not None else json.dumps(payload))
    return path


def _evidence_root(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _healthy_passes(root: Path, count: int = 3) -> None:
    """A handful of ordinary passes: recent, submitting, nothing blocked."""

    for index in range(count):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes), status="submitted", submitted=2
            ),
        )


def _write_tracker(
    root: Path,
    entries: list[dict[str, object]],
    *,
    schema_version: str = probe.TRACKER_SCHEMA_VERSION,
    raw: str | None = None,
) -> Path:
    path = root / probe.TRACKER_FILENAME
    if raw is not None:
        path.write_text(raw)
        return path
    path.write_text(json.dumps({"schema_version": schema_version, "entries": entries}))
    return path


def _tracker_entry(
    *,
    reason: str,
    consecutive_passes: int,
    subject_kind: str = "candidate",
    subject_id: str = "gfs:2026-09-23T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "adapter": subject_kind,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "reason": reason,
        "consecutive_passes": consecutive_passes,
        "first_pass_id": "scheduler_2026092300_000000000000",
        "last_pass_id": "scheduler_2026092312_000000000000",
    }


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    evidence_root: Path | None = None,
    receipt_root: Path | None = None,
    timer_properties: dict[str, str] | None = None,
    service_properties: dict[str, str] | None = None,
    systemctl_exit_code: int = 0,
    systemctl_path: str | None = None,
    timer_text: str | None = None,
    service_text: str | None = None,
    config: dict[str, str] | None = None,
    now: datetime | None = NOW,
    extra_argv: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Run the probe end-to-end; return (exit status, receipt root, call log)."""

    script, log = _write_fake_systemctl(
        tmp_path,
        timer_properties=timer_properties if timer_properties is not None else _timer_properties(),
        service_properties=(
            service_properties if service_properties is not None else _service_properties()
        ),
        exit_code=systemctl_exit_code,
        timer_text=timer_text,
        service_text=service_text,
    )
    for name in ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        probe.ENV_SYSTEMCTL, systemctl_path if systemctl_path is not None else str(script)
    )
    root = evidence_root if evidence_root is not None else _evidence_root(tmp_path)
    receipts = receipt_root if receipt_root is not None else tmp_path / "stall-receipts"
    monkeypatch.setenv(probe.ENV_EVIDENCE_ROOT, str(root))
    monkeypatch.setenv(probe.ENV_RECEIPT_ROOT, str(receipts))
    for key, value in {**BASE_CONFIG, **(config or {})}.items():
        monkeypatch.setenv(key, value)
    argv = [
        *([] if now is None else ["--now", now.isoformat().replace("+00:00", "Z")]),
        *(extra_argv or []),
    ]
    return probe.main(argv), receipts, log


def _receipt(root: Path) -> dict:
    return json.loads((root / "latest.json").read_text())


def _verdict(root: Path) -> str:
    return _receipt(root)["verdict"]


# ---------------------------------------------------------------------------
# 3.1 -- the twelve verdicts, each with its exit status
# ---------------------------------------------------------------------------


def test_v12_a_healthy_lane_is_ok_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 0
    assert _verdict(receipts) == "ok"


def test_v2_a_timer_that_is_not_enabled_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(unit_file_state="disabled"),
    )

    assert status == 1
    assert _verdict(receipts) == "timer_not_enabled"


def test_v3_an_inactive_timer_with_an_idle_service_is_timer_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(active_state="inactive", sub_state="dead"),
    )

    assert status == 1
    assert _verdict(receipts) == "timer_stopped"


def test_v4_a_non_success_result_is_scheduler_service_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.4b -- fail closed on ANY non-success result, no failure allowlist."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        service_properties=_service_properties(result="exit-code"),
    )

    assert status == 1
    assert _verdict(receipts) == "scheduler_service_failed"


@pytest.mark.parametrize("result", ["exit-code", "signal", "timeout", "core-dump", "resources", "oops"])
def test_v4_every_non_success_result_value_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    """A value-by-value pin: an allowlist implementation reds on `oops`."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        service_properties=_service_properties(result=result),
    )

    assert status == 1
    assert _verdict(receipts) == "scheduler_service_failed"


def test_v5_an_overdue_last_trigger_with_an_idle_service_is_not_triggering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(
            last_trigger=_systemd_timestamp(NOW - timedelta(minutes=400))
        ),
    )

    assert status == 1
    assert _verdict(receipts) == "scheduler_not_triggering"


def test_v6_an_evidence_root_with_no_terminal_pass_is_evidence_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from `evidence_stale`: nothing has ever been written here."""

    root = _evidence_root(tmp_path)
    _write_tracker(root, [])
    _write_pass(
        root,
        _pass_name(5, "0" * 12, pre_execution=True),
        _pass_payload(started_at=NOW - timedelta(minutes=5)),
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "evidence_unavailable"


def test_v7_healthy_units_with_an_old_newest_artifact_is_evidence_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(400, "0" * 12),
        _pass_payload(started_at=NOW - timedelta(minutes=400), status="submitted", submitted=1),
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "evidence_stale"


def test_v8_a_resource_limit_pass_in_the_window_is_pass_limit_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_pass(
        root,
        _pass_name(40, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=40), status="resource_limit_blocked", counts=False
        ),
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "pass_limit_blocked"


def test_v9_a_run_of_lock_contended_passes_is_its_own_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(2):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes), status="lock_contended", counts=False
            ),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "lock_contended_persistent"


def test_v10_sustained_blocked_work_is_submission_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(3):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "submission_stalled"


def test_v11_an_unsuppressed_tracker_entry_opens_the_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [_tracker_entry(reason="blocked:predecessor_pending", consecutive_passes=3)])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "no_progress_circuit_open"


def test_v1_an_unreadable_systemd_query_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, systemctl_exit_code=7
    )

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


# ---------------------------------------------------------------------------
# 3.2 -- precedence
# ---------------------------------------------------------------------------


def test_p1_a_dead_lane_outranks_a_resource_limit_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_pass(
        root,
        _pass_name(10, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=10), status="resource_limit_blocked", counts=False
        ),
    )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(active_state="inactive", sub_state="dead"),
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "timer_stopped"
    # The outranked observation stays readable.
    assert receipt["signals"]["resource_limit_passes_in_window"] == 1


def test_p2_the_resource_limit_verdict_outranks_the_open_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_pass(
        root,
        _pass_name(10, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=10), status="resource_limit_blocked", counts=False
        ),
    )
    _write_tracker(root, [_tracker_entry(reason="blocked:predecessor_pending", consecutive_passes=9)])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "pass_limit_blocked"
    assert receipt["signals"]["circuit_open_entries"] == 1


def test_p3_stale_evidence_outranks_the_submission_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(3):
        minutes = 400 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "evidence_stale"
    assert receipt["signals"]["no_submission_streak"] == 3


# ---------------------------------------------------------------------------
# 3.3 / 3.4 -- a pass in flight is never graded as a dead lane
# ---------------------------------------------------------------------------


def test_l1_a_long_pass_in_flight_is_not_graded_as_a_stopped_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured in-flight geometry, with BOTH backstops already overdue.

    A `Type=oneshot` reports `activating` while its ExecStart runs, so an
    implementation that tests `ActiveState == "active"` grades this healthy
    193-minute pass as three separate failures at once.
    """

    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(400, "0" * 12),
        _pass_payload(started_at=NOW - timedelta(minutes=400), status="submitted", submitted=1),
    )
    _write_tracker(root, [])

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(
            active_state="active",
            sub_state="running",
            last_trigger=_systemd_timestamp(NOW - timedelta(minutes=400)),
        ),
        service_properties=_in_flight_service_properties(),
    )
    verdict = _verdict(receipts)

    assert verdict not in {"timer_stopped", "scheduler_not_triggering", "evidence_stale"}
    assert verdict == "ok"
    assert status == 0


def test_l2_the_in_flight_gate_does_not_silence_the_remaining_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate suppresses three verdicts, not the whole tick."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_pass(
        root,
        _pass_name(10, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=10), status="resource_limit_blocked", counts=False
        ),
    )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(active_state="active", sub_state="running"),
        service_properties=_in_flight_service_properties(),
    )

    assert status == 1
    assert _verdict(receipts) == "pass_limit_blocked"


def test_l3_an_inactive_timer_while_a_pass_runs_is_not_graded_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conjunction is conservative: the service alone answers for the lane."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [])

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(active_state="inactive", sub_state="dead"),
        service_properties=_in_flight_service_properties(),
    )

    assert status == 0
    assert _verdict(receipts) == "ok"


def test_l3b_a_failed_oneshot_is_not_mistaken_for_a_pass_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SubState != "dead"` is NOT equivalent to "the lane is working".

    A oneshot whose run exited non-zero reports `failed`/`failed`.  Read as
    running, it gates off the stopped-timer verdict and the tick reports the
    lower-precedence service-failure verdict instead --- a precedence
    inversion on the geometry where both hold.
    """

    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(active_state="inactive", sub_state="dead"),
        service_properties=_service_properties(
            active_state="failed", sub_state="failed", result="exit-code"
        ),
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "timer_stopped"
    assert receipt["service"]["running"] is False


def test_l4_the_running_predicate_treats_activating_and_a_live_sub_state_as_running() -> None:
    """The unit-level pin on the single gate behind verdicts 3, 5 and 7."""

    assert probe.service_is_running(_in_flight_service_properties()) is True
    assert probe.service_is_running(_service_properties(active_state="active", sub_state="running")) is True
    assert (
        probe.service_is_running(_service_properties(active_state="deactivating", sub_state="final-sigterm"))
        is True
    )
    assert probe.service_is_running(_service_properties()) is False
    assert (
        probe.service_is_running(_service_properties(active_state="failed", sub_state="failed"))
        is False
    )
    assert "activating" in probe.SERVICE_RUNNING_ACTIVE_STATES
    assert probe.SERVICE_NOT_RUNNING_SUB_STATES == frozenset({"dead", "failed"})


# ---------------------------------------------------------------------------
# 3.5 / 3.6 / 3.7 -- ordering, mtime, and exclusion before truncation
# ---------------------------------------------------------------------------


def _reversed_order_evidence(root: Path) -> None:
    """Lexically greatest name carries the OLDEST recorded start.

    The one and only pass that submitted work is the lexically smallest and
    the chronologically newest, so an implementation that grades in filename
    order sees two consecutive blocked passes and reports the streak.
    """

    _write_pass(
        root,
        "scheduler_2026092312_aaaaaaaaaaaa.json",
        _pass_payload(started_at=NOW, status="submitted", submitted=1),
    )
    _write_pass(
        root,
        "scheduler_2026092312_ffffffffffff.json",
        _pass_payload(started_at=NOW - timedelta(minutes=10), blocked=4),
    )
    _write_pass(
        root,
        "scheduler_2026092312_eeeeeeeeeeee.json",
        _pass_payload(started_at=NOW - timedelta(minutes=20), blocked=4),
    )


def test_o1_recency_comes_from_started_at_not_from_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _reversed_order_evidence(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={
            probe.ENV_NO_SUBMISSION_PASSES: "2",
            probe.ENV_SCAN_LIMIT: "15",
            # #2662: a scan of 15 spans (15 - 12 - 1) x 5 = 10 minutes.
            ENV_IN_FLIGHT_MINUTES: "10",
        },
    )
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["evidence"]["newest_started_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert receipt["signals"]["no_submission_streak"] == 0


def test_o2_rewriting_a_modification_time_changes_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _reversed_order_evidence(root)
    config = {
        probe.ENV_NO_SUBMISSION_PASSES: "2",
        probe.ENV_SCAN_LIMIT: "15",
        # #2662: a scan of 15 spans (15 - 12 - 1) x 5 = 10 minutes.
        ENV_IN_FLIGHT_MINUTES: "10",
    }

    before_status, before_receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, config=config
    )
    before = _verdict(before_receipts)

    future = (NOW + timedelta(hours=1)).timestamp()
    os.utime(root / "scheduler_2026092312_eeeeeeeeeeee.json", (future, future))

    after_status, after_receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, config=config
    )

    assert before == "ok"
    assert _verdict(after_receipts) == before
    assert after_status == before_status == 0


def test_o3_a_pre_execution_snapshot_does_not_displace_its_terminal_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan is exactly full, and the snapshot sits at the boundary.

    Fifteen terminal artifacts fill `scan_limit=15`.  The single pass that
    submitted work is the lexically smallest name, and its pre-execution
    snapshot sorts immediately above it.  An implementation that truncates to
    fifteen names BEFORE dropping snapshots keeps the snapshot and discards
    that terminal artifact, leaving a window of nothing but blocked passes.
    """

    root = _evidence_root(tmp_path)
    boundary = "scheduler_2026092312_000000000000.json"
    _write_pass(root, boundary, _pass_payload(started_at=NOW, status="submitted", submitted=1))
    _write_pass(
        root,
        "scheduler_2026092312_000000000000.pre_execution.json",
        _pass_payload(started_at=NOW, status="planned"),
    )
    for index in range(14):
        suffix = chr(ord("a") + index) + "0" * 11
        _write_pass(
            root,
            f"scheduler_2026092312_{suffix}.json",
            _pass_payload(started_at=NOW - timedelta(minutes=10 + index * 5), blocked=4),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={
            probe.ENV_NO_SUBMISSION_PASSES: "2",
            probe.ENV_LOCK_PASSES: "2",
            probe.ENV_SCAN_LIMIT: "15",
            # #2662: a scan of 15 spans (15 - 12 - 1) x 5 = 10 minutes.
            ENV_IN_FLIGHT_MINUTES: "10",
        },
    )
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["evidence"]["terminal_passes_parsed"] == 15
    assert receipt["signals"]["no_submission_streak"] == 0


# ---------------------------------------------------------------------------
# 3.8 / 3.8b / 3.9 -- the four pass shapes
# ---------------------------------------------------------------------------


def test_s1_a_pass_that_submitted_work_breaks_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(5, "0" * 12),
        _pass_payload(started_at=NOW - timedelta(minutes=5), status="submitted", submitted=1, blocked=4),
    )
    for index in range(3):
        minutes = 15 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index + 1:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["signals"]["no_submission_streak"] == 0
    assert receipt["passes"]["progress_count"] == 1


def test_s2_a_pass_holding_blocked_candidates_extends_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(3):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    _status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert receipt["verdict"] == "submission_stalled"
    assert receipt["passes"]["blocked_count"] == 3


def test_s3_a_pass_with_no_blocked_candidate_breaks_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work that no longer appears blocked is no longer stalled."""

    root = _evidence_root(tmp_path)
    _write_pass(
        root, _pass_name(5, "0" * 12), _pass_payload(started_at=NOW - timedelta(minutes=5))
    )
    for index in range(3):
        minutes = 15 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index + 1:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["passes"]["idle_count"] == 1
    assert receipt["signals"]["no_submission_streak"] == 0


def test_s4_a_pass_without_a_progress_guard_does_not_break_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An early-exit pass sits inside a run of blocked passes.

    It has no status string of its own, so a status allowlist cannot reach it
    and it would fall through to the idle case and reset the streak.
    """

    root = _evidence_root(tmp_path)
    shapes = ["blocked", "neutral", "blocked", "blocked", "blocked"]
    for index, shape in enumerate(shapes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                blocked=0 if shape == "neutral" else 4,
                guard=shape != "neutral",
            ),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        # Prefix 17 - 12 = 5 walks all five passes, so the oldest one is graded.
        config={probe.ENV_SCAN_LIMIT: "17"},
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["passes"]["neutral_count"] == 1
    assert receipt["signals"]["no_submission_streak"] == 4


def test_s5_a_pass_without_a_progress_guard_does_not_extend_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    shapes = ["neutral", "neutral", "neutral", "idle", "blocked"]
    for index, shape in enumerate(shapes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                blocked=4 if shape == "blocked" else 0,
                guard=shape != "neutral",
            ),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        # Prefix 17 - 12 = 5 puts the trailing blocked pass inside the search,
        # so only the idle pass stands between it and the streak.
        config={probe.ENV_SCAN_LIMIT: "17"},
    )
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["passes"]["neutral_count"] == 3
    assert receipt["signals"]["no_submission_streak"] == 0


def test_s6_an_absent_count_makes_a_pass_neutral_not_zero_and_not_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.9 -- the degraded resource-limit artifact, outside the lookback.

    Placed outside the lookback window so the higher-precedence resource-limit
    verdict does not decide the tick, leaving the streak arithmetic itself
    observable.  Read as zero submissions it would either extend the streak to
    three (if the blocked count also defaulted) or break it as idle; neither is
    what the receipt must show.
    """

    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(5, "0" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=5), status="resource_limit_blocked", counts=False
        ),
    )
    for index in range(2):
        minutes = 15 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index + 1:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        # The lookback floor is 30 minutes; the degraded pass is 5 minutes old,
        # so it is pushed out of the window by pinning the clock instead.
        now=NOW + timedelta(minutes=200),
        config={probe.ENV_MAX_PASS_AGE_MINUTES: "240", probe.ENV_LIMIT_LOOKBACK_MINUTES: "30"},
    )
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["passes"]["neutral_count"] == 1
    assert receipt["signals"]["no_submission_streak"] == 2
    assert receipt["signals"]["resource_limit_passes_in_window"] == 0


@pytest.mark.parametrize("status_value", ["restart_reconciled", "preflight_blocked"])
def test_s7_a_fully_observed_pass_is_classified_by_its_counts_whatever_its_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_value: str
) -> None:
    """Regression on the shape of the first pass of the #2570 incident.

    `scheduler_2026092223_7e6955b406ba.json` is `restart_reconciled`, carries a
    progress guard, submitted nothing and held 47 blocked candidates.  A status
    allowlist for neutral would skip exactly the evidence this probe exists to
    catch.  `preflight_blocked` is the same argument from the other side: the
    scheduler counts it, so the probe must too.
    """

    root = _evidence_root(tmp_path)
    for index in range(3):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                status=status_value,
                submitted=0,
                blocked=47,
                guard=True,
            ),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["passes"]["blocked_count"] == 3
    assert receipt["passes"]["neutral_count"] == 0


# Newest-first shapes under the SHIPPED defaults.  The first one is the
# Phase 3 P1: twenty-one blocked passes with one neutral pass at position 3.
# Walked inside a window equal to the threshold (`max(20, 5) = 20`) the neutral
# pass spends a slot and the streak tops out at 19, so the verdict could never
# fire in production.  The second is the other side of the same boundary: the
# neutral pass is still skipped, but an idle pass halts the search one short of
# the threshold, so the wider search bound does not buy a streak it has not got.
SHIPPED_DEFAULT_STREAKS = {
    "neutral_interleaved": (
        ["blocked", "blocked", "neutral", *["blocked"] * 19],
        "submission_stalled",
        21,
    ),
    "idle_halts_one_short": (
        [*["blocked"] * 10, "neutral", *["blocked"] * 9, "idle", *["blocked"] * 5],
        "ok",
        19,
    ),
}


@pytest.mark.parametrize("case", sorted(SHIPPED_DEFAULT_STREAKS))
def test_s8_a_neutral_pass_does_not_cost_the_streak_a_position_at_the_shipped_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    shapes, expected, expected_streak = SHIPPED_DEFAULT_STREAKS[case]
    shipped = {
        probe.ENV_NO_SUBMISSION_PASSES: "20",
        probe.ENV_LOCK_PASSES: "5",
        probe.ENV_SCAN_LIMIT: "96",
        probe.ENV_CIRCUIT_PASSES: "20",
        ENV_IN_FLIGHT_MINUTES: "360",
    }
    # The literals ARE the shipped defaults; if a default moves, this test must
    # be re-derived rather than silently exercising some other geometry.
    assert (
        probe.DEFAULT_NO_SUBMISSION_PASSES,
        probe.DEFAULT_LOCK_PASSES,
        probe.DEFAULT_SCAN_LIMIT,
        probe.DEFAULT_CIRCUIT_PASSES,
    ) == (20, 5, 96, 20)
    # Re-derived for #2662, which moved the scan default 64 -> 96 so the
    # in-flight time gate is reachable.  Both shapes are unchanged by it: the
    # search still walks 23 / 26 passes, well inside either prefix, and the
    # longest run here spans 200 minutes, under the 360-minute gate.

    root = _evidence_root(tmp_path)
    for index, shape in enumerate(shapes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                blocked=4 if shape == "blocked" else 0,
                guard=shape != "neutral",
            ),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root, config=shipped)
    receipt = _receipt(receipts)

    assert receipt["verdict"] == expected
    assert status == (0 if expected == "ok" else 1)
    assert receipt["signals"]["no_submission_streak"] == expected_streak
    assert receipt["signals"]["no_submission_neutral_skipped"] == 1
    assert receipt["signals"]["no_submission_passes"] == 20
    # The search bound is the ordering-safe prefix, not the threshold.
    assert receipt["evidence"]["streak_window"] == 96 - probe.HOUR_BUCKET_MARGIN


def test_s9_a_guarded_resource_limit_pass_with_zero_counts_does_not_break_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resource-limit shape the writer CAN produce and the probe had missed.

    `scheduler_runtime.py` writes `counts` with both graded keys at zero on
    that path and attaches `progress_guard` whenever the error details carry
    one.  Without the status rule that pass reads as idle and resets the
    streak; the scheduler itself says resource-limit-aborted passes neither
    count nor clear.  Pushed outside the lookback (as in s6) so the
    higher-precedence resource-limit verdict does not decide the tick.
    """

    root = _evidence_root(tmp_path)
    shapes = ["blocked", "limit", "blocked", "blocked"]
    for index, shape in enumerate(shapes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                status="resource_limit_blocked" if shape == "limit" else "planned",
                submitted=0,
                blocked=0 if shape == "limit" else 4,
                guard=True,
                counts=True,
            ),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        now=NOW + timedelta(minutes=200),
        config={
            probe.ENV_MAX_PASS_AGE_MINUTES: "240",
            probe.ENV_LIMIT_LOOKBACK_MINUTES: "30",
        },
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["signals"]["resource_limit_passes_in_window"] == 0
    assert receipt["passes"]["neutral_count"] == 1
    assert receipt["passes"]["idle_count"] == 0
    assert receipt["signals"]["no_submission_streak"] == 3
    assert receipt["signals"]["no_submission_neutral_skipped"] == 1


# ---------------------------------------------------------------------------
# 3.10 / 3.11 -- streak and window boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("blocked_passes", "expected"), [(2, "ok"), (3, "submission_stalled")])
def test_b1_the_streak_fires_at_the_configured_count_and_not_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_passes: int, expected: str
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(blocked_passes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert _verdict(receipts) == expected
    assert status == (0 if expected == "ok" else 1)


def test_b2_one_submitting_pass_inside_the_window_breaks_the_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    for index in range(3):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                status="submitted" if index == 1 else "planned",
                submitted=1 if index == 1 else 0,
                blocked=4,
            ),
        )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 0
    assert _verdict(receipts) == "ok"


@pytest.mark.parametrize(
    ("minutes_ago", "expected"), [(90, "pass_limit_blocked"), (200, "ok")]
)
def test_b3_the_resource_limit_verdict_is_a_window_not_a_newest_pass_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, minutes_ago: int, expected: str
) -> None:
    """The degraded pass is never the newest one in either case."""

    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(5, "0" * 12),
        _pass_payload(started_at=NOW - timedelta(minutes=5), status="submitted", submitted=1),
    )
    _write_pass(
        root,
        _pass_name(minutes_ago, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=minutes_ago),
            status="resource_limit_blocked",
            counts=False,
        ),
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert _verdict(receipts) == expected
    assert status == (0 if expected == "ok" else 1)


def test_b4_the_lookback_window_is_not_shortened_by_the_streak_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy lane: the degraded pass is older than the four-pass streak window.

    Graded over the streak window alone it would be invisible; the lookback is
    a time window over every artifact the probe read.
    """

    root = _evidence_root(tmp_path)
    for index in range(6):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), status="submitted", submitted=1),
        )
    _write_pass(
        root,
        _pass_name(100, "f" * 12),
        _pass_payload(
            started_at=NOW - timedelta(minutes=100), status="resource_limit_blocked", counts=False
        ),
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "pass_limit_blocked"
    assert receipt["evidence"]["streak_window_passes"] == 4
    assert receipt["evidence"]["lookback_window_passes"] == 7


@pytest.mark.parametrize(("limit_passes", "listed", "truncated"), [(2, 2, 0), (22, 20, 2)])
def test_b5_the_receipt_names_the_resource_limit_passes_it_graded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_passes: int,
    listed: int,
    truncated: int,
) -> None:
    """The runbook tells the operator to open those artifacts, so name them.

    Bounded like every other receipt list: past `MAX_RECEIPT_LIST_ENTRIES`
    the overflow is counted, never silently dropped.
    """

    root = _evidence_root(tmp_path)
    _write_pass(
        root,
        _pass_name(1, "0" * 12),
        _pass_payload(started_at=NOW - timedelta(minutes=1), status="submitted", submitted=1),
    )
    written = []
    for index in range(limit_passes):
        minutes = 5 + index * 4
        name = _pass_name(minutes, f"{index + 1:012x}")
        written.append(name)
        _write_pass(
            root,
            name,
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                status="resource_limit_blocked",
                counts=False,
            ),
        )

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, config={probe.ENV_SCAN_LIMIT: "40"}
    )
    signals = _receipt(receipts)["signals"]

    assert status == 1
    assert signals["resource_limit_passes_in_window"] == limit_passes
    assert len(signals["resource_limit_pass_names"]) == listed
    assert signals["resource_limit_pass_names_truncated"] == truncated
    assert set(signals["resource_limit_pass_names"]) <= set(written)
    # Newest first, so the list clips away the oldest, not the freshest.
    assert signals["resource_limit_pass_names"][0] == written[0]


# ---------------------------------------------------------------------------
# 3.12 -- suppression
# ---------------------------------------------------------------------------


def test_sp1_a_chronic_entry_does_not_mask_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(
        root,
        [
            _tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1300, subject_kind="job"),
            _tracker_entry(reason="blocked:predecessor_pending", consecutive_passes=3),
        ],
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "no_progress_circuit_open"
    assert [row["reason"] for row in receipt["suppressed"]] == [SUPPRESSED_REASON]


def test_sp2_only_chronic_entries_grade_healthy_but_stay_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(
        root,
        [
            _tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1300, subject_kind="job"),
            _tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1198, subject_id="gfs:b"),
        ],
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert [row["consecutive_passes"] for row in receipt["suppressed"]] == [1300, 1198]
    assert {row["matched_rule"] for row in receipt["suppressed"]} == {SUPPRESSED_REASON}


def test_sp3_suppression_matches_the_reason_exactly_not_as_a_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(
        root,
        [_tracker_entry(reason=SUPPRESSED_REASON + ":extra", consecutive_passes=9)],
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "no_progress_circuit_open"


@pytest.mark.parametrize(
    "scenario", ["evidence_stale", "pass_limit_blocked", "submission_stalled", "timer_stopped"]
)
def test_sp4_suppression_never_reaches_any_other_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    root = _evidence_root(tmp_path)
    timer = _timer_properties()
    if scenario == "evidence_stale":
        _write_pass(
            root,
            _pass_name(400, "0" * 12),
            _pass_payload(started_at=NOW - timedelta(minutes=400), status="submitted", submitted=1),
        )
    elif scenario == "pass_limit_blocked":
        _healthy_passes(root)
        _write_pass(
            root,
            _pass_name(10, "f" * 12),
            _pass_payload(
                started_at=NOW - timedelta(minutes=10),
                status="resource_limit_blocked",
                counts=False,
            ),
        )
    elif scenario == "submission_stalled":
        for index in range(3):
            minutes = 5 + index * 10
            _write_pass(
                root,
                _pass_name(minutes, f"{index:012x}"),
                _pass_payload(started_at=NOW - timedelta(minutes=minutes), blocked=4),
            )
    else:
        _healthy_passes(root)
        timer = _timer_properties(active_state="inactive", sub_state="dead")
    _write_tracker(
        root, [_tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1300, subject_kind="job")]
    )

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, timer_properties=timer
    )

    assert status == 1
    assert _verdict(receipts) == scenario


# ---------------------------------------------------------------------------
# 3.13 -- fail-closed
# ---------------------------------------------------------------------------


def test_f1_an_oversize_artifact_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    payload = _pass_payload(started_at=NOW - timedelta(minutes=1))
    payload["padding"] = "x" * (probe.MAX_EVIDENCE_BYTES + 16)
    _write_pass(root, _pass_name(1, "f" * 12), payload)

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f2_a_symlinked_artifact_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_pass_payload(started_at=NOW)))
    (root / _pass_name(1, "f" * 12)).symlink_to(target)

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f3_a_non_regular_artifact_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    (root / _pass_name(1, "f" * 12)).mkdir()

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f4_an_unparseable_artifact_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_pass(root, _pass_name(1, "f" * 12), None, raw="{not json")

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f5_an_artifact_without_a_started_at_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    payload = _pass_payload(started_at=NOW)
    del payload["started_at"]
    _write_pass(root, _pass_name(1, "f" * 12), payload)

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f6_an_unrecognised_tracker_schema_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [], schema_version="nhms.scheduler.no_progress_tracker.v2")

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f7_a_malformed_tracker_row_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [{"subject_kind": "job", "subject_id": "x", "reason": "y"}])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f8_unparseable_systemctl_output_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, service_text="Failed to get properties\n"
    )

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f9_a_missing_systemctl_binary_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        systemctl_path=str(tmp_path / "no-such-systemctl"),
    )

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f10_reaching_the_enumeration_bound_is_probe_failed_not_a_silent_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.scandir` returns filesystem order, so a truncated listing is ungradable."""

    root = _evidence_root(tmp_path)
    for index in range(20):
        minutes = 5 + index
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(started_at=NOW - timedelta(minutes=minutes), status="submitted", submitted=1),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        # The smallest bound the config admits (it must be >= SCAN_LIMIT = 16),
        # still below the twenty entries on disk.
        config={probe.ENV_MAX_ENTRIES_SCANNED: "16"},
    )

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f10b_every_damaged_artifact_is_listed_not_just_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator holding `probe_failed` needs the whole list to act on."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    for index in range(3):
        _write_pass(root, _pass_name(1 + index, f"{0xF0 + index:012x}"), None, raw="{not json")

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "probe_failed"
    assert len(receipt["evidence"]["unreadable"]) == 3
    assert {row.split(": ")[-1] for row in receipt["evidence"]["unreadable"]} == {
        _pass_name(1 + index, f"{0xF0 + index:012x}") for index in range(3)
    }


def test_f11_an_unparseable_last_trigger_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(last_trigger="whenever"),
    )

    assert status == 1
    assert _verdict(receipts) == "probe_failed"


def test_f11b_an_absent_tracker_is_an_observation_not_unreadable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOENT is a definite answer; every other tracker failure is not.

    The circuit writes the tracker on its first enabled fully-observed pass and
    the runbook documents the absent state as the ordinary first-pass shape, so
    an absent file is recorded as "no entries" rather than graded probe-failed.
    Pinned so that tightening it to probe-failed becomes a visible decision
    instead of a silent one.
    """

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    assert not (root / probe.TRACKER_FILENAME).exists()

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["signals"]["tracker_present"] is False
    assert receipt["signals"]["tracker_entries"] == 0


def test_f12_a_never_triggered_timer_with_an_idle_service_is_not_graded_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, timer_properties=_timer_properties(last_trigger="")
    )

    assert status == 1
    assert _verdict(receipts) == "scheduler_not_triggering"


# ---------------------------------------------------------------------------
# 3.16 -- configuration refusal precedes evidence gathering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {probe.ENV_MAX_PASS_AGE_MINUTES: "60"},
        {probe.ENV_MAX_TRIGGER_AGE_MINUTES: "10"},
        {probe.ENV_LIMIT_LOOKBACK_MINUTES: "15"},
        {probe.ENV_LOCK_PASSES: "1"},
        {probe.ENV_NO_SUBMISSION_PASSES: "1"},
        {probe.ENV_CIRCUIT_PASSES: "0"},
        {probe.ENV_SCAN_LIMIT: "14"},
        {probe.ENV_MAX_ENTRIES_SCANNED: "3"},
        {probe.ENV_MAX_PASS_AGE_MINUTES: "not-a-number"},
        # #2662: below the two-timer-interval floor, and above what the scan
        # window can span ((16 - 12 - 1) x 5 = 15 minutes at BASE_CONFIG).
        {ENV_IN_FLIGHT_MINUTES: "9"},
        {ENV_IN_FLIGHT_MINUTES: "16"},
    ],
)
def test_c1_an_out_of_range_threshold_is_refused_before_any_evidence_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict[str, str]
) -> None:
    """The evidence tree is poisoned, so reading it would cost exit 1, not 2."""

    root = _evidence_root(tmp_path)
    _write_pass(root, _pass_name(1, "f" * 12), None, raw="{not json")

    status, receipts, log = _run(
        tmp_path, monkeypatch, evidence_root=root, config=override
    )

    assert status == 2
    assert not log.exists(), "systemctl was invoked before the configuration was validated"
    assert not (receipts / "latest.json").exists()


def test_c2_an_evidence_root_that_is_not_a_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_a_directory = tmp_path / "evidence-file"
    not_a_directory.write_text("{}")

    status, _receipts, log = _run(tmp_path, monkeypatch, evidence_root=not_a_directory)

    assert status == 2
    assert not log.exists()


def test_c3_a_receipt_root_inside_the_evidence_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's own output must never enter readiness discovery or retention."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, _receipts, log = _run(
        tmp_path, monkeypatch, evidence_root=root, receipt_root=root / "stall-health"
    )

    assert status == 2
    assert not log.exists()


def test_c4_the_shipped_defaults_satisfy_the_probes_own_range_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default that its own validator would refuse is a unit that never runs."""

    root = _evidence_root(tmp_path)
    for name in ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(probe.ENV_EVIDENCE_ROOT, str(root))

    config = probe.load_config()

    # The range rule itself, spelled against the thresholds rather than
    # against `streak_window` (which is DERIVED from `scan_limit`, so a check
    # written against it would be true by construction).
    assert config.scan_limit - probe.HOUR_BUCKET_MARGIN > max(
        config.no_submission_passes, config.lock_passes
    )
    # The streak search bound must hold strictly more than the threshold, or
    # a single neutral pass inside it makes `submission_stalled` unreachable.
    assert config.streak_window == config.scan_limit - probe.HOUR_BUCKET_MARGIN
    assert config.streak_window > config.no_submission_passes
    assert config.max_entries_scanned >= config.scan_limit
    # #2662: the shipped gate is reachable across the shipped safe prefix at
    # the timer's minimum cadence, and clears the longest healthy run the
    # repository records (193.9-minute pass + 79-minute forecast).
    assert config.in_flight_minutes == 360
    assert config.in_flight_minutes <= (config.streak_window - 1) * probe.MIN_INTER_PASS_MINUTES
    assert config.in_flight_minutes > 193.9 + 79
    assert config.suppressed_reasons == frozenset({SUPPRESSED_REASON})
    assert config.timer_unit == "nhms-compute-scheduler.timer"
    assert config.service_unit == "nhms-compute-scheduler.service"


# (no_submission_passes, lock_passes): the smallest-thresholds case, the lock
# threshold being the larger one, and the operator retune that raises only the
# submission threshold to 52 while leaving the shipped scan limit of 64.
SCAN_BOUNDARY_THRESHOLDS = [(3, 2), (3, 5), (52, 5)]


@pytest.mark.parametrize(("no_submission", "lock"), SCAN_BOUNDARY_THRESHOLDS)
def test_c7_a_scan_whose_safe_prefix_only_equals_the_longest_streak_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_submission: int,
    lock: int,
) -> None:
    """`scan_limit - margin == threshold`: one neutral pass would cap the streak.

    The evidence tree is poisoned as in c1, so reading it would cost exit 1.
    """

    scan_limit = max(no_submission, lock) + probe.HOUR_BUCKET_MARGIN
    root = _evidence_root(tmp_path)
    _write_pass(root, _pass_name(1, "f" * 12), None, raw="{not json")

    status, receipts, log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={
            probe.ENV_NO_SUBMISSION_PASSES: str(no_submission),
            probe.ENV_LOCK_PASSES: str(lock),
            probe.ENV_SCAN_LIMIT: str(scan_limit),
        },
    )
    stderr = capsys.readouterr().err

    assert status == 2
    assert not log.exists(), "systemctl was invoked before the configuration was validated"
    assert not (receipts / "latest.json").exists()
    assert f"{probe.ENV_SCAN_LIMIT}={scan_limit} must be greater than {scan_limit}" in stderr
    assert "must be longer than the longest graded streak threshold" in stderr
    assert "neutral passes inside it make the verdict unreachable" in stderr


@pytest.mark.parametrize(("no_submission", "lock"), SCAN_BOUNDARY_THRESHOLDS)
def test_c8_one_name_past_the_boundary_is_graded_and_reaches_the_stall_through_a_neutral_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_submission: int, lock: int
) -> None:
    """The smallest admitted scan: a prefix one pass longer than the threshold.

    The evidence is the submission threshold's worth of blocked passes plus
    one neutral pass -- exactly the prefix when the submission threshold is
    the larger, fewer than it when the lock threshold is -- which is the case
    an equal-length prefix could not reach.
    """

    scan_limit = max(no_submission, lock) + probe.HOUR_BUCKET_MARGIN + 1
    root = _evidence_root(tmp_path)
    shapes = ["blocked", "neutral", *(["blocked"] * (no_submission - 1))]
    for index, shape in enumerate(shapes):
        minutes = 5 + index * 10
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                blocked=0 if shape == "neutral" else 4,
                guard=shape != "neutral",
            ),
        )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={
            probe.ENV_NO_SUBMISSION_PASSES: str(no_submission),
            probe.ENV_LOCK_PASSES: str(lock),
            probe.ENV_SCAN_LIMIT: str(scan_limit),
        },
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["evidence"]["streak_window"] == max(no_submission, lock) + 1
    assert receipt["signals"]["no_submission_streak"] == no_submission
    assert receipt["signals"]["no_submission_neutral_skipped"] == 1


def test_c5_an_explicitly_empty_suppression_list_clears_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Environment=NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS=` in a drop-in.

    The one knob whose effect is to silence an alert must distinguish "unset"
    from "set to nothing"; folding the empty value back into the default would
    keep the checked-in suppression in force against the operator's explicit
    instruction.
    """

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(
        root, [_tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1300, subject_kind="job")]
    )

    status, receipts, _log = _run(
        tmp_path, monkeypatch, evidence_root=root, config={probe.ENV_SUPPRESSED_REASONS: ""}
    )
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "no_progress_circuit_open"
    assert receipt["suppressed_reasons"] == []
    assert receipt["suppressed"] == []
    assert receipt["open"][0]["reason"] == SUPPRESSED_REASON

    # And unset still means the checked-in default.
    monkeypatch.delenv(probe.ENV_SUPPRESSED_REASONS)
    assert probe.load_config().suppressed_reasons == frozenset({SUPPRESSED_REASON})


def test_c6_a_receipt_root_reached_through_a_symlink_into_the_evidence_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both roots are resolved the same way, so a symlink cannot launder one."""

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    alias = tmp_path / "looks-elsewhere"
    alias.symlink_to(root, target_is_directory=True)

    status, _receipts, log = _run(
        tmp_path, monkeypatch, evidence_root=root, receipt_root=alias / "stall-health"
    )

    assert status == 2
    assert not log.exists()
    assert not (root / "stall-health").exists()


# ---------------------------------------------------------------------------
# 3.14 -- the probe is structurally incapable of mutation, and self-contained
# ---------------------------------------------------------------------------


def test_d1_probe_source_contains_no_unit_mutation_verb() -> None:
    source = PROBE_SOURCE.read_text()
    hits = sorted({match.group(0) for match in MUTATION_VERBS.finditer(source)})

    assert hits == [], f"probe source carries unit mutation verbs: {hits}"


def test_d2_only_read_only_systemctl_subcommands_are_ever_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _status, _receipts, log = _run(tmp_path, monkeypatch, evidence_root=root)
    invocations = [line.split() for line in log.read_text().splitlines() if line.strip()]

    assert invocations, "the fake systemctl recorded no invocation at all"
    subcommands = set()
    units = set()
    for argv in invocations:
        assert argv[0] == "--user"
        subcommands.add(argv[1])
        units.add(argv[2])
    assert subcommands == {"show"}
    assert units == {"nhms-compute-scheduler.timer", "nhms-compute-scheduler.service"}


def test_d3_probe_imports_only_the_standard_library() -> None:
    tree = ast.parse(PROBE_SOURCE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif node.level:  # pragma: no cover - a relative import would be a defect
                pytest.fail("the probe must not carry a relative import")

    non_stdlib = sorted(imported - set(sys.stdlib_module_names))
    assert non_stdlib == [], f"probe imports non-stdlib modules: {non_stdlib}"
    assert "services" not in imported
    assert "packages" not in imported


@pytest.mark.parametrize(
    "fragment", ["from services", "from packages", "import services", "import packages"]
)
def test_d4_probe_source_carries_no_repository_import(fragment: str) -> None:
    """D4 textually as well as structurally: the probe is staged outside the tree."""

    source = PROBE_SOURCE.read_text()
    offending = [line for line in source.splitlines() if line.lstrip().startswith(fragment)]

    assert offending == [], f"probe source carries a repository import: {offending}"


# ---------------------------------------------------------------------------
# 3.15 -- parity with the service-layer definitions
# ---------------------------------------------------------------------------


PARITY_NAMES = (
    # Governed terminal pass artifacts.
    "scheduler_2026092312_7e6955b406ba.json",
    "scheduler_2026092223_000000000000.json",
    # A governed pre-execution snapshot.
    "scheduler_2026092312_7e6955b406ba.pre_execution.json",
    # Real non-pass entries of the node-22 evidence root.
    "no-progress-tracker.json",
    "no-progress-tracker.json.tmp",
    "repair_stale_gfs_2026071200.json",
    "stale-lock-clear-issue882-20260706T072555Z.json",
    "retention",
    "scheduler.lock",
    "scheduler_2026092312_7e6955b406ba.json.tmp",
    "scheduler_partial",
    "unrelated.json",
)


def test_e1_the_probes_filename_predicate_matches_the_service_layer_on_every_name() -> None:
    from services.orchestrator import scheduler_evidence

    for name in PARITY_NAMES:
        assert probe.is_pass_evidence_filename(name) is (
            scheduler_evidence.is_scheduler_pass_evidence_filename(name)
        ), name


def test_e2_the_probes_copied_constants_equal_the_service_layer_definitions() -> None:
    """The sample check above cannot see a predicate NARROWED upstream.

    If `is_scheduler_pass_evidence_filename` were tightened to "prefix + a
    ten-digit cycle + twelve hex digits + suffix", every name in the sample
    would still be classified alike while the probe's three-line copy silently
    grew permissive.  So the constants themselves are pinned, not the sample.
    """

    from services.orchestrator import scheduler_evidence

    assert probe.PASS_EVIDENCE_PREFIX == scheduler_evidence.SCHEDULER_PASS_EVIDENCE_PREFIX
    assert probe.PASS_EVIDENCE_SUFFIXES == scheduler_evidence.SCHEDULER_PASS_EVIDENCE_SUFFIXES
    assert probe.MAX_EVIDENCE_BYTES == scheduler_evidence.MAX_EVIDENCE_BYTES
    assert probe.PRE_EXECUTION_SUFFIX in scheduler_evidence.SCHEDULER_PASS_EVIDENCE_SUFFIXES


def test_e3_the_tracker_schema_version_matches_the_circuits_own() -> None:
    from services.orchestrator import scheduler_no_progress

    assert probe.TRACKER_SCHEMA_VERSION == scheduler_no_progress.STATE_SCHEMA_VERSION
    assert probe.TRACKER_FILENAME == scheduler_no_progress.STATE_FILENAME
    # The tracker is deliberately unprefixed so pass-evidence retention skips
    # it; the probe's predicate must agree.
    assert probe.is_pass_evidence_filename(probe.TRACKER_FILENAME) is False


def test_e5_the_terminal_skip_reasons_equal_the_schedulers_own() -> None:
    """#2662: the probe's "is this candidate finished" predicate IS the scheduler's.

    The probe cannot import the set (D4), so it carries a literal; this is the
    pin that keeps the literal from drifting, and `scripts/select_ci_tests.py`
    routes `scheduler_runtime.py` here so it runs on the diff that would drift
    it.  A reason added to the scheduler's set but not here would make the
    probe read finished work as in-flight (a false alarm after the gate); one
    removed there but kept here would read in-flight work as finished, which
    is the #2655 blind spot again.
    """

    from services.orchestrator import scheduler_runtime

    assert probe.TERMINAL_SKIP_REASONS == scheduler_runtime._RETENTION_TERMINAL_SKIP_REASONS
    assert "active_duplicate_pipeline" not in probe.TERMINAL_SKIP_REASONS


def test_e4_only_terminal_artifacts_enter_the_graded_window() -> None:
    assert probe.is_terminal_pass_filename("scheduler_2026092312_7e6955b406ba.json") is True
    assert (
        probe.is_terminal_pass_filename("scheduler_2026092312_7e6955b406ba.pre_execution.json")
        is False
    )
    assert probe.is_terminal_pass_filename("no-progress-tracker.json") is False


# ---------------------------------------------------------------------------
# 3.17 / 3.18 -- the receipt, and the read-only guarantee
# ---------------------------------------------------------------------------


def test_r1_the_receipt_pairs_every_graded_signal_with_its_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(
        root, [_tracker_entry(reason=SUPPRESSED_REASON, consecutive_passes=1300, subject_kind="job")]
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)
    target = receipts / "latest.json"

    assert status == 0
    assert receipt["schema_version"] == probe.RECEIPT_SCHEMA_VERSION
    assert receipt["exit_code"] == 0
    assert receipt["generated_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert receipt["runbook"]
    for observed, threshold in (
        ("last_trigger_age_minutes", "max_trigger_age_minutes"),
    ):
        assert observed in receipt["timer"] and threshold in receipt["timer"]
    for observed, threshold in (("newest_pass_age_minutes", "max_pass_age_minutes"),):
        assert observed in receipt["evidence"] and threshold in receipt["evidence"]
    for observed, threshold in (
        ("resource_limit_passes_in_window", "limit_lookback_minutes"),
        ("lock_contended_streak", "lock_passes"),
        ("no_submission_streak", "no_submission_passes"),
        ("circuit_open_entries", "circuit_passes"),
    ):
        assert observed in receipt["signals"] and threshold in receipt["signals"]
    assert set(receipt["passes"]) == {
        "progress_count",
        "blocked_count",
        "idle_count",
        "in_flight_held_count",
        "neutral_count",
    }
    assert "in_flight_run_span_minutes" in receipt["signals"]
    assert "in_flight_minutes" in receipt["signals"]
    assert receipt["suppressed"][0]["matched_rule"] == SUPPRESSED_REASON
    # Private, bounded, and outside the evidence root.
    assert receipts.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_size <= probe.MAX_HEALTH_RECEIPT_BYTES
    assert not target.is_relative_to(root)


def test_r1b_a_saturated_tick_still_produces_a_writable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worst case for the byte bound: every variable-length list is saturated.

    An unwritable receipt costs the operator the verdict itself, so the four
    clipped lists plus the configured reason set must not be able to push the
    document past `MAX_HEALTH_RECEIPT_BYTES`.
    """

    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    long_reason = "x" * 400
    _write_tracker(
        root,
        [
            _tracker_entry(
                reason=long_reason,
                consecutive_passes=5000 + index,
                subject_kind="candidate" + "y" * 400,
                subject_id="z" * 400,
            )
            for index in range(120)
        ],
    )

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={probe.ENV_SUPPRESSED_REASONS: ",".join(f"{long_reason}-{i}" for i in range(80))},
    )
    target = receipts / "latest.json"
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "no_progress_circuit_open"
    assert target.stat().st_size <= probe.MAX_HEALTH_RECEIPT_BYTES
    assert len(receipt["open"]) == probe.MAX_RECEIPT_LIST_ENTRIES
    assert receipt["open_truncated"] == 100
    assert receipt["suppressed_reasons_truncated"] == 60


def _github_heading_slug(text: str) -> str:
    """GitHub's heading anchor rule: lowercase, drop punctuation, spaces to hyphens."""

    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def _runbook_heading_slugs() -> list[str]:
    slugs: list[str] = []
    in_fence = False
    for line in RUNBOOK.read_text().splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match and not in_fence:
            slugs.append(_github_heading_slug(match.group(1)))
    return slugs


@pytest.mark.parametrize("verdict", sorted(probe.RUNBOOK_ANCHORS))
def test_r2_every_verdict_names_a_runbook_anchor_that_exists(verdict: str) -> None:
    """4.6 mechanically: the pointer the probe emits must resolve in the runbook.

    The anchors are headings, not inline HTML ids (the repo's Markdown lint
    admits none).  The pointer resolves only if exactly one heading slugs to
    it: GitHub suffixes a duplicate with `-1`, which would send the operator
    to whichever copy came first.
    """

    pointer = probe.runbook_pointer(verdict)
    anchor = pointer.split("#", 1)[1]
    slugs = _runbook_heading_slugs()

    assert pointer.startswith(probe.RUNBOOK_PATH + "#")
    assert slugs.count(anchor) == 1, f"{anchor!r} is the slug of {slugs.count(anchor)} headings"
    # The heading text IS the anchor, so no slugger variant can rewrite it.
    assert re.search(rf"^#{{1,6}} {re.escape(anchor)}$", RUNBOOK.read_text(), flags=re.MULTILINE)


def test_r3_a_non_healthy_verdict_writes_a_runbook_pointer_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        timer_properties=_timer_properties(unit_file_state="disabled"),
    )
    captured = capsys.readouterr()

    assert status == 1
    assert "verdict=timer_not_enabled" in captured.err
    assert f"runbook={probe.runbook_pointer('timer_not_enabled')}" in captured.err
    assert _verdict(receipts) == "timer_not_enabled"


def test_r4_the_probe_writes_nothing_under_the_evidence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _evidence_root(tmp_path)
    _healthy_passes(root)
    _write_tracker(root, [_tracker_entry(reason="blocked:x", consecutive_passes=9)])

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            entry.name: (entry.stat().st_mtime_ns, entry.stat().st_size)
            for entry in os.scandir(root)
        }

    before = snapshot()
    status, _receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)

    assert status == 1
    assert snapshot() == before


# ---------------------------------------------------------------------------
# 3.19 -- the unit files
# ---------------------------------------------------------------------------


def _directive_lines(unit: Path) -> list[str]:
    """Every non-comment, non-blank directive line of a unit file.

    Matched on directives rather than on raw text: both units DISCUSS
    `PrivateTmp`, `EnvironmentFile` and the scheduler units in comments, and a
    substring assertion would red on the very comments the fixture requires.
    """

    return [
        line.strip()
        for line in unit.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("unit", [SERVICE_UNIT, TIMER_UNIT], ids=["service", "timer"])
def test_u1_the_probe_units_declare_no_relationship_to_the_scheduler(unit: Path) -> None:
    directives = ("Wants=", "After=", "Requires=", "Before=", "BindsTo=", "PartOf=", "Requisite=")
    lines = _directive_lines(unit)
    offending = [
        line
        for line in lines
        if any(line.startswith(directive) for directive in directives)
        and "nhms-compute-scheduler" in line
    ]

    assert offending == [], f"{unit.name} names the watched lane in a dependency directive: {offending}"
    assert [line for line in lines if line.startswith("EnvironmentFile")] == []


def test_u2_the_service_unit_is_db_free_journal_logged_and_namespace_safe() -> None:
    text = SERVICE_UNIT.read_text()
    lines = _directive_lines(SERVICE_UNIT)
    unset_line = next(line for line in lines if line.startswith("UnsetEnvironment="))
    unset = unset_line.split("=", 1)[1].split()

    assert "Type=oneshot" in text
    assert (
        "ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python "
        "/scratch/frd_muziyao/NWM/scripts/node22_scheduler_stall_health.py" in text
    )
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text
    assert "UMask=0077" in text
    assert "TimeoutStartSec=" in text
    assert [line for line in lines if line.startswith("PrivateTmp")] == []
    assert set(PG_ENVIRONMENT_VARIABLES) <= set(unset)


def test_u3_the_timer_fires_every_quarter_hour_and_points_at_the_service() -> None:
    text = TIMER_UNIT.read_text()

    assert "OnCalendar=*:03/15" in text
    assert "RandomizedDelaySec=60" in text
    assert "Unit=nhms-node22-scheduler-stall-health.service" in text
    assert "WantedBy=default.target" in text


def test_u4_the_runbook_documents_the_drop_in_and_the_dropped_installer_guarantees() -> None:
    text = RUNBOOK.read_text()

    assert "nhms-node22-scheduler-stall-health.service.d/10-thresholds.conf" in text
    assert "install_node22_refresh_timer_health.sh" in text
    assert "nhms-scheduler-file-provider-refresh" in text
    assert SUPPRESSED_REASON in text


# ---------------------------------------------------------------------------
# Issue #2662 -- in-flight-held passes, the time gate, the frontier bypass
# ---------------------------------------------------------------------------

FREEZE_CYCLE = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
FREEZE_PROGRESS_STARTED_AT = datetime(2026, 9, 25, 16, 45, 42, tzinfo=UTC)
FREEZE_PROGRESS_NAME = "scheduler_2026092516_1436f9714b94.json"
FREEZE_NEWEST_STARTED_AT = datetime(2026, 9, 27, 3, 17, 11, tzinfo=UTC)
FREEZE_NOW = datetime(2026, 9, 27, 3, 18, 31, tzinfo=UTC)
FREEZE_PASSES = 340
#: 34.5 hours over 340 passes: 6.09 minutes from one pass start to the next.
FREEZE_INTERVAL = (FREEZE_NEWEST_STARTED_AT - FREEZE_PROGRESS_STARTED_AT) / FREEZE_PASSES


def _freeze_started_at(index: int) -> datetime:
    """Start of the ``index``-th zero-submission pass; exact at both measured ends."""

    span = FREEZE_NEWEST_STARTED_AT - FREEZE_PROGRESS_STARTED_AT
    return FREEZE_PROGRESS_STARTED_AT + span * index / FREEZE_PASSES

OLD_IFS_SUBJECT = "job_cycle_ifs_2026091100_convert_cohort_58980881757c_forecast"
OLD_GFS_SUBJECT = "job_cycle_gfs_2026091212_convert_cohort_6ae61a5780d2_forecast"
FRONTIER_IFS_SUBJECT = "job_cycle_ifs_2026092500_convert_cohort_34e13d82a8a5_forecast"
FRONTIER_GFS_SUBJECT = "job_cycle_gfs_2026092500_convert_cohort_caaad82942af_forecast"

# The shipped node-22 values, spelled as literals for the same reason s8 spells
# them: if a default moves these tests must be re-derived, not silently moved.
SHIPPED_CONFIG = {
    probe.ENV_NO_SUBMISSION_PASSES: "20",
    probe.ENV_LOCK_PASSES: "5",
    probe.ENV_CIRCUIT_PASSES: "20",
    probe.ENV_SCAN_LIMIT: "96",
    ENV_IN_FLIGHT_MINUTES: "360",
}


def _freeze_skipped_rows() -> list[dict[str, object]]:
    """The measured skip breakdown: 94 in-flight duplicates and 2 finished."""

    rows = [
        _skip_row("active_duplicate_pipeline", source, FREEZE_CYCLE, basin=f"basin_{index:02d}")
        for source in ("gfs", "IFS")
        for index in range(47)
    ]
    rows += [
        _skip_row("terminal_hydro_success", source, FREEZE_CYCLE, basin="basin_47")
        for source in ("gfs", "IFS")
    ]
    assert len(rows) == 96
    return rows


def _freeze_pass_name(started_at: datetime, index: int) -> str:
    return f"scheduler_{started_at:%Y%m%d%H}_{index:012x}.json"


def _write_freeze(
    root: Path, *, passes_after_progress: int, on_disk: int | None = None
) -> tuple[datetime, datetime]:
    """Write the freeze as it stood ``passes_after_progress`` passes in.

    Returns ``(newest pass started_at, the instant the probe runs)``.  The
    progress pass is written only while it would still be on disk next to the
    ``on_disk`` newest zero-submission passes; evidence retention is why the
    late-freeze fixture does not contain it.
    """

    rows = _freeze_skipped_rows()
    kept = passes_after_progress if on_disk is None else min(on_disk, passes_after_progress)
    for index in range(passes_after_progress - kept + 1, passes_after_progress + 1):
        started_at = _freeze_started_at(index)
        _write_pass(
            root,
            _freeze_pass_name(started_at, index),
            _pass_payload(started_at=started_at, status="planned", skipped=rows),
        )
    if kept == passes_after_progress:
        _write_pass(
            root,
            FREEZE_PROGRESS_NAME,
            _pass_payload(
                started_at=FREEZE_PROGRESS_STARTED_AT, status="submitted", submitted=94
            ),
        )
    newest = _freeze_started_at(passes_after_progress)
    return newest, newest + timedelta(seconds=80)


def _freeze_tracker(
    *, frontier_passes: int | None, old_passes: tuple[int, int] = (2200, 2098)
) -> list[dict[str, object]]:
    entries = [
        _tracker_entry(
            reason=SUPPRESSED_REASON,
            consecutive_passes=old_passes[0],
            subject_kind="job",
            subject_id=OLD_IFS_SUBJECT,
        ),
        _tracker_entry(
            reason=SUPPRESSED_REASON,
            consecutive_passes=old_passes[1],
            subject_kind="job",
            subject_id=OLD_GFS_SUBJECT,
        ),
    ]
    if frontier_passes is not None:
        entries += [
            _tracker_entry(
                reason=SUPPRESSED_REASON,
                consecutive_passes=frontier_passes,
                subject_kind="job",
                subject_id=subject,
            )
            for subject in (FRONTIER_IFS_SUBJECT, FRONTIER_GFS_SUBJECT)
        ]
    return entries


def _run_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    now: datetime,
    config: dict[str, str],
) -> tuple[int, dict]:
    """Run the probe at ``now`` on a lane whose units look healthy at ``now``."""

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        now=now,
        timer_properties=_timer_properties(
            last_trigger=_systemd_timestamp(now - timedelta(minutes=2))
        ),
        config=config,
    )
    return status, _receipt(receipts)


def test_i0_the_shipped_literals_are_the_shipped_defaults() -> None:
    assert (
        probe.DEFAULT_NO_SUBMISSION_PASSES,
        probe.DEFAULT_LOCK_PASSES,
        probe.DEFAULT_CIRCUIT_PASSES,
        probe.DEFAULT_SCAN_LIMIT,
        probe.DEFAULT_IN_FLIGHT_MINUTES,
    ) == (20, 5, 20, 96, 360)
    assert probe.ENV_IN_FLIGHT_MINUTES == ENV_IN_FLIGHT_MINUTES


def test_i1_the_reconstructed_freeze_is_not_ok_with_both_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt of 2026-09-27T03:18:31Z said `ok`.  It must not.

    Shipped defaults, the full measured tracker.  Both fixes are live: the
    time gate fires verdict 10, and the two frontier entries are open with
    the bypass mark while the two old-cycle ones stay suppressed.
    """

    root = _evidence_root(tmp_path)
    newest, _ = _write_freeze(root, passes_after_progress=FREEZE_PASSES, on_disk=130)
    assert newest == FREEZE_NEWEST_STARTED_AT
    _write_tracker(root, _freeze_tracker(frontier_passes=338))

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=FREEZE_NOW, config=SHIPPED_CONFIG)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["passes"] == {
        "progress_count": 0,
        "blocked_count": 0,
        "idle_count": 0,
        "in_flight_held_count": 96,
        "neutral_count": 0,
    }
    signals = receipt["signals"]
    assert signals["no_submission_streak"] == 0
    assert signals["in_flight_run_passes"] == 84
    assert signals["in_flight_run_in_flight_held_passes"] == 84
    assert signals["in_flight_run_ended_by"] == "scan_window"
    assert signals["in_flight_run_start_is_lower_bound"] is True
    assert signals["in_flight_minutes"] == 360
    # 83 intervals of 6.09 minutes across the ordering-safe prefix.
    assert signals["in_flight_run_span_minutes"] == pytest.approx(83 * 6.0926, abs=0.1)
    assert signals["in_flight_run_span_minutes"] >= signals["in_flight_minutes"]
    assert signals["in_flight_run_newest_started_at"] == "2026-09-27T03:17:11Z"
    assert signals["circuit_open_entries"] == 2
    assert signals["circuit_bypassed_frontier_entries"] == 2
    assert receipt["frontier"]["cycles"] == ["gfs_2026092500", "ifs_2026092500"]
    assert {row["subject_id"]: row["suppression_bypassed_frontier"] for row in receipt["open"]} == {
        FRONTIER_IFS_SUBJECT: True,
        FRONTIER_GFS_SUBJECT: True,
    }
    assert [row["subject_id"] for row in receipt["suppressed"]] == [OLD_IFS_SUBJECT, OLD_GFS_SUBJECT]


def test_i2_fix_a_alone_alerts_three_hours_in_through_the_bypassed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time gate cannot have fired yet; the frontier bypass alone alerts.

    The freeze 25 passes in (152 minutes after the last submission, well
    under the 360-minute gate), with the progress pass still inside the scan.
    The frontier entries have been counted 25 times --- the reconstruction
    walks the measured 338 back to this instant --- which is over the
    20-pass circuit threshold.  This is the "about three hours after the last
    submission" the issue says an unsuppressed verdict 11 would have given.
    """

    root = _evidence_root(tmp_path)
    _newest, now = _write_freeze(root, passes_after_progress=25)
    _write_tracker(
        root, _freeze_tracker(frontier_passes=25, old_passes=(2200 - 313, 2098 - 313))
    )

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=now, config=SHIPPED_CONFIG)

    assert status == 1
    assert receipt["verdict"] == "no_progress_circuit_open"
    signals = receipt["signals"]
    assert signals["in_flight_run_ended_by"] == "progress_pass"
    assert signals["in_flight_run_start_is_lower_bound"] is False
    assert signals["in_flight_run_started_at"] == "2026-09-25T16:45:42Z"
    assert signals["in_flight_run_span_minutes"] == pytest.approx(25 * 6.0926, abs=0.1)
    assert signals["in_flight_run_span_minutes"] < signals["in_flight_minutes"]
    assert signals["circuit_bypassed_frontier_entries"] == 2
    assert sorted(row["subject_id"] for row in receipt["open"]) == sorted(
        [FRONTIER_IFS_SUBJECT, FRONTIER_GFS_SUBJECT]
    )
    assert all(row["suppression_bypassed_frontier"] is True for row in receipt["open"])
    assert [row["subject_id"] for row in receipt["suppressed"]] == [OLD_IFS_SUBJECT, OLD_GFS_SUBJECT]


def test_i3_fix_b_alone_alerts_through_the_time_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No frontier entry in the tracker at all: nothing for the bypass to open.

    The full freeze with only the two old-cycle entries.  They stay
    suppressed and listed; the verdict comes from the in-flight time gate
    alone, measured as a lower bound because the last progress pass left the
    scan window 256 passes ago.
    """

    root = _evidence_root(tmp_path)
    _write_freeze(root, passes_after_progress=FREEZE_PASSES, on_disk=130)
    _write_tracker(root, _freeze_tracker(frontier_passes=None))

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=FREEZE_NOW, config=SHIPPED_CONFIG)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["open"] == []
    assert receipt["signals"]["circuit_bypassed_frontier_entries"] == 0
    assert receipt["signals"]["in_flight_run_start_is_lower_bound"] is True
    assert [row["subject_id"] for row in receipt["suppressed"]] == [OLD_IFS_SUBJECT, OLD_GFS_SUBJECT]


def test_i4_a_healthy_forecast_in_flight_with_old_cycle_entries_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must-preserve: `active_duplicate_pipeline` below the gate is health.

    Thirteen passes after a submitting pass: 79 minutes, the longest forecast
    #1736 measured.  The tracker holds only the two old-cycle entries, which
    are not on the frontier, stay suppressed and stay listed.
    """

    root = _evidence_root(tmp_path)
    _newest, now = _write_freeze(root, passes_after_progress=13)
    _write_tracker(root, _freeze_tracker(frontier_passes=None))

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=now, config=SHIPPED_CONFIG)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["passes"]["in_flight_held_count"] == 13
    assert receipt["passes"]["idle_count"] == 0
    assert receipt["signals"]["in_flight_run_span_minutes"] == pytest.approx(79.2, abs=0.1)
    assert receipt["open"] == []
    assert [row["subject_id"] for row in receipt["suppressed"]] == [OLD_IFS_SUBJECT, OLD_GFS_SUBJECT]
    assert all(row["matched_rule"] == SUPPRESSED_REASON for row in receipt["suppressed"])


def test_i5_a_progress_pass_inside_the_scan_gives_an_exact_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sixty passes in: 365 minutes since the last submission, measured exactly."""

    root = _evidence_root(tmp_path)
    _newest, now = _write_freeze(root, passes_after_progress=60)
    _write_tracker(root, _freeze_tracker(frontier_passes=None))

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=now, config=SHIPPED_CONFIG)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    signals = receipt["signals"]
    assert signals["in_flight_run_passes"] == 60
    assert signals["in_flight_run_started_at"] == "2026-09-25T16:45:42Z"
    assert signals["in_flight_run_ended_by"] == "progress_pass"
    assert signals["in_flight_run_start_is_lower_bound"] is False
    assert signals["in_flight_run_span_minutes"] == pytest.approx(365.56, abs=0.1)


def test_i5b_one_pass_short_of_the_gate_is_still_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty-nine passes in: 359.5 minutes, under the gate by one interval."""

    root = _evidence_root(tmp_path)
    _newest, now = _write_freeze(root, passes_after_progress=59)

    status, receipt = _run_at(tmp_path, monkeypatch, root=root, now=now, config=SHIPPED_CONFIG)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["signals"]["in_flight_run_span_minutes"] == pytest.approx(359.46, abs=0.1)


def test_i6_a_neutral_newest_pass_does_not_switch_the_bypass_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frontier is the newest NON-NEUTRAL pass.

    A lock-contended pass carries no candidate lists.  Were the newest pass
    taken outright, the two frontier entries would fall back under their
    suppression on every tick where such a pass happened to be newest.
    """

    root = _evidence_root(tmp_path)
    newest, _ = _write_freeze(root, passes_after_progress=25)
    neutral_started_at = newest + FREEZE_INTERVAL
    _write_pass(
        root,
        _freeze_pass_name(neutral_started_at, 26),
        _pass_payload(
            started_at=neutral_started_at, status="lock_contended", guard=False, counts=False
        ),
    )
    _write_tracker(root, _freeze_tracker(frontier_passes=25))

    status, receipt = _run_at(
        tmp_path,
        monkeypatch,
        root=root,
        now=neutral_started_at + timedelta(seconds=80),
        config=SHIPPED_CONFIG,
    )

    assert status == 1
    assert receipt["verdict"] == "no_progress_circuit_open"
    assert receipt["passes"]["neutral_count"] == 1
    assert receipt["frontier"]["pass"] == _freeze_pass_name(newest, 25)
    assert receipt["frontier"]["cycles"] == ["gfs_2026092500", "ifs_2026092500"]
    assert receipt["signals"]["circuit_bypassed_frontier_entries"] == 2


def test_i7_the_bypass_is_source_inclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same cycle, other source: not on the frontier, still suppressed."""

    root = _evidence_root(tmp_path)
    started_at = NOW - timedelta(minutes=5)
    _write_pass(
        root,
        _pass_name(5, "0" * 12),
        _pass_payload(
            started_at=started_at,
            skipped=[_skip_row("active_duplicate_pipeline", "gfs", FREEZE_CYCLE)],
        ),
    )
    _write_tracker(
        root,
        [
            _tracker_entry(
                reason=SUPPRESSED_REASON,
                consecutive_passes=30,
                subject_kind="job",
                subject_id=FRONTIER_IFS_SUBJECT,
            )
        ],
    )

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["frontier"]["cycles"] == ["gfs_2026092500"]
    assert [row["subject_id"] for row in receipt["suppressed"]] == [FRONTIER_IFS_SUBJECT]


@pytest.mark.parametrize(
    ("subject_id", "keys", "expected"),
    [
        (FRONTIER_IFS_SUBJECT, {"ifs_2026092500"}, True),
        (FRONTIER_IFS_SUBJECT, {"gfs_2026092500"}, False),
        (OLD_IFS_SUBJECT, {"ifs_2026092500", "gfs_2026092500"}, False),
        ("job_cycle_xgfs_2026092500_convert", {"gfs_2026092500"}, False),
        ("JOB_CYCLE_IFS_2026092500_CONVERT", {"ifs_2026092500"}, True),
        ("gfs:2026-09-25T00:00:00+00:00", {"gfs_2026092500"}, False),
        (FRONTIER_IFS_SUBJECT, set(), False),
    ],
)
def test_i8_a_subject_is_on_the_frontier_by_source_and_cycle(
    subject_id: str, keys: set[str], expected: bool
) -> None:
    assert probe.subject_on_frontier(subject_id, frozenset(keys)) is expected


def test_i9_the_frontier_keys_fold_the_source_case_and_read_every_candidate_list() -> None:
    cycle = "2026-09-25T00:00:00Z"
    payload = {
        "candidates": [{"source_id": "IFS", "cycle_time": cycle}],
        "blocked_candidates": [{"source": "gfs", "cycle_time_utc": "2026-09-25T12:00:00Z"}],
        "skipped_candidates": [
            {"source_id": "ECMWF_AIFS", "cycle_time": "2026-09-24T12:00:00+00:00"},
            {"source_id": "gfs", "cycle_time": "not-a-time"},
            {"cycle_time": cycle},
            "not-a-row",
        ],
    }

    assert probe.pass_cycle_keys(payload) == frozenset(
        {"ifs_2026092500", "gfs_2026092512", "ecmwf_aifs_2026092412"}
    )


def _zero_zero_payload(**extra: object) -> dict[str, object]:
    payload = _pass_payload(started_at=NOW)
    payload.update(extra)
    return payload


IN_FLIGHT_SHAPES = {
    "active_duplicate": _zero_zero_payload(
        skipped_candidates=[_skip_row("active_duplicate_pipeline", "gfs", FREEZE_CYCLE)]
    ),
    "one_in_flight_among_terminal": _zero_zero_payload(
        skipped_candidates=[
            _skip_row("terminal_hydro_success", "gfs", FREEZE_CYCLE),
            _skip_row("active_slurm_job", "gfs", FREEZE_CYCLE),
        ]
    ),
    "unknown_reason": _zero_zero_payload(
        skipped_candidates=[_skip_row("a_reason_added_next_year", "gfs", FREEZE_CYCLE)]
    ),
    "row_without_a_reason": _zero_zero_payload(skipped_candidates=[{"source_id": "gfs"}]),
    "row_that_is_not_an_object": _zero_zero_payload(skipped_candidates=["summary lost"]),
    "list_that_is_not_a_list": _zero_zero_payload(skipped_candidates="dropped"),
    "count_with_no_list": _zero_zero_payload(
        counts={"submitted_count": 0, "blocked_candidate_count": 0, "skipped_candidate_count": 96}
    ),
    "count_with_an_emptied_list": _zero_zero_payload(
        counts={"submitted_count": 0, "blocked_candidate_count": 0, "skipped_candidate_count": 96},
        skipped_candidates=[],
    ),
    "count_above_the_terminal_rows_present": _zero_zero_payload(
        counts={"submitted_count": 0, "blocked_candidate_count": 0, "skipped_candidate_count": 2},
        skipped_candidates=[_skip_row("terminal_hydro_success", "gfs", FREEZE_CYCLE)],
    ),
}

IDLE_SHAPES = {
    "no_skipped_candidate_at_all": _zero_zero_payload(),
    "an_empty_list_and_a_zero_count": _zero_zero_payload(skipped_candidates=[]),
    "every_row_terminal": _zero_zero_payload(
        counts={"submitted_count": 0, "blocked_candidate_count": 0, "skipped_candidate_count": 5},
        skipped_candidates=[
            _skip_row(reason, "gfs", FREEZE_CYCLE)
            for reason in (
                "completed_duplicate_pipeline",
                "terminal_hydro_success",
                "terminal_completed_cycle",
                "terminal_pipeline_success",
                "duplicate_candidate_identity",
            )
        ],
    ),
}


@pytest.mark.parametrize("case", sorted(IN_FLIGHT_SHAPES))
def test_i10_a_non_terminal_or_unreadable_skip_is_in_flight_held(case: str) -> None:
    shape, submitted, blocked = probe.classify_pass(IN_FLIGHT_SHAPES[case])

    assert (shape, submitted, blocked) == ("in_flight_held", 0, 0)


@pytest.mark.parametrize("case", sorted(IDLE_SHAPES))
def test_i11_a_pass_with_nothing_in_flight_is_still_idle(case: str) -> None:
    shape, _submitted, _blocked = probe.classify_pass(IDLE_SHAPES[case])

    assert shape == "idle"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"submitted": 2}, "progress"),
        ({"blocked": 3}, "blocked"),
        ({"guard": False}, "neutral"),
        ({"status": "resource_limit_blocked"}, "neutral"),
    ],
)
def test_i12_in_flight_skips_do_not_outrank_the_existing_shapes(
    extra: dict[str, object], expected: str
) -> None:
    payload = _pass_payload(
        started_at=NOW,
        skipped=[_skip_row("active_duplicate_pipeline", "gfs", FREEZE_CYCLE)],
        **extra,
    )

    assert probe.classify_pass(payload)[0] == expected


def _shape_run(root: Path, shapes: list[str], *, spacing_minutes: int = 2) -> None:
    """Write newest-first ``shapes`` two minutes apart, newest five minutes ago."""

    for index, shape in enumerate(shapes):
        minutes = 5 + index * spacing_minutes
        _write_pass(
            root,
            _pass_name(minutes, f"{index:012x}"),
            _pass_payload(
                started_at=NOW - timedelta(minutes=minutes),
                blocked=4 if shape == "blocked" else 0,
                submitted=1 if shape == "progress" else 0,
                skipped=(
                    [_skip_row("active_duplicate_pipeline", "gfs", FREEZE_CYCLE)]
                    if shape == "held"
                    else None
                ),
            ),
        )


def test_i13_an_in_flight_held_pass_does_not_clear_the_count_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three blocked passes around one in-flight-held pass reach the threshold of 3.

    Read as idle, the held pass halts the search at a streak of 1.
    """

    root = _evidence_root(tmp_path)
    _shape_run(root, ["blocked", "held", "blocked", "blocked"])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 1
    assert receipt["verdict"] == "submission_stalled"
    assert receipt["signals"]["no_submission_streak"] == 3
    assert receipt["signals"]["no_submission_neutral_skipped"] == 0
    assert receipt["passes"]["in_flight_held_count"] == 1


def test_i14_an_in_flight_held_pass_does_not_extend_the_count_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two blocked and two held is a streak of 2, under the threshold of 3."""

    root = _evidence_root(tmp_path)
    _shape_run(root, ["blocked", "held", "held", "blocked"])

    status, receipts, _log = _run(tmp_path, monkeypatch, evidence_root=root)
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["signals"]["no_submission_streak"] == 2
    assert receipt["signals"]["in_flight_run_passes"] == 4
    assert receipt["signals"]["in_flight_run_span_minutes"] == 6.0
    assert receipt["signals"]["in_flight_run_start_is_lower_bound"] is True


def _record(minutes_ago: int, shape: str) -> probe.PassRecord:
    return probe.PassRecord(
        name=f"scheduler_{minutes_ago}.json",
        started_at=NOW - timedelta(minutes=minutes_ago),
        status="planned",
        shape=shape,
        submitted_count=0,
        blocked_candidate_count=0,
    )


@pytest.mark.parametrize(
    ("shapes", "window", "passes", "start_minutes_ago", "ended_by", "lower_bound", "span"),
    [
        # Ended by the last progress pass: the start is that pass's start.
        (["in_flight_held", "blocked", "in_flight_held", "progress"], 10, 3, 40, "progress_pass", False, 30.0),
        # Ended by an idle pass: the start is the oldest pass of the run.
        (["in_flight_held", "in_flight_held", "idle", "progress"], 10, 2, 20, "idle_pass", False, 10.0),
        # Ended by the window: the same oldest pass, but a lower bound.
        (["in_flight_held", "in_flight_held", "in_flight_held"], 10, 3, 30, "scan_window", True, 20.0),
        (["in_flight_held", "in_flight_held", "progress"], 2, 2, 20, "scan_window", True, 10.0),
        # Neutral passes are walked past and cost nothing.
        (["neutral", "in_flight_held", "neutral", "progress"], 10, 1, 40, "progress_pass", False, 20.0),
        # No run at all: the newest non-neutral pass cleared or submitted.
        (["progress", "in_flight_held"], 10, 0, None, None, False, None),
        (["idle", "blocked"], 10, 0, None, None, False, None),
        (["neutral"], 10, 0, None, None, False, None),
        ([], 10, 0, None, None, False, None),
    ],
)
def test_i15_the_run_start_and_span_follow_how_the_run_ended(
    shapes: list[str],
    window: int,
    passes: int,
    start_minutes_ago: int | None,
    ended_by: str | None,
    lower_bound: bool,
    span: float | None,
) -> None:
    records = [_record(10 * (index + 1), shape) for index, shape in enumerate(shapes)]

    run = probe.in_flight_run(records, window=window)

    assert run.passes == passes
    assert run.ended_by == ended_by
    assert run.start_is_lower_bound is lower_bound
    assert run.span_minutes == span
    assert run.started_at == (
        None if start_minutes_ago is None else NOW - timedelta(minutes=start_minutes_ago)
    )


def test_i16_the_time_gate_is_measured_between_passes_not_to_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long pass in flight ages nothing: the span ends at the newest artifact.

    One in-flight-held pass 8 minutes after a submitting pass, then a pass
    that has been running for three hours.  Measured to the clock the run
    would be 188 minutes; measured between artifacts it is 8.
    """

    root = _evidence_root(tmp_path)
    _shape_run(root, ["held", "progress"], spacing_minutes=8)

    status, receipts, _log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        now=NOW + timedelta(minutes=175),
        service_properties=_in_flight_service_properties(),
        timer_properties=_timer_properties(last_trigger=_systemd_timestamp(NOW + timedelta(minutes=2))),
    )
    receipt = _receipt(receipts)

    assert status == 0
    assert receipt["verdict"] == "ok"
    assert receipt["signals"]["in_flight_run_span_minutes"] == 8.0


@pytest.mark.parametrize(("scan_limit", "gate", "refused"), [(96, 415, False), (96, 416, True), (64, 360, True)])
def test_i17_a_gate_the_scan_window_cannot_span_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scan_limit: int,
    gate: int,
    refused: bool,
) -> None:
    """`gate <= (scan_limit - 12 - 1) x 5`, refused before any evidence is read.

    The third case is the pre-#2662 shipped scan of 64 kept by a drop-in: it
    spans only 255 guaranteed minutes, so the shipped 360-minute gate could
    stay unmet through a freeze of any length.
    """

    root = _evidence_root(tmp_path)
    _healthy_passes(root)

    status, receipts, log = _run(
        tmp_path,
        monkeypatch,
        evidence_root=root,
        config={**SHIPPED_CONFIG, probe.ENV_SCAN_LIMIT: str(scan_limit), ENV_IN_FLIGHT_MINUTES: str(gate)},
    )
    stderr = capsys.readouterr().err

    if not refused:
        assert status == 0
        assert _receipt(receipts)["signals"]["in_flight_minutes"] == gate
        return
    assert status == 2
    assert not log.exists(), "systemctl was invoked before the configuration was validated"
    assert not (receipts / "latest.json").exists()
    assert f"{ENV_IN_FLIGHT_MINUTES}={gate} must be at most {(scan_limit - 13) * 5}" in stderr
    assert "or the verdict is unreachable" in stderr


def test_i18_the_runbook_documents_the_new_shape_the_gate_and_the_bypass() -> None:
    text = RUNBOOK.read_text()

    assert "in_flight_held" in text
    assert ENV_IN_FLIGHT_MINUTES in text
    assert "suppression_bypassed_frontier" in text
    assert "in_flight_run_start_is_lower_bound" in text
