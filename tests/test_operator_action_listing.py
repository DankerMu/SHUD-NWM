"""``list-operator-actions`` (#1186): enumerate manual-action decisions from db-free pass evidence.

Every test drives the shipped CLI entry ``cli.main([...])`` against REAL
``scheduler_*.json`` files on disk.  Expected values are literals taken from the
producer shapes (``scheduler_state_failure.py`` permanent/cancelled evidence,
``scheduler_candidates.py`` breaker/budget evidence, ``scheduler_discovery.py``
breaker-released ``source_cycles`` entries), never recomputed by the module under
test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli, scheduler_evidence_payload

_EVIDENCE_ROOT_ENV = "NHMS_SCHEDULER_EVIDENCE_ROOT"


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any] | None, str]:
    """``main`` prefers click and, with argv supplied, raises ``SystemExit`` on non-zero."""

    try:
        code = cli.main(["list-operator-actions", *argv])
    except SystemExit as error:
        code = int(error.code or 0)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def _candidate_row(
    *,
    model_id: str,
    decision: str,
    reason: str,
    retry_policy: dict[str, Any] | None = None,
    cycle_time: str = "2026-05-21T12:00:00Z",
    extra_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_evidence: dict[str, Any] = {"decision": decision, "reason": reason}
    if retry_policy is not None:
        state_evidence["retry_policy"] = retry_policy
    state_evidence.update(extra_evidence or {})
    return {
        "candidate_id": f"gfs:{cycle_time}:{model_id}:forecast_gfs_deterministic",
        "source_id": "gfs",
        "source": "gfs",
        "cycle_time_utc": cycle_time,
        "cycle_time": cycle_time,
        "model_id": model_id,
        "status": "blocked",
        "reason": reason,
        "state_evidence": state_evidence,
    }


def _permanent_failure_row(model_id: str = "model_a") -> dict[str, Any]:
    return _candidate_row(
        model_id=model_id,
        decision="permanent_failure",
        reason="retry_limit_exhausted",
        retry_policy={"automatic_retry_allowed": False, "manual_retry_required": True, "attempt": 3, "retry_limit": 3},
    )


def _cancelled_row(model_id: str = "model_b") -> dict[str, Any]:
    return _candidate_row(
        model_id=model_id,
        decision="cancelled_manual_retry_required",
        reason="manual_retry_required_after_cancelled",
        retry_policy={"automatic_retry_allowed": False, "manual_retry_required": True, "attempt": 1, "retry_limit": 3},
    )


def _budget_row(model_id: str = "model_c") -> dict[str, Any]:
    return _candidate_row(
        model_id=model_id,
        decision="blocked_strict_warm_start_init_state_mismatch",
        reason="strict_warm_start_retry_budget_exhausted",
        retry_policy={
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "attempt": 12,
            "retry_limit": 12,
        },
    )


def _breaker_row(model_id: str = "model_d") -> dict[str, Any]:
    return _candidate_row(
        model_id=model_id,
        decision="blocked_journal_predecessor_identity_quarantine",
        reason="journal_predecessor_identity_quarantine_breaker_engaged",
        retry_policy={
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "occurrences": 1,
            "occurrence_threshold": 1,
        },
        extra_evidence={
            "journal_predecessor_identity": {
                "recorded_init_state_id": "state_gfs_model_d_2026052112_gfs_2026052100_f012",
                "occurrences": 1,
            }
        },
    )


def _unrelated_blocked_row() -> dict[str, Any]:
    return _candidate_row(
        model_id="model_z",
        decision="blocked_retryable",
        reason="source_cycle_unavailable",
    )


def _write_pass(
    root: Path,
    name: str,
    *,
    mtime: int,
    blocked: list[dict[str, Any]] | None = None,
    source_cycles: list[dict[str, Any]] | None = None,
    candidate_lists: str | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": "nhms.production_scheduler.pass_evidence.v1",
        "pass_id": Path(name).name.removesuffix(".json"),
        "limit": {"max_evidence_bytes": 5_000_000},
        "blocked_candidates": list(blocked or []),
        "source_cycles": list(source_cycles or []),
    }
    if candidate_lists is not None:
        payload["limit"]["candidate_lists"] = candidate_lists
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _action_projection(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (item["model_id"], item["decision"], item["attempt"], item["retry_limit"], item["occurrences"])
        for item in payload["operator_actions"]
    ]


@pytest.mark.parametrize(
    ("row_factory", "expected"),
    [
        (_permanent_failure_row, ("model_a", "permanent_failure", 3, 3, None)),
        (_cancelled_row, ("model_b", "cancelled_manual_retry_required", 1, 3, None)),
        (_budget_row, ("model_c", "blocked_strict_warm_start_init_state_mismatch", 12, 12, None)),
        (_breaker_row, ("model_d", "blocked_journal_predecessor_identity_quarantine", None, None, 1)),
    ],
)
def test_each_operator_action_decision_is_listed_and_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    row_factory: Any,
    expected: tuple[Any, ...],
) -> None:
    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        blocked=[row_factory(), _unrelated_blocked_row()],
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert payload["operator_action_count"] == 1
    assert _action_projection(payload) == [expected]
    action = payload["operator_actions"][0]
    assert action["source_id"] == "gfs"
    assert action["cycle_time"] == "2026-05-21T12:00:00Z"
    assert action["candidate_id"] == f"gfs:2026-05-21T12:00:00Z:{expected[0]}:forecast_gfs_deterministic"
    assert action["first_seen_pass"] == action["last_seen_pass"] == "scheduler_2026052112_aaaaaaaaaaaa.json"
    assert action["seen_in_passes"] == 1
    if expected[1] == "blocked_journal_predecessor_identity_quarantine":
        assert action["recorded_init_state_id"] == "state_gfs_model_d_2026052112_gfs_2026052100_f012"
    else:
        assert action["recorded_init_state_id"] is None


def test_only_unrelated_blocked_candidates_print_an_empty_list_and_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[_unrelated_blocked_row()])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["operator_action_count"] == 0
    assert payload["passes_scanned"] == 1
    assert payload["unreadable_passes"] == []
    assert payload["candidate_lists_dropped_passes"] == []


def test_summarized_pass_still_lists_a_budget_exhausted_candidate_from_bounded_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec scenario: the summary drops ``state_evidence``; the bounded keys carry the numbers."""

    summarized = [
        scheduler_evidence_payload._bounded_candidate_summary(row) for row in (_budget_row(), _breaker_row())
    ]
    assert all("state_evidence" not in row for row in summarized)
    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        blocked=summarized,
        candidate_lists="summarized",
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert _action_projection(payload) == [
        ("model_c", "blocked_strict_warm_start_init_state_mismatch", 12, 12, None),
        ("model_d", "blocked_journal_predecessor_identity_quarantine", None, None, 1),
    ]


