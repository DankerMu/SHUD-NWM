"""#2668: ``list-operator-actions`` lists held reservations restart reconcile could not resolve.

A held forecast master freezes the whole source's forward lane (#2667) but never
reaches ``blocked_candidates``, so the listing reads it from the pass evidence
``restart_reconcile.reserved_unbound.outcomes[]`` by a closed per-action table
(design Decision 6).  Every test drives the shipped CLI entry against REAL pass
files on disk; outcome rows use the producer's keys
(``scheduler_runtime._serialize_reserved_unbound_outcome``).  The closure pin
reads the reconcile producer with ``ast``, never the module under test.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli, operator_action_listing, scheduler_evidence_payload, scheduler_runtime
from services.orchestrator.operator_action_listing_held import (
    HELD_RESERVATION_AGED_LISTED_ACTIONS,
    HELD_RESERVATION_ALWAYS_LISTED_ACTIONS,
    HELD_RESERVATION_NEVER_LISTED_ACTIONS,
    HELD_RESERVATION_OPERATOR_COMMANDS,
)
from tests.test_operator_action_listing import _permanent_failure_row, _run, _write_pass

_RECONCILE = Path(__file__).resolve().parents[1] / "services" / "orchestrator" / "reconcile.py"
_STARTED = datetime(2026, 7, 12, 12, tzinfo=UTC)
_JOB = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
_FORCING_JOB = "job_cycle_gfs_2026071200_convert_cohort_36e4f7b9bf80_forcing"
_PASS_A = "scheduler_2026071212_aaaaaaaaaaaa.json"
_PASS_B = "scheduler_2026071218_bbbbbbbbbbbb.json"
_RUNBOOK = "docs/runbooks/failed-basin-retry.md"


def _iso(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _outcome(
    action: str, *, job_id: str = _JOB, anchor: datetime | None = _STARTED - timedelta(hours=7)
) -> dict[str, Any]:
    """One reserved-unbound outcome row in the producer's key spelling."""

    row: dict[str, Any] = {
        "job_id": job_id,
        "idempotency_key": "cycle_gfs_2026071200_forecast_fixture:forecast",
        "action": action,
        "status": "reserved",
        "reconciliation_source": "slurm_exact_comment",
        "reconciliation_decision": "accounting_unavailable",
        "reconciliation_reason_class": "comment_accounting_unproven",
        "submission_attempt": 1,
    }
    if anchor is not None:
        row["submission_attempt_started_at"] = _iso(anchor)
    return row


def _write_held_pass(
    root: Path,
    name: str,
    *,
    mtime: int,
    outcomes: list[dict[str, Any]] | None = None,
    restart_reconcile: Any = None,
    started_at: datetime = _STARTED,
    **kwargs: Any,
) -> dict[str, Any]:
    """A scope-complete evaluating pass plus the ``restart_reconcile`` block the runtime writes."""

    path = _write_pass(root, name, mtime=mtime, **kwargs)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["started_at"] = _iso(started_at)
    if restart_reconcile is None and outcomes is not None:
        restart_reconcile = {
            "status": "completed",
            "reserved_unbound": {"count": len(outcomes), "outcomes": outcomes},
            "inflight": {"count": 0, "outcomes": []},
        }
    if restart_reconcile is not None:
        payload["restart_reconcile"] = restart_reconcile
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return payload


def _held(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload["operator_actions"] if row["decision"] == "held_reservation_unresolved"]


# --- the closed per-action table ------------------------------------------------------------


def _reserved_unbound_action_vocabulary() -> set[str]:
    """Every ``action`` a ``ReservationReconcileOutcome`` can carry, read from reconcile.py source."""

    tree = ast.parse(_RECONCILE.read_text(encoding="utf-8"))
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def strings(node: ast.AST, scope: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return strings(node.body, scope) | strings(node.orelse, scope)
        if isinstance(node, ast.Name) and node.id in constants:
            return {constants[node.id]}
        if isinstance(node, ast.Name):
            # A local variable: every value assigned to it in the enclosing function.
            found: set[str] = set()
            for item in ast.walk(scope):
                if isinstance(item, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == node.id for target in item.targets
                ):
                    found |= strings(item.value, scope)
            assert found, f"unresolvable action variable {node.id}"
            return found
        raise AssertionError(f"unresolvable action expression {ast.dump(node)}")

    vocabulary: set[str] = set()
    calls = 0
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for call in ast.walk(function):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "ReservationReconcileOutcome"
            ):
                calls += 1
                (action,) = [keyword.value for keyword in call.keywords if keyword.arg == "action"]
                vocabulary |= strings(action, function)
    assert calls >= 10
    return vocabulary


