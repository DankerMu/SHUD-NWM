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


#: What the scheduler writes on a pass that looked everywhere: backfill on
#: (``scheduler_runtime.py:1396``) and the four-key filter mapping at its empty
#: defaults (``scheduler_evidence.py:248``).  The mapping is NON-empty on such a
#: pass -- all 169 live node-22 passes measured on 2026-09-16 carry it that way --
#: which is why scope-completeness is read from the filter VALUES.
_SCOPE_COMPLETE_BACKFILL: dict[str, Any] = {"enabled": True, "lookback_hours": 48, "audit": []}
_SCOPE_COMPLETE_OPERATOR_FILTERS: dict[str, Any] = {
    "model_ids": [],
    "basin_ids": [],
    "expression": None,
    "excluded_runnable_count": 0,
}
#: The statuses written here that the scheduler only reaches AFTER candidate
#: construction, so only those passes carry the two scope keys at all.  A
#: transparent pass is written before construction and structurally has no
#: ``backfill`` key, and ``bounded_evidence_payload`` drops both keys, so
#: defaulting the keys onto every status would write shapes production can not
#: produce.  A pass written with a status outside this set and no explicit scope
#: keys is therefore ``scope_unknown``, which is loud, not silent.
_SCOPE_KEY_BEARING_STATUSES = frozenset(("blocked", "planned", "submitted", "submission_failed"))
#: ``_write_pass`` default: fill the scope keys in per the rule above.  Passing
#: ``None`` omits the key, any mapping writes it verbatim.
_DEFAULT_SCOPE = object()


def _narrowed_operator_filters(
    *,
    model_ids: tuple[str, ...] = (),
    basin_ids: tuple[str, ...] = (),
    expression: str | None = None,
) -> dict[str, Any]:
    """``scheduler_evidence.py:248`` shape carrying the operator's own narrowing."""

    return {
        "model_ids": list(model_ids),
        "basin_ids": list(basin_ids),
        "expression": expression,
        "excluded_runnable_count": 0,
    }


def _write_pass(
    root: Path,
    name: str,
    *,
    mtime: int,
    blocked: list[dict[str, Any]] | None = None,
    source_cycles: list[dict[str, Any]] | None = None,
    candidate_lists: str | None = None,
    status: str = "blocked",
    backfill: Any = _DEFAULT_SCOPE,
    operator_filters: Any = _DEFAULT_SCOPE,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": "nhms.production_scheduler.pass_evidence.v1",
        "pass_id": Path(name).name.removesuffix(".json"),
        "status": status,
        "limit": {"max_evidence_bytes": 5_000_000},
        "blocked_candidates": list(blocked or []),
        "source_cycles": list(source_cycles or []),
    }
    if candidate_lists is not None:
        # The size fallback rewrites the status, keeps the pass's own one, and
        # empties ``source_cycles`` exactly like ``bounded_evidence_payload``.
        payload["status"] = "resource_limit_blocked"
        payload["limit"].update({"candidate_lists": candidate_lists, "pre_limit_status": status})
        payload["source_cycles"] = []
    # Decided on the FINAL status: the size-fallback rewrite above lands outside
    # the bearing set, which is what ``bounded_evidence_payload`` does when it
    # drops both scope keys.
    bears_scope_keys = payload["status"] in _SCOPE_KEY_BEARING_STATUSES
    if backfill is _DEFAULT_SCOPE:
        backfill = _SCOPE_COMPLETE_BACKFILL if bears_scope_keys else None
    if operator_filters is _DEFAULT_SCOPE:
        operator_filters = _SCOPE_COMPLETE_OPERATOR_FILTERS if bears_scope_keys else None
    if backfill is not None:
        payload["backfill"] = backfill
    if operator_filters is not None:
        payload["operator_filters"] = operator_filters
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