def test_legacy_summary_without_the_bounded_retry_keys_reports_null_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy_summary = {
        "candidate_id": "gfs:2026-05-21T12:00:00Z:model_a:forecast_gfs_deterministic",
        "source": "gfs",
        "cycle_time": "2026-05-21T12:00:00Z",
        "model_id": "model_a",
        "status": "blocked",
        "reason": "retry_limit_exhausted",
        "decision": "permanent_failure",
    }
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[legacy_summary])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    (action,) = payload["operator_actions"]
    assert action["source_id"] == "gfs"
    assert (action["attempt"], action["retry_limit"], action["occurrences"]) == (None, None, None)


def test_pre_execution_evidence_is_not_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.pre_execution.json",
        mtime=2_000,
        blocked=[_permanent_failure_row()],
    )
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=1_000, blocked=[_unrelated_blocked_row()])
    (tmp_path / "no-progress-tracker.json").write_text(
        json.dumps({"blocked_candidates": [_budget_row()]}), encoding="utf-8"
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["passes_scanned"] == 1
    assert payload["operator_actions"] == []


def test_passes_limit_scans_the_newest_files_by_modification_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """uuid suffixes do not sort by time: the OLDEST pass name here sorts last."""

    _write_pass(tmp_path, "scheduler_2026052112_ffffffffffff.json", mtime=1_000, blocked=[_permanent_failure_row()])
    _write_pass(tmp_path, "scheduler_2026052112_000000000000.json", mtime=2_000, blocked=[_unrelated_blocked_row()])
    _write_pass(tmp_path, "scheduler_2026052112_111111111111.json", mtime=3_000, blocked=[_cancelled_row()])

    code, payload, _err = _run(["--evidence-root", str(tmp_path), "--passes", "2"], capsys)

    assert code == 1
    assert payload is not None
    assert payload["passes_scanned"] == 2
    assert [item["decision"] for item in payload["operator_actions"]] == ["cancelled_manual_retry_required"]


def test_one_candidate_seen_across_passes_is_listed_once_with_first_and_last_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pass(tmp_path, "scheduler_2026052112_cccccccccccc.json", mtime=1_000, blocked=[_budget_row()])
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=2_000, blocked=[_unrelated_blocked_row()])
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=3_000, blocked=[_budget_row()])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    (action,) = payload["operator_actions"]
    assert action["first_seen_pass"] == "scheduler_2026052112_cccccccccccc.json"
    assert action["last_seen_pass"] == "scheduler_2026052112_bbbbbbbbbbbb.json"
    assert action["seen_in_passes"] == 2