def test_the_held_action_table_is_closed_over_the_reconcile_reserved_unbound_vocabulary() -> None:
    vocabulary = _reserved_unbound_action_vocabulary()
    # Independent literal of the producer's vocabulary: a new action fails here first.
    assert vocabulary == {
        "bound",
        "reservation_lost",
        "absence_retry_permitted",
        "identity_mismatch_released",
        "ambiguous_fallback_match",
        "legacy_unversioned_read_only",
        "multiple_matches_blocked",
        "query_unavailable",
        "fallback_no_match",
        "absence_unconfirmed",
        "identity_mismatch_blocked",
        "stale_attempt_blocked",
        "journal_quarantined",
    }
    never = set(HELD_RESERVATION_NEVER_LISTED_ACTIONS)
    always = set(HELD_RESERVATION_ALWAYS_LISTED_ACTIONS)
    aged = set(HELD_RESERVATION_AGED_LISTED_ACTIONS)
    assert never | always | aged == vocabulary
    assert not (never & always) and not (never & aged) and not (always & aged)
    commands = set(HELD_RESERVATION_ALWAYS_LISTED_ACTIONS.values()) | set(HELD_RESERVATION_AGED_LISTED_ACTIONS.values())
    assert commands == set(HELD_RESERVATION_OPERATOR_COMMANDS) == {"bind-reserved-job", "triage", "escalate"}


_TABLE = [
    ("bound", None),
    ("reservation_lost", None),
    ("absence_retry_permitted", None),
    ("identity_mismatch_released", None),
    ("ambiguous_fallback_match", "bind-reserved-job"),
    ("legacy_unversioned_read_only", "escalate"),
    ("multiple_matches_blocked", "escalate"),
    ("query_unavailable", "triage"),
    ("fallback_no_match", "triage"),
    ("absence_unconfirmed", "triage"),
    ("identity_mismatch_blocked", "escalate"),
    ("stale_attempt_blocked", "escalate"),
    ("journal_quarantined", "escalate"),
    ("a_future_action_outside_the_table", "escalate"),
]


@pytest.mark.parametrize(("action", "command"), _TABLE)
def test_every_action_of_the_table_is_listed_as_tabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str, command: str | None
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome(action)])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert payload is not None
    if command is None:
        assert (code, payload["operator_actions"]) == (0, [])
        return
    assert code == 1
    expected = {
        "decision": "held_reservation_unresolved",
        "reason": action,
        "job_id": _JOB,
        "source_id": "gfs",
        "cycle_time": "2026-07-12T00:00:00Z",
        "submission_attempt_started_at": "2026-07-12T05:00:00Z",
        "operator_command": command,
        "recovery_runbook": _RUNBOOK,
        "first_seen_pass": _PASS_A,
        "last_seen_pass": _PASS_A,
        "seen_in_passes": 1,
    }
    if action == "legacy_unversioned_read_only":
        expected["follow_up_issue"] = "#2674"
    assert payload["operator_actions"] == [expected]
    assert payload["operator_action_count"] == 1


@pytest.mark.parametrize(
    "action", ["ambiguous_fallback_match", "legacy_unversioned_read_only", "multiple_matches_blocked"]
)
def test_always_listed_actions_ignore_the_anchor_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    _write_held_pass(
        tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome(action, anchor=_STARTED - timedelta(minutes=5))]
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert [row["reason"] for row in _held(payload)] == [action]


@pytest.mark.parametrize(
    ("anchor", "listed"),
    [
        (_STARTED - timedelta(hours=5, minutes=59), False),
        (_STARTED - timedelta(hours=6), True),
        (_STARTED - timedelta(hours=30), True),
        (None, True),  # a pass written before #2668 carries no anchor: unknown age is listed
    ],
)
def test_query_unavailable_is_listed_only_once_its_anchor_is_six_hours_old_or_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], anchor: datetime | None, listed: bool
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("query_unavailable", anchor=anchor)])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert [row["operator_command"] for row in _held(payload)] == (["triage"] if listed else [])
    assert code == (1 if listed else 0)
    if listed:
        assert _held(payload)[0]["submission_attempt_started_at"] == (_iso(anchor) if anchor else None)