def test_a_window_of_non_evaluating_passes_is_undecidable_even_with_an_older_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """round 1 cand-03 (a): the window never reaches back past non-evaluating passes."""

    _write_pass(tmp_path, "scheduler_2026052106_000000000000.json", mtime=1_000, blocked=[_permanent_failure_row()])
    names = [f"scheduler_2026052112_{index:012d}.json" for index in range(1, 7)]
    for index, name in enumerate(names, start=1):
        _write_pass(
            tmp_path,
            name,
            mtime=1_000 + index,
            status="lock_contended" if index % 2 else "preflight_blocked",
        )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["passes_scanned"] == 6
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {
            "pass": name,
            "status": "lock_contended" if index % 2 else "preflight_blocked",
            "reason": "status_not_evaluating",
        }
        for index, name in enumerate(names, start=1)
    ]


def test_a_window_of_unreadable_passes_is_undecidable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """round 1 cand-03 (b)."""

    for index in range(2):
        corrupt = tmp_path / f"scheduler_2026052112_{index:012d}.json"
        corrupt.write_text("{not json", encoding="utf-8")
        os.utime(corrupt, (1_000 + index, 1_000 + index))

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["unreadable_passes"] == [
        "scheduler_2026052112_000000000000.json",
        "scheduler_2026052112_000000000001.json",
    ]
    assert payload["non_evaluating_passes"] == []


def test_an_empty_evidence_root_is_undecidable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#2399: an existing but empty root (for example a drifted env) is not "nothing waits"."""

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["passes_scanned"] == 0
    assert payload["operator_actions"] == []


def test_one_clean_evaluating_pass_in_the_window_decides_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """round 1 cand-03 (c): non-evaluating neighbours do not taint an evaluating pass.

    P7 F-1: the size-fallback neighbour used to count as evaluating by the status
    it kept; it is non-evaluating now (its ``source_cycles`` were emptied).
    Round 3 r3-01: the clean ``planned`` pass decides 0 only because it is NEWER
    than the fallback -- see the next test for the other order.
    """

    _write_real_size_fallback_pass(
        tmp_path,
        "scheduler_2026052112_cccccccccccc.json",
        mtime=1_000,
        original={
            "pass_id": "scheduler_2026052112_cccccccccccc",
            "status": "submitted",
            "source_cycles": [],
            "blocked_candidates": [_unrelated_blocked_row()],
        },
    )
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="lock_contended")
    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=3_000, status="planned")

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["non_evaluating_passes"] == [
        {
            "pass": "scheduler_2026052112_bbbbbbbbbbbb.json",
            "status": "lock_contended",
            "reason": "status_not_evaluating",
        },
        {
            "pass": "scheduler_2026052112_cccccccccccc.json",
            "status": "submitted",
            "reason": "size_fallback_source_cycles_absent",
        },
    ]