def test_corrupt_pass_is_reported_unreadable_without_aborting_the_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "scheduler_2026052112_dddddddddddd.json"
    corrupt.write_text("{not json", encoding="utf-8")
    os.utime(corrupt, (3_000, 3_000))
    not_object = tmp_path / "scheduler_2026052112_eeeeeeeeeeee.json"
    not_object.write_text("[1, 2]", encoding="utf-8")
    os.utime(not_object, (2_000, 2_000))
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[_permanent_failure_row()])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert payload["passes_scanned"] == 3
    assert payload["unreadable_passes"] == [
        "scheduler_2026052112_dddddddddddd.json",
        "scheduler_2026052112_eeeeeeeeeeee.json",
    ]
    assert [item["decision"] for item in payload["operator_actions"]] == ["permanent_failure"]


def test_missing_evidence_root_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, payload, err = _run(["--evidence-root", str(tmp_path / "absent")], capsys)
    assert code == 2
    assert payload is None
    assert err.strip()

    monkeypatch.delenv(_EVIDENCE_ROOT_ENV, raising=False)
    code, payload, err = _run([], capsys)
    assert code == 2
    assert payload is None
    assert _EVIDENCE_ROOT_ENV in err


def test_evidence_root_defaults_to_the_scheduler_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[_cancelled_row()])
    monkeypatch.setenv(_EVIDENCE_ROOT_ENV, str(tmp_path))

    code, payload, _err = _run([], capsys)

    assert code == 1
    assert payload is not None
    assert payload["evidence_root"] == str(tmp_path)


def test_passes_below_one_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload, _err = _run(["--evidence-root", str(tmp_path), "--passes", "0"], capsys)
    assert code == 2
    assert payload is None