@pytest.mark.parametrize(
    ("outcome", "anchor"),
    [
        ("query_unavailable", _STARTED - timedelta(hours=7)),
        ("multiple_matches_blocked", _STARTED - timedelta(minutes=1)),
    ],
)
def test_a_held_forcing_row_is_escalated_with_its_follow_up_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], outcome: str, anchor: datetime
) -> None:
    """Neither bind nor demote accepts a forcing master; its exit is tracked in #2675."""

    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome(outcome, job_id=_FORCING_JOB, anchor=anchor)])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    (entry,) = _held(payload)
    assert (entry["job_id"], entry["reason"], entry["operator_command"], entry["follow_up_issue"]) == (
        _FORCING_JOB,
        outcome,
        "escalate",
        "#2675",
    )
    assert (entry["source_id"], entry["cycle_time"]) == ("gfs", "2026-07-12T00:00:00Z")


def test_a_young_forcing_query_unavailable_keeps_the_age_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_held_pass(
        tmp_path,
        _PASS_A,
        mtime=1_000,
        outcomes=[_outcome("query_unavailable", job_id=_FORCING_JOB, anchor=_STARTED - timedelta(hours=1))],
    )
    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)
    assert (code, _held(payload)) == (0, [])


@pytest.mark.parametrize(
    ("job_id", "command"),
    [
        ("job_cycle_gfs_2026071200_forecast_fixture_forecast_retry_2", "bind-reserved-job"),
        ("job_cycle_IFS_2026071212_convert_cohort_ab12_run_shud_forecast_array", "bind-reserved-job"),
        ("not_a_master_job_id", "escalate"),
    ],
)
def test_the_forecast_master_test_reads_the_stage_suffix_and_fails_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], job_id: str, command: str
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("ambiguous_fallback_match", job_id=job_id)])
    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)
    assert code == 1
    (entry,) = _held(payload)
    assert entry["operator_command"] == command
    assert ("follow_up_issue" in entry) is (command == "escalate")


# --- dedup, resolution, unscanned passes ------------------------------------------------------


def test_a_job_held_across_passes_is_listed_once_with_first_and_last_seen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("query_unavailable")])
    _write_held_pass(
        tmp_path,
        _PASS_B,
        mtime=2_000,
        started_at=_STARTED + timedelta(hours=6),
        outcomes=[_outcome("ambiguous_fallback_match"), _outcome("query_unavailable", job_id=_FORCING_JOB)],
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    by_job = {row["job_id"]: row for row in _held(payload)}
    assert set(by_job) == {_JOB, _FORCING_JOB}
    # Newest pass wins every value; first seen and the count carry over.
    assert (by_job[_JOB]["reason"], by_job[_JOB]["operator_command"]) == (
        "ambiguous_fallback_match",
        "bind-reserved-job",
    )
    assert (by_job[_JOB]["first_seen_pass"], by_job[_JOB]["last_seen_pass"], by_job[_JOB]["seen_in_passes"]) == (
        _PASS_A,
        _PASS_B,
        2,
    )
    assert (by_job[_FORCING_JOB]["first_seen_pass"], by_job[_FORCING_JOB]["seen_in_passes"]) == (_PASS_B, 1)


@pytest.mark.parametrize("newer_outcomes", [[], [_outcome("bound")], [_outcome("reservation_lost")]])
def test_a_held_job_resolved_before_the_next_reconciling_pass_is_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], newer_outcomes: list[dict[str, Any]]
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("ambiguous_fallback_match")])
    _write_held_pass(tmp_path, _PASS_B, mtime=2_000, outcomes=newer_outcomes)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert _held(payload) == []
    assert (code, payload["operator_action_count"]) == (0, 0)


def test_dropping_a_resolved_hold_leaves_other_actions_to_decide_the_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("ambiguous_fallback_match")])
    _write_held_pass(tmp_path, _PASS_B, mtime=2_000, outcomes=[], blocked=[_permanent_failure_row()])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert [row["decision"] for row in payload["operator_actions"]] == ["permanent_failure"]