def test_a_size_fallback_pass_newer_than_the_newest_decidable_pass_is_undecidable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round 3 r3-01: the breaker may engage after the decidable pass; the newer fallback hid it."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="lock_contended")
    original = {
        "pass_id": "scheduler_2026052112_cccccccccccc",
        "status": "blocked",
        "source_cycles": [_breaker_released_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    newest = tmp_path / "scheduler_2026052112_cccccccccccc.json"
    # Unbounded, the newest pass WOULD list the breaker release.
    _write_pass(tmp_path, newest.name, mtime=3_000, **_as_write_pass_kwargs(original))
    assert _run(["--evidence-root", str(tmp_path)], capsys)[0] == 1
    newest.unlink()
    _write_real_size_fallback_pass(tmp_path, newest.name, mtime=3_000, original=original)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert [(item["pass"], item["reason"]) for item in payload["non_evaluating_passes"]] == [
        ("scheduler_2026052112_bbbbbbbbbbbb.json", "status_not_evaluating"),
        ("scheduler_2026052112_cccccccccccc.json", "size_fallback_source_cycles_absent"),
    ]


_DECIDABLE = "decidable"
_SIZE_FALLBACK = "size_fallback"
_UNREADABLE_TRUNCATED = "unreadable_truncated"
_UNREADABLE_EMPTY = "unreadable_empty"
_LOCK_CONTENDED = "lock_contended"
_PREFLIGHT_BLOCKED = "preflight_blocked"
_LEASE_LOST = "lease_lost"
_RESOURCE_LIMIT_EXCEPTION = "resource_limit_blocked_exception_path"
_UNKNOWN_STATUS = "unknown_status"
_NON_STRING_STATUS = "non_string_status"
# Round 5 (design.md D3): the size fallback of a pass whose own status was
# transparent, and the four scope shapes an otherwise-evaluating pass can carry.
_SIZE_FALLBACK_FROM_PREFLIGHT = "size_fallback_from_preflight_blocked"
_SCOPE_NARROWED = "scope_narrowed"
_BACKFILL_DISABLED = "backfill_disabled"
_MISSING_BACKFILL_KEY = "missing_backfill_key"
_MISSING_FILTERS_KEY = "missing_filters_key"
# Phase-2 defence: the scope key is THERE but partial, so every key the scope
# test reads is absent and would otherwise read as an empty default.
_EMPTY_FILTERS_MAPPING = "empty_filters_mapping"
_EMPTY_BACKFILL_MAPPING = "empty_backfill_mapping"


@pytest.mark.parametrize(
    ("oldest_to_newest", "expected_code"),
    [
        # A hidden pass (size fallback or unreadable) newer than the newest decidable pass.
        ([_DECIDABLE, _SIZE_FALLBACK], 3),
        ([_DECIDABLE, _UNREADABLE_TRUNCATED], 3),
        ([_DECIDABLE, _UNREADABLE_EMPTY], 3),
        # ... still hidden when a newer non-evaluating pass follows it.
        ([_DECIDABLE, _SIZE_FALLBACK, _LOCK_CONTENDED], 3),
        ([_DECIDABLE, _UNREADABLE_TRUNCATED, _LOCK_CONTENDED], 3),
        ([_DECIDABLE, _LOCK_CONTENDED, _UNREADABLE_EMPTY], 3),
        ([_DECIDABLE, _SIZE_FALLBACK, _DECIDABLE, _UNREADABLE_EMPTY], 3),
        # The newest decidable pass is newer than every hidden pass.
        ([_SIZE_FALLBACK, _DECIDABLE], 0),
        ([_UNREADABLE_TRUNCATED, _DECIDABLE], 0),
        ([_SIZE_FALLBACK, _DECIDABLE, _LOCK_CONTENDED], 0),
        ([_UNREADABLE_EMPTY, _DECIDABLE, _LOCK_CONTENDED], 0),
        ([_DECIDABLE, _LOCK_CONTENDED], 0),
        ([_LOCK_CONTENDED, _DECIDABLE], 0),
        ([_DECIDABLE, _UNREADABLE_TRUNCATED, _DECIDABLE], 0),
        # No decidable pass at all.
        ([_SIZE_FALLBACK, _UNREADABLE_TRUNCATED], 3),
        ([_UNREADABLE_EMPTY, _SIZE_FALLBACK, _LOCK_CONTENDED], 3),
        ([_LOCK_CONTENDED], 3),
        # Round 4 ruling: only a transparent pass (lock_contended, preflight_blocked) is
        # newer than a decidable pass without making it stale; every other status arms.
        ([_DECIDABLE, _LEASE_LOST], 3),
        ([_DECIDABLE, _RESOURCE_LIMIT_EXCEPTION], 3),
        ([_DECIDABLE, _UNKNOWN_STATUS], 3),
        ([_DECIDABLE, _NON_STRING_STATUS], 3),
        ([_DECIDABLE, _LEASE_LOST, _PREFLIGHT_BLOCKED], 3),
        ([_DECIDABLE, _PREFLIGHT_BLOCKED], 0),
        ([_DECIDABLE, _LEASE_LOST, _DECIDABLE], 0),
        ([_LEASE_LOST, _DECIDABLE, _PREFLIGHT_BLOCKED], 0),
        ([_DECIDABLE, _PREFLIGHT_BLOCKED, _LOCK_CONTENDED], 0),
        # -- design.md D3, the round-5 rows ---------------------------------
        # 1 (r5-00): a size fallback is non-evaluating and ARMS whatever status it
        # kept in ``limit.pre_limit_status`` -- including a transparent one.  The
        # raw status of the artifact is ``resource_limit_blocked``, never
        # transparent; reading the kept status here instead would give 0.
        ([_DECIDABLE, _SIZE_FALLBACK_FROM_PREFLIGHT], 3),
        # 2 (r5-01): the only row that locks "narrowed LEAVES the flag".  D clears,
        # the fallback arms, the narrowed pass must leave it armed; the window
        # still has one evaluating pass, so the exit code rests on the flag alone.
        ([_DECIDABLE, _SIZE_FALLBACK, _SCOPE_NARROWED], 3),
        # 3-5 (r5-03): a narrowed pass never counts as evaluating.
        ([_UNREADABLE_TRUNCATED, _SCOPE_NARROWED], 3),
        ([_SCOPE_NARROWED, _BACKFILL_DISABLED], 3),
        # 4: ... but it does not re-arm a flag an older scope-complete pass cleared.
        ([_DECIDABLE, _SCOPE_NARROWED], 0),
        # 7 (F-02) is ``[_DECIDABLE, _LOCK_CONTENDED]`` above, which since the
        # scope keys exist means "a transparent pass carrying NO backfill key":
        # status is judged BEFORE scope, so it keeps its transparent
        # classification instead of becoming ``scope_unknown``.  One row further
        # with a narrowed pass on top, since both of them must leave the flag.
        ([_DECIDABLE, _LOCK_CONTENDED, _SCOPE_NARROWED], 0),
        # 8/9: a missing scope key arms POSITIONALLY -- it is not a global veto
        # the way a dropped candidate list is.
        ([_DECIDABLE, _MISSING_BACKFILL_KEY], 3),
        ([_DECIDABLE, _MISSING_FILTERS_KEY], 3),
        ([_MISSING_FILTERS_KEY, _DECIDABLE], 0),
        ([_MISSING_BACKFILL_KEY, _DECIDABLE], 0),
        # A PARTIAL scope mapping is unreadable scope, not unnarrowed scope: an
        # absent key must never read as its empty default, or the pass would be
        # called scope-complete and clear the flag on scope nobody established.
        ([_DECIDABLE, _EMPTY_FILTERS_MAPPING], 3),
        ([_DECIDABLE, _EMPTY_BACKFILL_MAPPING], 3),
    ],
    ids=lambda value: "-".join(value) if isinstance(value, list) else str(value),
)
def test_pass_kind_orderings_decide_by_the_hidden_pass_recency_rule(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    oldest_to_newest: list[str],
    expected_code: int,
) -> None:
    """Round 4 r4-02/r4-03: every ordering of pass kinds, not only the newest position.

    Expected codes come from the spec rule, not the module: a size-fallback or
    unreadable pass newer than the newest decidable pass makes the empty window
    undecidable (3), and so does any other pass that is neither decidable nor
    transparent (``lease_lost``, exception-path ``resource_limit_blocked``, an unknown
    or non-string status); a transparent ``lock_contended`` / ``preflight_blocked``
    pass neither arms nor clears that; no decidable pass at all is 3.  Every size-fallback pass is the
    REAL ``bounded_evidence_payload`` of a pass that, unbounded, would list a
    breaker release, and every unreadable pass is a half-written file.

    Round 5 adds the scope rows (design.md D3): a pass the operator narrowed is
    non-evaluating and LEAVES the flag, a pass whose scope keys are missing arms
    it positionally, and a size fallback arms whatever status it kept.
    """

    breaker_hiding_original = {
        "status": "blocked",
        "source_cycles": [_breaker_released_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    for index, kind in enumerate(oldest_to_newest):
        name = f"scheduler_2026052112_{index:012d}.json"
        mtime = 1_000 * (index + 1)
        if kind == _DECIDABLE:
            path = _write_pass(tmp_path, name, mtime=mtime, status="planned", blocked=[_unrelated_blocked_row()])
            written = json.loads(path.read_text(encoding="utf-8"))
            assert written["backfill"]["enabled"] is True
            assert (written["operator_filters"]["model_ids"], written["operator_filters"]["basin_ids"]) == ([], [])
            assert written["operator_filters"]["expression"] is None
        elif kind in (_LOCK_CONTENDED, _PREFLIGHT_BLOCKED, _LEASE_LOST):
            path = _write_pass(tmp_path, name, mtime=mtime, status=kind)
            if kind in (_LOCK_CONTENDED, _PREFLIGHT_BLOCKED):
                # A transparent pass is written before candidate construction, so
                # it structurally has NEITHER scope key (spec.md:54).
                written = json.loads(path.read_text(encoding="utf-8"))
                assert "backfill" not in written and "operator_filters" not in written
        elif kind == _SCOPE_NARROWED:
            _write_pass(
                tmp_path,
                name,
                mtime=mtime,
                status="planned",
                blocked=[_unrelated_blocked_row()],
                operator_filters=_narrowed_operator_filters(
                    model_ids=("model_a",), expression="model_id in [model_a]"
                ),
            )
        elif kind == _BACKFILL_DISABLED:
            _write_pass(
                tmp_path,
                name,
                mtime=mtime,
                status="planned",
                blocked=[_unrelated_blocked_row()],
                backfill={"enabled": False},
            )
        elif kind == _MISSING_BACKFILL_KEY:
            _write_pass(tmp_path, name, mtime=mtime, status="planned", backfill=None)
        elif kind == _MISSING_FILTERS_KEY:
            _write_pass(tmp_path, name, mtime=mtime, status="planned", operator_filters=None)
        elif kind == _EMPTY_FILTERS_MAPPING:
            path = _write_pass(tmp_path, name, mtime=mtime, status="planned", operator_filters={})
            written = json.loads(path.read_text(encoding="utf-8"))
            # The key IS there, and every filter the scope test reads is absent.
            assert written["operator_filters"] == {}
            assert written["backfill"]["enabled"] is True
        elif kind == _EMPTY_BACKFILL_MAPPING:
            path = _write_pass(tmp_path, name, mtime=mtime, status="planned", backfill={})
            written = json.loads(path.read_text(encoding="utf-8"))
            assert written["backfill"] == {}
            assert "expression" in written["operator_filters"]
        elif kind == _SIZE_FALLBACK_FROM_PREFLIGHT:
            original = {**breaker_hiding_original, "pass_id": name.removesuffix(".json"), "status": "preflight_blocked"}
            bounded = _write_real_size_fallback_pass(tmp_path, name, mtime=mtime, original=original)
            assert bounded["status"] == "resource_limit_blocked"
            assert bounded["limit"]["pre_limit_status"] == "preflight_blocked"
            assert bounded["limit"]["candidate_lists"] == "summarized"
            assert bounded["source_cycles"] == []
        elif kind == _RESOURCE_LIMIT_EXCEPTION:
            # ``scheduler_runtime.py`` exception path: emptied lists, no ``limit.candidate_lists``.
            path = _write_pass(tmp_path, name, mtime=mtime, status="resource_limit_blocked")
            written = json.loads(path.read_text(encoding="utf-8"))
            assert "candidate_lists" not in written["limit"]
        elif kind == _UNKNOWN_STATUS:
            _write_pass(tmp_path, name, mtime=mtime, status="status_from_a_future_scheduler")
        elif kind == _NON_STRING_STATUS:
            path = tmp_path / name
            non_string = {"pass_id": name.removesuffix(".json"), "status": ["planned"]}
            path.write_text(json.dumps(non_string), encoding="utf-8")
            os.utime(path, (mtime, mtime))
        elif kind == _SIZE_FALLBACK:
            original = {"pass_id": name.removesuffix(".json"), **breaker_hiding_original}
            bounded = _write_real_size_fallback_pass(tmp_path, name, mtime=mtime, original=original)
            assert bounded["source_cycles"] == []
        else:
            whole = json.dumps({"pass_id": name.removesuffix(".json"), **breaker_hiding_original})
            path = tmp_path / name
            path.write_text(whole[: len(whole) // 2] if kind == _UNREADABLE_TRUNCATED else "", encoding="utf-8")
            os.utime(path, (mtime, mtime))

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == expected_code
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["unreadable_passes"] == [
        f"scheduler_2026052112_{index:012d}.json"
        for index, kind in enumerate(oldest_to_newest)
        if kind in (_UNREADABLE_TRUNCATED, _UNREADABLE_EMPTY)
    ]


def test_transparent_pass_statuses_are_the_closed_hide_nothing_set() -> None:
    """Round 4 membership pin (``scheduler_runtime.py`` writers): lock_contended 716 and
    preflight_blocked 594/644/674/762/799/841/898 (empty lists) or 1328-1343 (full lists)."""

    from services.orchestrator import operator_action_listing

    assert operator_action_listing.TRANSPARENT_PASS_STATUSES == {"lock_contended", "preflight_blocked"}
    assert not operator_action_listing.TRANSPARENT_PASS_STATUSES & operator_action_listing.EVALUATING_PASS_STATUSES


def _write_real_size_fallback_pass(root: Path, name: str, *, mtime: int, original: dict[str, Any]) -> dict[str, Any]:
    """Write what the scheduler writes when a pass overflows: the REAL ``bounded_evidence_payload``."""

    bounded = scheduler_evidence_payload.bounded_evidence_payload(original, reason="evidence_bytes_exceeded")
    path = root / name
    path.write_text(json.dumps(bounded), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return bounded


def _breaker_released_source_cycle() -> dict[str, Any]:
    return {
        "source_id": "gfs",
        "cycle_id": "gfs_2026052100",
        "cycle_time_utc": "2026-05-21T00:00:00Z",
        "selection_status": "not_selected",
        "selection_reason": "journal_predecessor_identity_quarantine_breaker_engaged",
        "journal_predecessor_identity_quarantine": {
            "models": [
                {
                    "model_id": "model_a",
                    "recorded_init_state_id": "state_gfs_model_a_2026052100_gfs_2026052012_f012",
                    "occurrences": 1,
                }
            ]
        },
    }


def test_a_window_of_size_fallback_passes_is_undecidable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """P7 F-1: the size fallback drops ``source_cycles``, which hid a breaker-released cycle behind exit 0."""

    original = {
        "pass_id": "scheduler_2026052112_aaaaaaaaaaaa",
        "status": "blocked",
        "source_cycles": [_breaker_released_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    # The whole pass (unbounded) WOULD list the breaker action.
    _write_pass(tmp_path, "scheduler_2026052112_000000000000.json", mtime=1_000, **_as_write_pass_kwargs(original))
    assert _run(["--evidence-root", str(tmp_path)], capsys)[0] == 1
    (tmp_path / "scheduler_2026052112_000000000000.json").unlink()

    bounded = _write_real_size_fallback_pass(
        tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, original=original
    )
    assert bounded["source_cycles"] == []
    assert bounded["limit"]["candidate_lists"] == "summarized"

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {
            "pass": "scheduler_2026052112_aaaaaaaaaaaa.json",
            "status": "blocked",
            "reason": "size_fallback_source_cycles_absent",
        }
    ]


def test_a_size_fallback_pass_still_lists_its_summarized_blocked_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P7 F-1: non-evaluating is only about proving absence; what the summary holds is still listed."""

    original = {
        "pass_id": "scheduler_2026052112_aaaaaaaaaaaa",
        "status": "blocked",
        "source_cycles": [_breaker_released_source_cycle()],
        "blocked_candidates": [_budget_row(), _unrelated_blocked_row()],
    }
    _write_real_size_fallback_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, original=original)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert _action_projection(payload) == [
        ("model_c", "blocked_strict_warm_start_init_state_mismatch", 12, 12, None),
    ]
    assert [item["reason"] for item in payload["non_evaluating_passes"]] == ["size_fallback_source_cycles_absent"]


@pytest.mark.parametrize(
    ("narrowing", "write_kwargs"),
    [
        ("model_ids", {"operator_filters": _narrowed_operator_filters(
            model_ids=("model_a",), expression="model_id in [model_a]"
        )}),
        ("basin_ids", {"operator_filters": _narrowed_operator_filters(
            basin_ids=("basin_7",), expression="basin_id in [basin_7]"
        )}),
        ("expression", {"operator_filters": _narrowed_operator_filters(expression="model_id in [model_a]")}),
        ("backfill_disabled", {"backfill": {"enabled": False}}),
    ],
)
def test_a_window_of_only_narrowed_passes_is_undecidable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    narrowing: str,
    write_kwargs: dict[str, Any],
) -> None:
    """r5-01: a narrowed pass listed nothing *inside its own scope*, which is not an answer.

    Each shape is one of the two narrowings the pass file records (spec: backfill
    disabled, or operator filters selecting a subset), written in the producer's
    own four-key ``operator_filters`` shape (``scheduler_evidence.py:248``) and
    ``backfill`` shape (``scheduler_runtime.py:1396-1402``).
    """

    names = [f"scheduler_2026052112_{index:012d}.json" for index in range(2)]
    for index, name in enumerate(names):
        _write_pass(
            tmp_path,
            name,
            mtime=1_000 + index,
            status="planned",
            blocked=[_unrelated_blocked_row()],
            **write_kwargs,
        )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3, narrowing
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {"pass": name, "status": "planned", "reason": "scope_narrowed"} for name in names
    ]


def test_a_narrowed_pass_does_not_re_arm_a_flag_an_earlier_scope_complete_pass_cleared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """r5-01 three-state: narrowing is the operator's own instruction, so it hides nothing."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        operator_filters=_narrowed_operator_filters(model_ids=("model_a",), expression="model_id in [model_a]"),
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_bbbbbbbbbbbb.json", "status": "planned", "reason": "scope_narrowed"}
    ]


def test_a_narrowed_pass_does_not_clear_a_flag_a_size_fallback_armed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """design.md D3 row 2: the one shape where "narrowed CLEARS" would read as exit 0.

    Oldest clears, the real ``bounded_evidence_payload`` fallback arms, and the
    newest narrowed pass must leave it armed.  One evaluating pass survives in the
    window, so ``evaluating_count < 1`` does not decide this row -- the flag does.
    """

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_real_size_fallback_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        original={
            "pass_id": "scheduler_2026052112_bbbbbbbbbbbb",
            "status": "blocked",
            "source_cycles": [_breaker_released_source_cycle()],
            "blocked_candidates": [_unrelated_blocked_row()],
        },
    )
    _write_pass(
        tmp_path,
        "scheduler_2026052112_cccccccccccc.json",
        mtime=3_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        operator_filters=_narrowed_operator_filters(model_ids=("model_a",), expression="model_id in [model_a]"),
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert [(item["pass"], item["reason"]) for item in payload["non_evaluating_passes"]] == [
        ("scheduler_2026052112_bbbbbbbbbbbb.json", "size_fallback_source_cycles_absent"),
        ("scheduler_2026052112_cccccccccccc.json", "scope_narrowed"),
    ]


def test_a_narrowed_pass_that_lists_a_blocked_action_still_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Narrowing only bounds what "nothing found" means; what WAS found is still listed."""

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_budget_row(), _unrelated_blocked_row()],
        backfill={"enabled": False},
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert _action_projection(payload) == [
        ("model_c", "blocked_strict_warm_start_init_state_mismatch", 12, 12, None)
    ]
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_aaaaaaaaaaaa.json", "status": "planned", "reason": "scope_narrowed"}
    ]


def test_a_transparent_pass_without_a_backfill_key_is_not_reclassified_as_narrowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-02 / spec.md:54: the status test runs first, so no scope test ever sees this pass."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    transparent = _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="lock_contended")
    written = json.loads(transparent.read_text(encoding="utf-8"))
    assert "backfill" not in written and "operator_filters" not in written

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["non_evaluating_passes"] == [
        {
            "pass": "scheduler_2026052112_bbbbbbbbbbbb.json",
            "status": "lock_contended",
            "reason": "status_not_evaluating",
        }
    ]


def test_an_unnarrowed_pass_carrying_the_empty_filter_mapping_is_scope_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D2, measured: production writes a NON-empty four-key mapping whose values are all empty.

    A rule keyed on the mapping's presence or size would classify every live
    node-22 pass as narrowed and could never reach exit 0.
    """

    path = _write_pass(
        tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned", blocked=[]
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert len(written["operator_filters"]) == 4
    assert written["operator_filters"] == {
        "model_ids": [],
        "basin_ids": [],
        "expression": None,
        "excluded_runnable_count": 0,
    }
    assert written["backfill"]["enabled"] is True

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["non_evaluating_passes"] == []


def test_an_evaluating_pass_missing_the_backfill_key_arms_the_hidden_pass_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fourth state: scope unknowable is the same uncertainty as a size fallback, so it arms."""

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        backfill=None,
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_bbbbbbbbbbbb.json", "status": "planned", "reason": "scope_unknown"}
    ]


@pytest.mark.parametrize(
    ("shape", "write_kwargs"),
    [
        ("empty_operator_filters", {"operator_filters": {}}),
        ("operator_filters_without_expression", {"operator_filters": {"model_ids": [], "basin_ids": []}}),
        ("empty_backfill", {"backfill": {}}),
    ],
)
def test_a_partial_scope_mapping_is_unknown_scope_not_complete_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    write_kwargs: dict[str, Any],
) -> None:
    """The key is present but the filters the scope test reads are not.

    Absent keys must never read as their empty defaults: that would call the pass
    scope-complete, clear the hidden-pass flag and permit exit 0 off a pass whose
    scope nobody established -- r5-01's failure mode through a different door.
    Production can not write these shapes (``scheduler_evidence.py:248-253`` is an
    unconditional four-key literal and both legs of ``scheduler_runtime.py``
    1394-1402 carry ``enabled``), which is exactly why the rule must be keyed on
    presence rather than assumed.
    """

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        **write_kwargs,
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3, shape
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_bbbbbbbbbbbb.json", "status": "planned", "reason": "scope_unknown"}
    ]


def test_a_missing_key_pass_older_than_a_scope_complete_pass_does_not_force_exit_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """design.md D3 row 9: arming is POSITIONAL, not the global veto a dropped list is."""

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        operator_filters=None,
    )
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="planned")

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_aaaaaaaaaaaa.json", "status": "planned", "reason": "scope_unknown"}
    ]


def _as_write_pass_kwargs(original: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": original["status"],
        "source_cycles": original["source_cycles"],
        "blocked": original["blocked_candidates"],
    }


def test_a_window_of_submission_failed_passes_without_actions_decides_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """round 2 r2-03: a pass whose submissions failed still evaluated its candidates."""

    for index in range(3):
        _write_pass(
            tmp_path,
            f"scheduler_2026052112_{index:012d}.json",
            mtime=1_000 + index,
            blocked=[_unrelated_blocked_row()],
            status="submission_failed",
        )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == []


def test_evaluating_pass_statuses_are_the_closed_post_candidate_construction_set() -> None:
    """Membership pin: every status here is written only after ``_build_candidates`` ran."""

    from services.orchestrator import operator_action_listing

    assert operator_action_listing.EVALUATING_PASS_STATUSES == {
        "planned",
        "blocked",
        "unavailable",
        "submitted",
        "submitted_partial",
        "slurm_status_synced",
        "slurm_status_sync_failed",
        "slurm_cancelled",
        "slurm_partially_cancelled",
        "slurm_cancellation_blocked",
        "restart_reconciled",
        "restart_reconcile_unknown",
        "submission_failed",
        "skipped_duplicate_submission",
        "reconciling",
        "submit_result_ambiguous",
        "reconcile_unverified",
        "cancelled",
        "complete",
        "succeeded",
        "parsed_partial",
        "forcing_ready_partial",
        "forcing_ready",
        "already_done",
    }
    # Written before (or without) candidate construction, or ambiguous: never evaluating.
    for status in ("lock_contended", "preflight_blocked", "lease_lost", "resource_limit_blocked", None):
        assert status not in operator_action_listing.EVALUATING_PASS_STATUSES


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