def test_breaker_released_source_cycle_lists_each_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """F1: in backfill mode a breaker-released cycle never reaches ``blocked_candidates``."""

    released = {
        "source_id": "gfs",
        "cycle_id": "gfs_2026052100",
        "cycle_time_utc": "2026-05-21T00:00:00Z",
        "available": True,
        "status": "discovered",
        "selection_status": "not_selected",
        "selection_reason": "journal_predecessor_identity_quarantine_breaker_engaged",
        "journal_predecessor_identity_quarantine": {
            "models": [
                {
                    "model_id": "model_a",
                    "recorded_init_state_id": "state_gfs_model_a_2026052100_gfs_2026052012_f012",
                    "expected_init_state_id": "state_gfs_model_a_2026052100_gfs_2026052018_f006",
                    "occurrences": 1,
                },
                {
                    "model_id": "model_b",
                    "recorded_init_state_id": "state_gfs_model_b_2026052100_gfs_2026052012_f012",
                    "expected_init_state_id": "state_gfs_model_b_2026052100_gfs_2026052018_f006",
                    "occurrences": 2,
                },
            ],
            "occurrence_threshold": 1,
        },
    }
    selected = {
        "source_id": "gfs",
        "cycle_time_utc": "2026-05-21T06:00:00Z",
        "selection_status": "selected",
        "selection_reason": "oldest_available_gap",
    }
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, source_cycles=[released, selected])

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert [
        (item["model_id"], item["cycle_time"], item["decision"], item["occurrences"], item["recorded_init_state_id"])
        for item in payload["operator_actions"]
    ] == [
        (
            "model_a",
            "2026-05-21T00:00:00Z",
            "blocked_journal_predecessor_identity_quarantine",
            1,
            "state_gfs_model_a_2026052100_gfs_2026052012_f012",
        ),
        (
            "model_b",
            "2026-05-21T00:00:00Z",
            "blocked_journal_predecessor_identity_quarantine",
            2,
            "state_gfs_model_b_2026052100_gfs_2026052012_f012",
        ),
    ]
    assert all(item["source_id"] == "gfs" for item in payload["operator_actions"])


def test_no_action_with_dropped_candidate_lists_is_undecidable_and_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F7: the most congested pass would otherwise read as a false negative."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[_unrelated_blocked_row()])
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, candidate_lists="dropped")

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["candidate_lists_dropped_passes"] == ["scheduler_2026052112_bbbbbbbbbbbb.json"]


def test_bounded_candidate_summary_retains_every_retry_policy_key_including_false_and_zero() -> None:
    """A.2: the four ``retry_policy`` pulls survive summarization, falsy values included."""

    row = _candidate_row(
        model_id="model_a",
        decision="permanent_failure",
        reason="retry_limit_exhausted",
        retry_policy={"attempt": 0, "retry_limit": 0, "occurrences": 0, "manual_retry_required": False},
    )

    summary = scheduler_evidence_payload._bounded_candidate_summary(row)

    assert summary["retry_attempt"] == 0
    assert summary["retry_limit"] == 0
    assert summary["retry_occurrences"] == 0
    assert summary["manual_retry_required"] is False
    assert scheduler_evidence_payload._bounded_candidate_summary(summary) == summary
    truthy = scheduler_evidence_payload._bounded_candidate_summary(
        _candidate_row(
            model_id="model_a",
            decision="permanent_failure",
            reason="retry_limit_exhausted",
            retry_policy={"attempt": 3, "retry_limit": 3, "occurrences": 2, "manual_retry_required": True},
        )
    )
    assert (truthy["retry_attempt"], truthy["retry_limit"], truthy["retry_occurrences"]) == (3, 3, 2)
    assert truthy["manual_retry_required"] is True
    # The new keys never collide with a row-level summary key.
    new_keys = {"retry_attempt", "retry_limit", "retry_occurrences", "manual_retry_required"}
    assert not new_keys & set(scheduler_evidence_payload._BOUNDED_CANDIDATE_SUMMARY_KEYS)


def test_argparse_entrypoint_matches_click_receipt_and_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hosts without click fall back to argparse; both must produce the same receipt."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, blocked=[_budget_row()])
    click_code, click_payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    argparse_code = cli._argparse_main(["list-operator-actions", "--evidence-root", str(tmp_path)])
    argparse_payload = json.loads(capsys.readouterr().out)

    assert (click_code, argparse_code) == (1, 1)
    assert argparse_payload == click_payload
    assert cli._argparse_main(["list-operator-actions", "--evidence-root", str(tmp_path / "absent")]) == 2