@pytest.mark.parametrize(
    ("restart_reconcile", "reason"),
    [
        ({"status": "skipped", "reason": "reconcile_store_unavailable"}, "restart_reconcile_skipped"),
        (
            {
                "status": "error",
                "reserved_unbound_error": "OperationalError: reconcile sacct session unavailable",
                "inflight": {"count": 0, "outcomes": []},
            },
            "reserved_unbound_error",
        ),
        (None, "restart_reconcile_absent"),
    ],
)
def test_a_pass_whose_reserved_lane_did_not_run_is_unscanned_and_changes_no_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restart_reconcile: Any, reason: str
) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, restart_reconcile=restart_reconcile)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload["restart_reconcile_unscanned_passes"] == [{"pass": _PASS_A, "reason": reason}]
    assert payload["operator_actions"] == []


def test_an_unscanned_newer_pass_does_not_drop_a_held_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("ambiguous_fallback_match")])
    _write_held_pass(tmp_path, _PASS_B, mtime=2_000, restart_reconcile={"status": "skipped", "reason": "x"})

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert [(row["job_id"], row["last_seen_pass"]) for row in _held(payload)] == [(_JOB, _PASS_A)]
    assert payload["restart_reconcile_unscanned_passes"] == [{"pass": _PASS_B, "reason": "restart_reconcile_skipped"}]


def test_a_size_compacted_pass_is_read_like_a_full_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("query_unavailable")])
    compact = scheduler_evidence_payload._compact_bounded_restart_reconcile(original["restart_reconcile"])
    assert compact["reserved_unbound"]["outcomes"] == [
        {
            "job_id": _JOB,
            "action": "query_unavailable",
            "status": "reserved",
            "reconciliation_reason_class": "comment_accounting_unproven",
            "submission_attempt_started_at": "2026-07-12T05:00:00Z",
        }
    ]
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, restart_reconcile=compact)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert [(row["reason"], row["submission_attempt_started_at"]) for row in _held(payload)] == [
        ("query_unavailable", "2026-07-12T05:00:00Z")
    ]


def test_argparse_entrypoint_matches_the_click_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_held_pass(tmp_path, _PASS_A, mtime=1_000, outcomes=[_outcome("ambiguous_fallback_match")])
    click_code, click_payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)
    argparse_code = cli._argparse_main(["list-operator-actions", "--evidence-root", str(tmp_path)])
    assert (click_code, argparse_code) == (1, 1)
    assert json.loads(capsys.readouterr().out) == click_payload


def test_the_help_text_names_the_held_decision_its_commands_and_the_fifth_boundary() -> None:
    help_text = operator_action_listing.LIST_OPERATOR_ACTIONS_HELP
    for token in (
        "held_reservation_unresolved",
        "restart_reconcile_unscanned_passes",
        "submission_attempt_started_at",
        "#2674",
        "#2675",
        *HELD_RESERVATION_OPERATOR_COMMANDS,
        *HELD_RESERVATION_NEVER_LISTED_ACTIONS,
        *HELD_RESERVATION_ALWAYS_LISTED_ACTIONS,
        *HELD_RESERVATION_AGED_LISTED_ACTIONS,
    ):
        assert token in help_text, token
    assert "fifth known boundary of exit 0" in help_text


# --- evidence: the attempt anchor rides the serialized outcome and survives compaction ---------


@pytest.fixture
def _utc_local_time() -> Iterator[None]:
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    with _pinned_local_timezone("UTC"):
        yield


@pytest.mark.usefixtures("_utc_local_time")
def test_the_serialized_reserved_unbound_outcome_carries_the_durable_attempt_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.orchestrator.reconcile import ReservationReconcileOutcome
    from tests.orchestrator_bind_reserved_job_helpers import JOB_ID, held_repository

    repository = held_repository(tmp_path, monkeypatch)
    outcome = ReservationReconcileOutcome(
        job_id=JOB_ID, idempotency_key="k", action="ambiguous_fallback_match", status="reserved"
    )

    serialized = scheduler_runtime._serialize_reserved_unbound_outcome(repository, outcome)

    assert serialized["submission_attempt_started_at"] == "2026-07-12T00:00:13Z"
    bounded = scheduler_evidence_payload._compact_bounded_restart_reconcile(
        {"status": "completed", "reserved_unbound": {"outcomes": [serialized]}}
    )
    assert bounded["reserved_unbound"]["outcomes"][0]["submission_attempt_started_at"] == "2026-07-12T00:00:13Z"
    # Inflight outcomes keep their exact pre-change key set.
    assert "submission_attempt_started_at" not in scheduler_runtime._restart_reconcile_attempt_evidence(
        repository, JOB_ID
    )
