"""``list-operator-actions`` (#1186): enumerate manual-action decisions from db-free pass evidence.

Every test drives the shipped CLI entry ``cli.main([...])`` against REAL
``scheduler_*.json`` files on disk.  Expected values are literals taken from the
producer shapes (``scheduler_state_failure.py`` permanent/cancelled evidence,
``scheduler_candidates.py`` breaker/budget evidence, ``scheduler_discovery.py``
breaker-released ``source_cycles`` entries), never recomputed by the module under
test.
"""

from __future__ import annotations

import ast
import functools
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


def _breaker_row(
    model_id: str = "model_d",
    *,
    recorded_init_state_id: str = "state_gfs_model_d_2026052112_gfs_2026052100_f012",
    occurrences: int = 1,
) -> dict[str, Any]:
    """The token and the count are parameters because they CHANGE from pass to pass.

    ``recorded_init_state_id`` is what the operator feeds to
    ``confirm-operator-reentry``; a receipt that reports an older pass's token
    would have the operator's dry run refused.
    """

    return _candidate_row(
        model_id=model_id,
        decision="blocked_journal_predecessor_identity_quarantine",
        reason="journal_predecessor_identity_quarantine_breaker_engaged",
        retry_policy={
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "occurrences": occurrences,
            "occurrence_threshold": 1,
        },
        extra_evidence={
            "journal_predecessor_identity": {
                "recorded_init_state_id": recorded_init_state_id,
                "occurrences": occurrences,
            }
        },
    )


def _sink_refusal_row(model_id: str = "model_e") -> dict[str, Any]:
    """The fifth manual-action decision (#1555 round 4), written by ``scheduler_candidates.py``.

    Shape from the producer: a ``blocked_*`` candidate whose ``state_evidence``
    carries the decision, the sink-refusal block, and a ``retry_policy`` with
    ``manual_retry_required: True`` and NO ``attempt`` / ``retry_limit`` /
    ``occurrences`` -- the operator re-entry policy keys only.
    """

    return _candidate_row(
        model_id=model_id,
        decision="blocked_operator_reentry_restart_stage_refused",
        reason="operator_reentry_restart_stage_not_forecast",
        retry_policy={
            "automatic_retry_allowed": False,
            "manual_retry_required": True,
            "operator_reentry_command": "confirm-operator-reentry",
            "recovery_runbook": "node22-control-plane-manual-recovery",
        },
        extra_evidence={
            "classifier": "operator_reentry_authorization_scope",
            "operator_reentry_sink_refusal": {
                "refused_restart_stage": "convert",
                "refused_restart_from_stage": "convert",
            },
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
#: pass -- every live node-22 pass measured on 2026-09-16 (EF-8) carries it that
#: way, without exception -- which is why scope-completeness is read from the
#: filter VALUES.
_SCOPE_COMPLETE_BACKFILL: dict[str, Any] = {"enabled": True, "lookback_hours": 48, "audit": []}
_SCOPE_COMPLETE_OPERATOR_FILTERS: dict[str, Any] = {
    "model_ids": [],
    "basin_ids": [],
    "expression": None,
    "excluded_runnable_count": 0,
}
#: The third scope dimension: the whole production source set a pass must have
#: looked at.  Literal from the producers (``scheduler.py``'s
#: ``DEFAULT_PRODUCTION_SOURCES`` and the ``cli.py`` ``resolved_sources``
#: fallback), written into the pass by ``scheduler_evidence.py:268`` as
#: ``list(config.sources)``.
_SCOPE_COMPLETE_SOURCES: list[str] = ["gfs", "IFS"]
#: The fourth scope dimension, in the producer's own five-key shape
#: (``scheduler_evidence.py:270-276``, written unconditionally in
#: ``base_evidence``).  Only ``lookback_hours`` is read; the live node-22 value is
#: 96 on every pass without exception (measured 2026-09-16, EF-8), and the two
#: timestamps are derived from it plus ``cycle_lag_hours``
#: (``scheduler_evidence.py:244-245``).
_SCOPE_COMPLETE_CYCLE_WINDOW: dict[str, Any] = {
    "start_time_utc": "2026-05-17T08:00:00Z",
    "end_time_utc": "2026-05-21T08:00:00Z",
    "lookback_hours": 96,
    "cycle_lag_hours": 16,
    "max_cycles_per_source": 1,
}
#: #2443: the counter block, in the producer's own shape
#: (``scheduler_runtime.py:1346-1350``, the same dict literal that carries
#: ``blocked_candidates``).  ``selected_model_count`` is ``len(models)``; every
#: live node-22 pass measured 2026-09-16 carried 76, without exception.  Only
#: the model count is read, so the other counters are kept at a plain shape.
_SCOPE_COMPLETE_COUNTS: dict[str, Any] = {
    "candidate_count": 0,
    "blocked_candidate_count": 0,
    "skipped_candidate_count": 0,
    "selected_model_count": 76,
    "source_cycle_count": 1,
}
#: The sixth scope dimension's carrier, in the producer's own shape
#: (``scheduler_evidence.py:293-297``, written unconditionally in ``base_evidence``
#: and never overwritten).  Only ``allowed_cycle_hours_utc`` is read; every live
#: node-22 pass measured 2026-09-16 carried ``[0, 12]``, the code default.  The
#: other entries are kept because the reader must tolerate them, not because it
#: reads them -- each is a copy of a key judged elsewhere or a throughput knob.
_SCOPE_COMPLETE_RUNTIME_CONFIG: dict[str, Any] = {
    "allowed_cycle_hours_utc": [0, 12],
    "sources": ["gfs", "IFS"],
    "lookback_hours": 96,
    "cycle_lag_hours": 16,
    "max_cycles_per_source": 1,
    "dry_run": True,
    "interval_seconds": 900,
}
#: The statuses written here that the scheduler only reaches AFTER candidate
#: construction, so only those passes carry the six scope keys at all.  A
#: transparent pass is written before construction and structurally has no
#: ``backfill`` key, and ``bounded_evidence_payload`` drops those keys, so
#: defaulting the keys onto every status would write shapes production can not
#: produce.  A pass written with a status outside this set and no explicit scope
#: keys is therefore ``scope_unknown``, which is loud, not silent.  (Six scope
#: keys since round 2: ``counts`` and ``runtime_config`` joined the four.)
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
    sources: Any = _DEFAULT_SCOPE,
    cycle_window: Any = _DEFAULT_SCOPE,
    counts: Any = _DEFAULT_SCOPE,
    runtime_config: Any = _DEFAULT_SCOPE,
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
        # empties ``source_cycles``.  These hand-written fixtures carry NO
        # ``limit.source_cycles`` marker on purpose: that is the legacy shape,
        # read as ``dropped`` (#2402).
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
    if sources is _DEFAULT_SCOPE:
        sources = _SCOPE_COMPLETE_SOURCES if bears_scope_keys else None
    if cycle_window is _DEFAULT_SCOPE:
        cycle_window = _SCOPE_COMPLETE_CYCLE_WINDOW if bears_scope_keys else None
    if counts is _DEFAULT_SCOPE:
        counts = _SCOPE_COMPLETE_COUNTS if bears_scope_keys else None
    if runtime_config is _DEFAULT_SCOPE:
        runtime_config = _SCOPE_COMPLETE_RUNTIME_CONFIG if bears_scope_keys else None
    if backfill is not None:
        payload["backfill"] = backfill
    if operator_filters is not None:
        payload["operator_filters"] = operator_filters
    if sources is not None:
        payload["sources"] = sources
    if cycle_window is not None:
        payload["cycle_window"] = cycle_window
    if counts is not None:
        payload["counts"] = counts
    if runtime_config is not None:
        payload["runtime_config"] = runtime_config
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _action_projection(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    """``reason`` is a SHALL field of every listed action (spec.md:5), so it is projected.

    Without it, dropping ``reason=row.get("reason")`` from the module -- or
    aliasing it onto ``decision`` -- left the whole suite green.
    ``recorded_init_state_id`` is projected for the same reason (R2-01): it is the
    ONLY executable output of the breaker arm, and the suite stayed green while a
    summarized pass reported it as ``null``.
    """

    return [
        (
            item["model_id"],
            item["decision"],
            item["reason"],
            item["attempt"],
            item["retry_limit"],
            item["occurrences"],
            item["recorded_init_state_id"],
        )
        for item in payload["operator_actions"]
    ]


@pytest.mark.parametrize(
    ("row_factory", "expected"),
    [
        (_permanent_failure_row, ("model_a", "permanent_failure", "retry_limit_exhausted", 3, 3, None, None)),
        (
            _cancelled_row,
            ("model_b", "cancelled_manual_retry_required", "manual_retry_required_after_cancelled", 1, 3, None, None),
        ),
        (
            _budget_row,
            (
                "model_c",
                "blocked_strict_warm_start_init_state_mismatch",
                "strict_warm_start_retry_budget_exhausted",
                12,
                12,
                None,
                None,
            ),
        ),
        (
            _breaker_row,
            (
                "model_d",
                "blocked_journal_predecessor_identity_quarantine",
                "journal_predecessor_identity_quarantine_breaker_engaged",
                None,
                None,
                1,
                "state_gfs_model_d_2026052112_gfs_2026052100_f012",
            ),
        ),
        (
            _sink_refusal_row,
            (
                "model_e",
                "blocked_operator_reentry_restart_stage_refused",
                "operator_reentry_restart_stage_not_forecast",
                None,
                None,
                None,
                None,
            ),
        ),
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
        (
            "model_c",
            "blocked_strict_warm_start_init_state_mismatch",
            "strict_warm_start_retry_budget_exhausted",
            12,
            12,
            None,
            None,
        ),
        (
            "model_d",
            "blocked_journal_predecessor_identity_quarantine",
            "journal_predecessor_identity_quarantine_breaker_engaged",
            None,
            None,
            1,
            # R2-01: the summary keeps the token at the row level, so the listing
            # reports it instead of the ``null`` it used to.
            "state_gfs_model_d_2026052112_gfs_2026052100_f012",
        ),
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


def test_a_candidate_seen_in_several_passes_reports_the_newest_passs_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round 1 C1: every value field comes from ``last_seen_pass``, not from the oldest pass.

    Keeping the first-seen values while naming the newest pass produced a
    self-contradictory receipt, and the runbook feeds ``recorded_init_state_id``
    from it into ``confirm-operator-reentry`` -- which refuses a stale token
    before it even reaches its dry-run branch.
    """

    names = [f"scheduler_2026052112_{index:012d}.json" for index in range(3)]
    tokens = [
        "state_gfs_model_d_2026052112_gfs_2026052100_f012",
        "state_gfs_model_d_2026052112_gfs_2026052106_f006",
        "state_gfs_model_d_2026052112_gfs_2026052112_f000",
    ]
    for index, name in enumerate(names):
        _write_pass(
            tmp_path,
            name,
            mtime=1_000 * (index + 1),
            blocked=[_breaker_row(recorded_init_state_id=tokens[index], occurrences=index + 1)],
        )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    (action,) = payload["operator_actions"]
    assert action["recorded_init_state_id"] == tokens[2]
    assert action["occurrences"] == 3
    assert action["first_seen_pass"] == names[0]
    assert action["last_seen_pass"] == names[2]
    assert action["seen_in_passes"] == 3


def test_the_newest_passs_token_survives_that_pass_being_bounded_summarized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-01: newest-wins must not silently mean "newest, unless it overflowed".

    The whole window is scanned, the newest pass is a REAL
    ``_bounded_candidate_summary`` product, and the merged entry must report THAT
    pass's token.  Before the fix the summary projected no token at all and the
    reader only looked in ``state_evidence``, so the receipt named the newest pass
    and reported ``recorded_init_state_id: null`` -- leaving the operator without
    the one value ``confirm-operator-reentry --recorded-init-state-id`` requires,
    with no other surface in the repo printing it and the refusal payload not
    echoing it either.
    """

    stale = "state_gfs_model_d_2026052112_gfs_2026052100_f012"
    live = "state_gfs_model_d_2026052112_gfs_2026052112_f000"
    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        blocked=[_breaker_row(recorded_init_state_id=stale, occurrences=1)],
    )
    summarized = scheduler_evidence_payload._bounded_candidate_summary(
        _breaker_row(recorded_init_state_id=live, occurrences=2)
    )
    assert "state_evidence" not in summarized
    assert summarized["recorded_init_state_id"] == live
    # The summary name must not collide with a row-level key, or the row-level
    # second read in ``_pass_actions`` would read a different producer's value.
    assert "recorded_init_state_id" not in scheduler_evidence_payload._BOUNDED_CANDIDATE_SUMMARY_KEYS
    # Idempotent: a second summary pass (a re-summarized row) keeps it too.
    assert scheduler_evidence_payload._bounded_candidate_summary(summarized) == summarized
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        blocked=[summarized],
        candidate_lists="summarized",
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    (action,) = payload["operator_actions"]
    assert action["recorded_init_state_id"] == live
    assert action["occurrences"] == 2
    assert (action["first_seen_pass"], action["last_seen_pass"], action["seen_in_passes"]) == (
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        2,
    )


def test_a_candidate_id_from_an_older_pass_survives_a_newer_pass_that_has_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Newest-wins has one exception: the breaker-released leg carries no ``candidate_id``."""

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        blocked=[_breaker_row(recorded_init_state_id="state_gfs_model_d_2026052112_gfs_2026052100_f012")],
    )
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        source_cycles=[
            {
                "source_id": "gfs",
                "cycle_time_utc": "2026-05-21T12:00:00Z",
                "selection_status": "not_selected",
                "selection_reason": "journal_predecessor_identity_quarantine_breaker_engaged",
                "journal_predecessor_identity_quarantine": {
                    "models": [
                        {
                            "model_id": "model_d",
                            "recorded_init_state_id": "state_gfs_model_d_2026052112_gfs_2026052112_f000",
                            "occurrences": 4,
                        }
                    ]
                },
            }
        ],
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    (action,) = payload["operator_actions"]
    assert action["candidate_id"] == "gfs:2026-05-21T12:00:00Z:model_d:forecast_gfs_deterministic"
    assert action["recorded_init_state_id"] == "state_gfs_model_d_2026052112_gfs_2026052112_f000"
    assert action["occurrences"] == 4
    assert (action["first_seen_pass"], action["last_seen_pass"], action["seen_in_passes"]) == (
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        2,
    )


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


class _OsProxy:
    """Stands in for the listing module's own ``os``: one overridden attribute, the rest real.

    Patching the global ``os.scandir`` would reach pytest and click too; the module
    only ever reaches the filesystem through its module-level ``os`` name.
    """

    def __init__(self, scandir: Any) -> None:
        self.scandir = scandir

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _scandir_with_one_vanishing_entry(vanishing: str, failing_method: str) -> Any:
    """Real ``scandir``, except one entry raises ``FileNotFoundError`` like a deleted file.

    The retention timer (``nhms-scheduler-evidence-retention.timer``) deletes under
    this very root on its own schedule, so ``scandir`` handing back a name whose
    ``is_file``/``stat`` then fails is a real race, not a hypothetical one.
    """

    class _VanishingEntry:
        def __init__(self, entry: Any) -> None:
            self.name = entry.name
            self.path = entry.path
            self._entry = entry

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            if failing_method == "is_file":
                raise FileNotFoundError(2, "No such file or directory", self.path)
            return bool(self._entry.is_file(follow_symlinks=follow_symlinks))

        def stat(self, *, follow_symlinks: bool = True) -> Any:
            if failing_method == "stat":
                raise FileNotFoundError(2, "No such file or directory", self.path)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class _Scandir:
        def __init__(self, path: Any) -> None:
            self._iterator = os.scandir(path)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc_info: Any) -> bool:
            self._iterator.close()
            return False

        def __iter__(self) -> Any:
            for entry in self._iterator:
                yield _VanishingEntry(entry) if entry.name == vanishing else entry

    return _Scandir


@pytest.mark.parametrize("failing_method", ["stat", "is_file"])
def test_a_pass_file_deleted_mid_scan_is_reported_without_aborting_the_scan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failing_method: str,
) -> None:
    """Round 1 B1: one deleted file is not "evidence root unreadable".

    Before the fix the per-entry ``is_file``/``stat`` sat under the root-level
    ``try``, so a single concurrent deletion turned the whole scan into exit 2
    naming the ROOT -- which the runbook reads as "you took the wrong root" -- and
    threw every readable pass away with it.
    """

    from services.orchestrator import operator_action_listing

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
    )
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="planned")
    vanishing = "scheduler_2026052112_cccccccccccc.json"
    _write_pass(tmp_path, vanishing, mtime=3_000, status="planned")
    monkeypatch.setattr(
        operator_action_listing,
        "os",
        _OsProxy(_scandir_with_one_vanishing_entry(vanishing, failing_method)),
    )

    code, payload, err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert err == ""
    assert payload is not None
    assert payload["passes_scanned"] == 2
    assert payload["unreadable_passes"] == [vanishing]
    assert payload["non_evaluating_passes"] == []
    assert payload["operator_actions"] == []
    # A global veto, not a positional one: its mtime was never read, so it can not
    # be placed in the time order at all -- two scope-complete evaluating passes do
    # NOT make this window answer "nothing waits".
    assert code == 3


def test_an_unreadable_root_still_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control leg of the test above: the ROOT failing is still the exit-2 case."""

    from services.orchestrator import operator_action_listing

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")

    def _refuse(path: Any) -> Any:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(operator_action_listing, "os", _OsProxy(_refuse))

    code, payload, err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 2
    assert payload is None
    assert "evidence root unreadable" in err


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
    # The breaker-released leg has no candidate row to take a reason from: the
    # module pins the not-selected entry's own ``selection_reason`` literal.
    assert [item["reason"] for item in payload["operator_actions"]] == [
        "journal_predecessor_identity_quarantine_breaker_engaged",
        "journal_predecessor_identity_quarantine_breaker_engaged",
    ]


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
    it kept; it is non-evaluating now (it can show at most its breaker-released
    ``source_cycles`` projection, never that nothing else was released).
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
            "reason": "size_fallback_source_cycles_summarized",
        },
    ]


def test_a_size_fallback_pass_newer_than_the_newest_decidable_pass_is_undecidable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round 3 r3-01: the breaker may engage after the decidable pass; the newer fallback hid it.

    #2402 retarget: what the fallback still loses is every source cycle outside
    the breaker-released projection, so the hidden cycle here is a deferred one.
    """

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="lock_contended")
    original = {
        "pass_id": "scheduler_2026052112_cccccccccccc",
        "status": "blocked",
        "source_cycles": [_dropped_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    newest = tmp_path / "scheduler_2026052112_cccccccccccc.json"
    bounded = _write_real_size_fallback_pass(tmp_path, newest.name, mtime=3_000, original=original)
    # The source cycle the unbounded pass carried is gone from the artifact.
    assert bounded["source_cycles"] == []

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert [(item["pass"], item["reason"]) for item in payload["non_evaluating_passes"]] == [
        ("scheduler_2026052112_bbbbbbbbbbbb.json", "status_not_evaluating"),
        ("scheduler_2026052112_cccccccccccc.json", "size_fallback_source_cycles_summarized"),
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
_SOURCE_NARROWED = "source_narrowed"
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
        # 2b: ``--source`` is the THIRD narrowing dimension and must behave exactly
        # like row 2 -- a pass that only looked at gfs never looked at IFS.  Same
        # discriminating shape: D clears, the fallback arms, the source-narrowed
        # pass must leave it armed.
        ([_DECIDABLE, _SIZE_FALLBACK, _SOURCE_NARROWED], 3),
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
    REAL ``bounded_evidence_payload`` of a pass whose source cycle the fallback
    drops (#2402: a deferred entry, outside the breaker-released projection the
    fallback keeps), and every unreadable pass is a half-written file.

    Round 5 adds the scope rows (design.md D3): a pass the operator narrowed is
    non-evaluating and LEAVES the flag, a pass whose scope keys are missing arms
    it positionally, and a size fallback arms whatever status it kept.
    """

    source_cycle_hiding_original = {
        "status": "blocked",
        "source_cycles": [_dropped_source_cycle()],
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
            assert written["sources"] == ["gfs", "IFS"]
        elif kind in (_LOCK_CONTENDED, _PREFLIGHT_BLOCKED, _LEASE_LOST):
            path = _write_pass(tmp_path, name, mtime=mtime, status=kind)
            if kind in (_LOCK_CONTENDED, _PREFLIGHT_BLOCKED):
                # A transparent pass is written before candidate construction, so
                # it structurally has no ``backfill`` key (spec.md:54).  It does
                # carry ``operator_filters`` in production (``_base_evidence``
                # writes it unconditionally); this fixture simply omits it, which
                # the status-first ordering makes unobservable -- so nothing here
                # may assert its ABSENCE as if that were the producer's shape.
                written = json.loads(path.read_text(encoding="utf-8"))
                assert "backfill" not in written
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
        elif kind == _SOURCE_NARROWED:
            path = _write_pass(
                tmp_path,
                name,
                mtime=mtime,
                status="planned",
                blocked=[_unrelated_blocked_row()],
                sources=["gfs"],
            )
            written = json.loads(path.read_text(encoding="utf-8"))
            # Narrowed by VALUE, not by absence: every other scope key is complete.
            assert written["sources"] == ["gfs"]
            assert written["backfill"]["enabled"] is True
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
            original = {
                **source_cycle_hiding_original,
                "pass_id": name.removesuffix(".json"),
                "status": "preflight_blocked",
            }
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
            original = {"pass_id": name.removesuffix(".json"), **source_cycle_hiding_original}
            bounded = _write_real_size_fallback_pass(tmp_path, name, mtime=mtime, original=original)
            assert bounded["source_cycles"] == []
        else:
            whole = json.dumps({"pass_id": name.removesuffix(".json"), **source_cycle_hiding_original})
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


def _dropped_source_cycle() -> dict[str, Any]:
    """A not-selected cycle the bounded projection does NOT keep (#2402).

    The fallback keeps only the breaker-released leg, so a deferred entry like
    this one is exactly what a size-fallback pass still loses -- which is why
    such a pass stays non-evaluating even when ``limit.source_cycles`` is
    ``summarized``: it can show the releases it kept, never that nothing else
    was released.  Producer literal: ``scheduler_discovery.py``
    ``_backfill_deferred_evidence``.
    """

    return {
        "source_id": "gfs",
        "cycle_id": "gfs_2026052100",
        "cycle_time_utc": "2026-05-21T00:00:00Z",
        "selection_status": "not_selected",
        "selection_reason": "backfill_deferred_waiting_for_prior_cycle",
    }


def test_a_window_of_size_fallback_passes_is_undecidable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """P7 F-1: the size fallback drops every source cycle it does not project, so it can not answer 0.

    Retargeted by #2402: the fallback now KEEPS the breaker-released entries, so
    the window that stays undecidable is the one whose lost cycles are the other
    ones -- here a deferred entry.  The breaker-released leg has its own test
    below (it is listed and exits 1).
    """

    original = {
        "pass_id": "scheduler_2026052112_aaaaaaaaaaaa",
        "status": "blocked",
        "source_cycles": [_dropped_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    bounded = _write_real_size_fallback_pass(
        tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, original=original
    )
    assert bounded["source_cycles"] == []
    assert bounded["limit"]["candidate_lists"] == "summarized"
    assert bounded["limit"]["source_cycles"] == {
        "status": "summarized",
        "breaker_released_total": 0,
        "retained": 0,
    }

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {
            "pass": "scheduler_2026052112_aaaaaaaaaaaa.json",
            "status": "blocked",
            "reason": "size_fallback_source_cycles_summarized",
        }
    ]


def test_a_size_fallback_pass_lists_the_breaker_release_its_projection_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2402: the projection is the whole point -- the release survives the fallback and is listed."""

    original = {
        "pass_id": "scheduler_2026052112_aaaaaaaaaaaa",
        "status": "blocked",
        "source_cycles": [_breaker_released_source_cycle(), _dropped_source_cycle()],
        "blocked_candidates": [_unrelated_blocked_row()],
    }
    # The whole pass (unbounded) lists the breaker action.
    _write_pass(tmp_path, "scheduler_2026052112_000000000000.json", mtime=1_000, **_as_write_pass_kwargs(original))
    full_code, full_payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)
    assert full_code == 1
    assert full_payload is not None
    (tmp_path / "scheduler_2026052112_000000000000.json").unlink()

    bounded = _write_real_size_fallback_pass(
        tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, original=original
    )
    assert bounded["limit"]["source_cycles"] == {
        "status": "summarized",
        "breaker_released_total": 1,
        "retained": 1,
    }
    # Only the breaker-released leg is kept; the deferred entry is gone.
    assert [item["selection_reason"] for item in bounded["source_cycles"]] == [
        "journal_predecessor_identity_quarantine_breaker_engaged"
    ]

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert _action_projection(payload) == _action_projection(full_payload)
    assert [item["reason"] for item in payload["non_evaluating_passes"]] == [
        "size_fallback_source_cycles_summarized"
    ]


def test_a_size_fallback_pass_still_lists_its_summarized_blocked_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P7 F-1: non-evaluating is only about proving absence; what the summary holds is still listed."""

    original = {
        "pass_id": "scheduler_2026052112_aaaaaaaaaaaa",
        "status": "blocked",
        "source_cycles": [_dropped_source_cycle()],
        "blocked_candidates": [_budget_row(), _unrelated_blocked_row()],
    }
    _write_real_size_fallback_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, original=original)

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 1
    assert payload is not None
    assert _action_projection(payload) == [
        (
            "model_c",
            "blocked_strict_warm_start_init_state_mismatch",
            "strict_warm_start_retry_budget_exhausted",
            12,
            12,
            None,
            None,
        ),
    ]
    assert [item["reason"] for item in payload["non_evaluating_passes"]] == [
        "size_fallback_source_cycles_summarized"
    ]


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
        # ``plan-production --source gfs``: the pass never looked at IFS.
        ("sources", {"sources": ["gfs"]}),
        # R2-02, the fourth dimension: ``--lookback-hours 0`` is a zero-width
        # window, blind to every older cycle -- and the breaker release sits by
        # construction on the oldest side (``scheduler_discovery.py:824-832``).
        # ``0`` is the exact reachable value: ``cli.py:431-435`` has no lower
        # bound and ``scheduler_config/config.py:458`` clamps negatives to it.
        ("lookback_hours_zero", {"cycle_window": {**_SCOPE_COMPLETE_CYCLE_WINDOW, "lookback_hours": 0}}),
        # The fifth dimension: ``discover_cycles`` drops every cycle whose hour is
        # outside this set, so a pass allowed only 00Z never evaluated the 12Z slot.
        (
            "allowed_cycle_hours_subset",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": [0]}},
        ),
        # The degenerate end of the same dimension: no cycle hour is allowed at all.
        (
            "allowed_cycle_hours_empty",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": []}},
        ),
    ],
)
def test_a_window_of_only_narrowed_passes_is_undecidable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    narrowing: str,
    write_kwargs: dict[str, Any],
) -> None:
    """r5-01: a narrowed pass listed nothing *inside its own scope*, which is not an answer.

    Each shape is one of the five narrowings the pass file records (spec:
    backfill disabled, operator filters selecting a subset, ``sources`` naming
    less than the production set, a zero-width cycle window, or an
    ``allowed_cycle_hours_utc`` narrower than the code default), written in the
    producer's own four-key ``operator_filters`` shape
    (``scheduler_evidence.py:248``), ``backfill`` shape
    (``scheduler_runtime.py:1396-1402``), ``sources`` list
    (``scheduler_evidence.py:268``), five-key ``cycle_window``
    (``scheduler_evidence.py:270-276``) and ``runtime_config`` block
    (``scheduler_evidence.py:293-297``).
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


@pytest.mark.parametrize(
    ("shape", "sources"),
    [
        ("superset", ["gfs", "IFS", "ERA5"]),
        ("reordered", ["IFS", "gfs"]),
    ],
)
def test_a_pass_covering_the_whole_production_source_set_is_scope_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str, sources: list[str]
) -> None:
    """R2-03: the test is COVERAGE of the production set, not equality with it.

    ``--source gfs --source IFS --source ERA5`` did look at gfs and IFS, so it can
    answer for them; only a SUBSET answers for less than everywhere.  Equality
    called such a pass narrowed and forced exit 3 -- conservative, but it made
    ``scope_narrowed`` mean two different things.  Not case-folded on purpose:
    ``scheduler_config/config.py:448`` normalizes spellings through
    ``normalize_source_id`` and raises on an unknown one, so a case variant can not
    reach the pass file at all, while the adapter's manifest path is case-sensitive.
    """

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        sources=sources,
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0, shape
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == []


@pytest.mark.parametrize(
    ("shape", "allowed_hours"),
    [
        ("superset", [0, 6, 12, 18]),
        ("reordered", [12, 0]),
    ],
)
def test_a_pass_allowing_more_cycle_hours_than_the_default_is_scope_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str, allowed_hours: list[int]
) -> None:
    """The cycle-hour dimension is coverage of the DEFAULT, exactly like ``sources``.

    A pass that let 06Z and 18Z through as well still evaluated both default
    slots, so it can answer for them.  And the completeness authority is the code
    default rather than the whole 0-23 day on purpose: production runs the default,
    so judging against the full day would report every production pass narrowed and
    manufacture a false exit 3 -- the same invented threshold this change already
    refused for ``cycle_lag_hours``.
    """

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        runtime_config={**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": allowed_hours},
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0, shape
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == []


def test_the_cycle_hour_authority_is_the_scheduler_default_not_a_local_copy() -> None:
    """The sixth closed set of this surface is ALIASED from its authority, not copied.

    Round 2's recurring invariant: every closed set on this surface must be
    reverse-derived from the module that owns it.  Both production closed sets are
    therefore bound to ``scheduler.py``'s own names -- identity, not equality, so a
    stale copy is not even expressible.
    """

    from services.orchestrator import operator_action_listing, scheduler

    assert operator_action_listing.SCOPE_COMPLETE_CYCLE_HOURS_UTC is scheduler.DEFAULT_ALLOWED_CYCLE_HOURS_UTC
    assert operator_action_listing.SCOPE_COMPLETE_SOURCES is scheduler.DEFAULT_PRODUCTION_SOURCES


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


def test_a_pass_that_selected_no_models_arms_the_flag_instead_of_answering_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2443: a pass that evaluated NOTHING must not be the pass that says "nothing waits".

    Reachable with no operator instruction at all: the db-free registry manifest
    accepts ``models: []`` (``scheduler_file_providers.py:883-940`` has no lower
    bound) and a manifest whose rows are all excluded by
    ``scheduler_models.py:137-145`` lands in the same place with the registry still
    ``ready`` -- and the evidence still records ``backfill.enabled: true``, because
    the writer records the CONFIGURED leg while ``scheduler_discovery.py:700`` also
    requires a non-empty model set to take it.  So every other scope key reads
    complete and only the model count tells the truth.

    The older scope-complete pass is here on purpose: it CLEARS the flag first, so
    the exit 3 is produced by this pass arming it, not by ``evaluating_count < 1``.
    """

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        counts={**_SCOPE_COMPLETE_COUNTS, "selected_model_count": 0},
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_bbbbbbbbbbbb.json", "status": "planned", "reason": "no_models_evaluated"}
    ]


def test_a_zero_model_pass_that_is_also_narrowed_reports_the_arming_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2443 ordering: "evaluated nothing" outranks "the operator narrowed it".

    ``scope_narrowed`` LEAVES the flag because such a pass still answered for its
    own scope.  A zero-model pass answered for nothing, not even for its own scope,
    so the two must not be collapsed -- reporting ``scope_narrowed`` here would let
    an earlier pass's exit 0 stand.
    """

    _write_pass(tmp_path, "scheduler_2026052112_aaaaaaaaaaaa.json", mtime=1_000, status="planned")
    _write_pass(
        tmp_path,
        "scheduler_2026052112_bbbbbbbbbbbb.json",
        mtime=2_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        backfill={"enabled": False},
        counts={**_SCOPE_COMPLETE_COUNTS, "selected_model_count": 0},
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3
    assert payload is not None
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_bbbbbbbbbbbb.json", "status": "planned", "reason": "no_models_evaluated"}
    ]


def test_a_newer_scope_complete_pass_clears_the_flag_a_zero_model_pass_armed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2443 is POSITIONAL, like every other arming reason on this surface.

    A zero-model pass arms the flag because nobody knows what it would have found;
    a LATER pass that did evaluate the whole scope knows, and its answer supersedes.
    Without this the first empty registry manifest would wedge the surface at exit 3
    for the rest of the retention window.
    """

    _write_pass(
        tmp_path,
        "scheduler_2026052112_aaaaaaaaaaaa.json",
        mtime=1_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        counts={**_SCOPE_COMPLETE_COUNTS, "selected_model_count": 0},
    )
    _write_pass(tmp_path, "scheduler_2026052112_bbbbbbbbbbbb.json", mtime=2_000, status="planned")

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 0
    assert payload is not None
    assert payload["operator_actions"] == []
    assert payload["non_evaluating_passes"] == [
        {"pass": "scheduler_2026052112_aaaaaaaaaaaa.json", "status": "planned", "reason": "no_models_evaluated"}
    ]


@pytest.mark.parametrize(
    ("narrowing", "write_kwargs"),
    [
        (
            "operator_filters",
            {"operator_filters": _narrowed_operator_filters(
                model_ids=("model_a",), expression="model_id in [model_a]"
            )},
        ),
        # The round-2 dimension gets its own row: "leaves the flag as it found it"
        # is asserted for the newest narrowing dimension, not only the oldest one.
        (
            "allowed_cycle_hours",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": [0]}},
        ),
    ],
)
def test_a_narrowed_pass_does_not_clear_a_flag_a_size_fallback_armed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    narrowing: str,
    write_kwargs: dict[str, Any],
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
            "source_cycles": [_dropped_source_cycle()],
            "blocked_candidates": [_unrelated_blocked_row()],
        },
    )
    _write_pass(
        tmp_path,
        "scheduler_2026052112_cccccccccccc.json",
        mtime=3_000,
        status="planned",
        blocked=[_unrelated_blocked_row()],
        **write_kwargs,
    )

    code, payload, _err = _run(["--evidence-root", str(tmp_path)], capsys)

    assert code == 3, narrowing
    assert payload is not None
    assert payload["operator_actions"] == []
    assert [(item["pass"], item["reason"]) for item in payload["non_evaluating_passes"]] == [
        ("scheduler_2026052112_bbbbbbbbbbbb.json", "size_fallback_source_cycles_summarized"),
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
        (
            "model_c",
            "blocked_strict_warm_start_init_state_mismatch",
            "strict_warm_start_retry_budget_exhausted",
            12,
            12,
            None,
            None,
        )
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
    # Only ``backfill`` is a key the producer structurally can not write here
    # (``scheduler_runtime.py`` writes it on the main path only); ``operator_filters``
    # IS written on a real transparent pass, so its absence is a fixture detail and
    # must not be asserted as producer shape.
    assert "backfill" not in written

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
        # The sixth field the scope test reads.  A missing ``sources`` key must not
        # read as "no source filter"; a non-list one is a shape the writer can not
        # produce (``scheduler_evidence.py:268`` writes ``list(config.sources)``).
        ("missing_sources", {"sources": None}),
        ("string_sources", {"sources": "gfs"}),
        ("non_string_source_element", {"sources": [["gfs"]]}),
        # The seventh: ``cycle_window.lookback_hours``.  R2-02 through a different
        # door -- a missing window read as "unnarrowed" is the same "absent means
        # complete" defect the reason split exists to prevent.  ``True`` is
        # explicitly not an integer here (``bool`` is an ``int`` subclass, and the
        # writer only ever stores ``max(int(...), 0)``).
        ("missing_cycle_window", {"cycle_window": None}),
        ("empty_cycle_window", {"cycle_window": {}}),
        (
            "cycle_window_without_lookback_hours",
            {"cycle_window": {key: value for key, value in _SCOPE_COMPLETE_CYCLE_WINDOW.items()
                              if key != "lookback_hours"}},
        ),
        ("string_lookback_hours", {"cycle_window": {**_SCOPE_COMPLETE_CYCLE_WINDOW, "lookback_hours": "96"}}),
        ("bool_lookback_hours", {"cycle_window": {**_SCOPE_COMPLETE_CYCLE_WINDOW, "lookback_hours": True}}),
        # #2443, the eighth: ``counts.selected_model_count``.  A missing counter
        # block must not read as "some models were evaluated".
        ("missing_counts", {"counts": None}),
        ("empty_counts", {"counts": {}}),
        ("string_selected_model_count", {"counts": {**_SCOPE_COMPLETE_COUNTS, "selected_model_count": "76"}}),
        # The ninth: ``runtime_config.allowed_cycle_hours_utc``.  A missing block
        # must not read as "the default hours were allowed"; the writer stores a
        # list of plain ints, so a string or a bool element is a shape it can not
        # produce -- and ``set()`` over an unhashable element would escape the
        # 0/1/2/3 contract as a TypeError instead of an exit code.
        ("missing_runtime_config", {"runtime_config": None}),
        ("empty_runtime_config", {"runtime_config": {}}),
        (
            "runtime_config_without_allowed_cycle_hours",
            {"runtime_config": {key: value for key, value in _SCOPE_COMPLETE_RUNTIME_CONFIG.items()
                                if key != "allowed_cycle_hours_utc"}},
        ),
        (
            "string_allowed_cycle_hours",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": "0,12"}},
        ),
        (
            "bool_allowed_cycle_hour_element",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": [0, True]}},
        ),
        (
            "unhashable_allowed_cycle_hour_element",
            {"runtime_config": {**_SCOPE_COMPLETE_RUNTIME_CONFIG, "allowed_cycle_hours_utc": [[0], [12]]}},
        ),
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
    unconditional four-key literal, ``:270-276`` an unconditional five-key
    ``cycle_window`` literal, and both legs of ``scheduler_runtime.py`` 1394-1402
    carry ``enabled``), which is exactly why the rule must be keyed on presence
    rather than assumed.
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


# #2442: the 24-literal self-copy that used to stand here
# (``test_evaluating_pass_statuses_are_the_closed_post_candidate_construction_set``)
# is GONE.  It asserted ``EVALUATING_PASS_STATUSES == {the same 24 literals}``,
# which froze the module constant against a careless edit and said nothing about
# the writers: changing a writer reddened no test.  Its replacement is
# ``tests/test_operator_action_status_closure.py``, which reads the writer
# sources with ``ast`` and binds each literal either to a write site or to the
# declared execution-evidence passthrough -- including the negative half this one
# carried (``lock_contended`` / ``preflight_blocked`` / ``lease_lost`` /
# ``resource_limit_blocked`` are never evaluating), which is now the partition
# assertion ``statuses - evaluating == transparent | {lease_lost,
# resource_limit_blocked}``.  Deliberately not two unrelated copies of the same
# 24 lines.


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


#: The orchestrator package on disk.  The pins below READ these files with ``ast``
#: and never import them: the listing surface is db-free on purpose, and importing
#: the writers here would add importer pairs to the CI selector's directory rules.
_ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1] / "services" / "orchestrator"
#: EVERY module of the orchestrator package, derived rather than listed (R2-05).
#: The previous two-name literal made the closure pin below unclosed one level up:
#: a ``manual_retry_required: True`` decision written by any THIRD module was
#: invisible to it, which is the same silent-exit-0 failure the pin exists to
#: catch.  ``rglob``, not ``glob`` -- ``scheduler_config/`` is a sub-package and
#: its modules would otherwise sit outside the scan.  ``Path`` elements, not
#: basenames, for the same reason: a sub-package module does not resolve under
#: ``_ORCHESTRATOR_DIR / name``.  Measured: 106 files, both legs together ~0.6s.
_MANUAL_ACTION_WRITER_FILES: tuple[Path, ...] = tuple(sorted(_ORCHESTRATOR_DIR.rglob("*.py")))


@functools.cache
def _parsed_orchestrator_module(path: Path) -> ast.Module:
    """One parse per module; both ``literal_true`` legs walk the same tree."""

    return ast.parse(path.read_text(encoding="utf-8"))


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments, so a decision named by constant resolves."""

    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _module_dict_constants(tree: ast.Module) -> dict[str, ast.Dict]:
    """Module-level ``NAME = {...}`` assignments, so a ``**NAME`` spread resolves."""

    constants: dict[str, ast.Dict] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
    return constants


def _is_module_constant_spelling(node: ast.expr) -> bool:
    """A ``**NAME`` spread whose NAME is spelled like a module constant.

    The line between "must resolve" and "known limit".  Measured over the whole
    package: 3 spreads name a module-level dict literal (the
    ``**_OPERATOR_REENTRY_POLICY`` trio), 123 name a lowercase local such as
    ``**base_evidence``, 81 are ``**dict(...)``/other calls, 16 are comprehensions,
    attributes or conditionals, and ZERO are constant-spelled but unresolvable.
    Reporting the locals and the calls would keep this pin permanently red over
    the very shapes documented as known limits in
    :func:`_written_manual_action_decisions`; reporting a constant-spelled name
    that stopped resolving (moved to another module, renamed, rebuilt by a call)
    catches the one refactor that could silently drop a listed decision.
    """

    return isinstance(node, ast.Name) and node.id.lstrip("_").isupper()


def _dict_decision_value(
    node: ast.Dict,
    *,
    dict_constants: dict[str, ast.Dict],
    _seen: frozenset[str] = frozenset(),
) -> ast.expr | None:
    """The ``decision`` value of a dict literal, read through ``**MODULE_CONSTANT`` too.

    Symmetric with :func:`_marks_manual_retry_required`, and that symmetry is the
    point (round-3 C2).  The flag side has resolved spreads since round 2, while
    this side matched ``ast.Constant`` keys ONLY -- a ``**spread`` entry carries a
    ``None`` key and was skipped -- so a writer that named its decision in a
    module constant and spread it in was invisible: marked as needing an operator,
    with no decision to add, dropped by the caller's ``continue``.  That is the
    round-1 A1 failure (a decision missing from the listed set answers ``exit 0``
    for a candidate the runbook still handles) reappearing inside the guard built
    to close it, and the shape is one DRY refactor away -- 30-odd dicts in this
    package already carry a literal ``decision`` beside an outer ``**spread``.

    Last write wins, as in Python itself: a literal ``decision`` after a spread
    overrides the spread's, and a spread after a literal overrides the literal.
    Unresolvable constant-spelled spreads are NOT reported here -- the caller
    already has them from the flag side, which walks exactly the same entries of
    exactly the same node, and reporting them twice would double every line.
    """

    value: ast.expr | None = None
    for literal_key, item in zip(node.keys, node.values, strict=True):
        if literal_key is None:
            if isinstance(item, ast.Name) and item.id in dict_constants and item.id not in _seen:
                nested = _dict_decision_value(
                    dict_constants[item.id],
                    dict_constants=dict_constants,
                    _seen=_seen | {item.id},
                )
                if nested is not None:
                    value = nested
            continue
        if isinstance(literal_key, ast.Constant) and literal_key.value == "decision":
            value = item
    return value


def _marks_manual_retry_required(
    node: ast.Dict,
    *,
    literal_true: bool,
    dict_constants: dict[str, ast.Dict],
    _seen: frozenset[str] = frozenset(),
) -> tuple[bool, list[int]]:
    """``(marks the flag, line numbers of spreads that would not resolve)``.

    Nested matters: the flag lives in the ``retry_policy`` sub-dict of the
    decision dict on four of the five listed writers.  ``literal_true=True``
    matches a literal ``True`` only -- an expression cannot be judged statically;
    ``literal_true=False`` matches the expression spellings instead, so the pin can
    close over those too rather than being silently blind to them.

    A ``**MODULE_CONSTANT`` spread is resolved against the module's dict
    constants, and one spelled like a module constant that does NOT resolve is
    REPORTED rather than read as "no flag here".  That shape is the one with real
    risk: ``scheduler_candidates.py:1677,2739,2857`` already spread
    ``**_OPERATOR_REENTRY_POLICY`` into ``retry_policy`` right beside the two
    literal flag lines, so DRY-ing the pair into that constant is the obvious
    refactor -- and doing it at only one of the three sites would make a listed
    decision vanish from this pin while it stayed green.  A spread can also
    OVERRIDE a literal flag, so the gaps are reported even when one was found.
    """

    marked = False
    unresolved_spreads: list[int] = []
    for literal_key, value in zip(node.keys, node.values, strict=True):
        if literal_key is None:
            if isinstance(value, ast.Name) and value.id in dict_constants and value.id not in _seen:
                nested_marked, nested_spreads = _marks_manual_retry_required(
                    dict_constants[value.id],
                    literal_true=literal_true,
                    dict_constants=dict_constants,
                    _seen=_seen | {value.id},
                )
                marked = marked or nested_marked
                unresolved_spreads.extend(nested_spreads)
            elif _is_module_constant_spelling(value):
                unresolved_spreads.append(value.lineno)
            continue
        if isinstance(literal_key, ast.Constant) and literal_key.value == "manual_retry_required":
            if literal_true:
                if isinstance(value, ast.Constant) and value.value is True:
                    marked = True
            elif not isinstance(value, ast.Constant):
                marked = True
        if isinstance(value, ast.Dict):
            nested_marked, nested_spreads = _marks_manual_retry_required(
                value, literal_true=literal_true, dict_constants=dict_constants, _seen=_seen
            )
            marked = marked or nested_marked
            unresolved_spreads.extend(nested_spreads)
    return marked, unresolved_spreads


def _written_manual_action_decisions(*, literal_true: bool = True) -> tuple[set[str], list[str]]:
    """Every decision the writers pair with ``manual_retry_required``, plus what would not resolve.

    Known limits, all of them shapes no writer uses today (checked: every
    ``retry_policy`` value in the package is a dict literal): a flag or decision
    reached through ``dict(...)``, through a function return, or through a
    list-of-dicts comprehension is not resolved statically and would be missed.
    A ``decision`` or a flag reached through a ``**spread`` is NOT in that list --
    a spread of a same-module dict constant is resolved, and one spelled like a
    module constant that does not resolve is reported.  It was in that list until
    round 3, silently: the decision lookup ignored spreads entirely and the caller
    then dropped the flag side's spread gaps along with the node.
    """

    decisions: set[str] = set()
    unresolved: list[str] = []
    for path in _MANUAL_ACTION_WRITER_FILES:
        where = path.relative_to(_ORCHESTRATOR_DIR).as_posix()
        tree = _parsed_orchestrator_module(path)
        constants = _module_string_constants(tree)
        dict_constants = _module_dict_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            marked, spread_gaps = _marks_manual_retry_required(
                node, literal_true=literal_true, dict_constants=dict_constants
            )
            if not marked and not spread_gaps:
                continue
            # The gaps are reported BEFORE the decision is looked for, and that
            # order is the round-3 C2 fix.  The old code looked the decision up
            # first and ``continue``d on a miss, which threw the spread gaps away
            # with it -- so a node the flag side had explicitly flagged as
            # unreadable went unreported precisely because its decision was
            # unreadable too.  Two standards inside one pin: a decision that would
            # not resolve was loud, a flag that would not resolve was silent.
            # The five inner ``retry_policy`` sub-dicts this ``continue`` used to
            # swallow stay quiet on their own merits now, not by being dropped:
            # scheduler_candidates.py 1674/2732/2852 spread only
            # ``**_OPERATOR_REENTRY_POLICY``, which RESOLVES against that module's
            # own constants, and scheduler_state_failure.py 1987/2183 spread
            # nothing at all.  Either way they contribute no gaps to report.
            unresolved.extend(f"{where}:{lineno} **spread" for lineno in spread_gaps)
            value = _dict_decision_value(node, dict_constants=dict_constants)
            if value is None:
                continue
            if not marked:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                decisions.add(value.value)
            elif isinstance(value, ast.Name) and value.id in constants:
                decisions.add(constants[value.id])
            else:
                unresolved.append(f"{where}:{value.lineno}")
    return decisions, unresolved


def test_operator_action_decisions_are_closed_over_every_manual_retry_writer() -> None:
    """The listed set must equal what the writers actually mark as needing an operator.

    A missing literal is a SILENT exit 0 -- "nothing waits" for a candidate the
    runbook's second step still handles.  That is how
    ``blocked_operator_reentry_restart_stage_refused`` went unlisted, so the pin is
    the deliverable, not the literal.  Source text + ``ast`` only: no import of the
    writers, and an unresolvable ``decision`` is a failure rather than a silent gap.

    R2-05: the SCAN DOMAIN is derived too -- every module under
    ``services/orchestrator/``, sub-packages included -- because a hand-listed one
    reproduced the same unclosed-set defect one level up.
    """

    from services.orchestrator import operator_action_listing

    decisions, unresolved = _written_manual_action_decisions()

    assert unresolved == []
    assert decisions == {
        "permanent_failure",
        "cancelled_manual_retry_required",
        "blocked_strict_warm_start_init_state_mismatch",
        "blocked_journal_predecessor_identity_quarantine",
        "blocked_operator_reentry_restart_stage_refused",
    }
    assert set(operator_action_listing.OPERATOR_ACTION_DECISIONS) == decisions

    # The other direction.  Matching only the literal ``True`` is correct -- an
    # expression cannot be judged statically -- but on its own it is a back door: a
    # NEW ``manual_retry_required: <expression>`` writer would be invisible to this
    # pin, and it could be the sixth operator decision.  So the expression writers
    # are enumerated too; adding one fails here and has to be dispositioned.
    #
    # The two that exist today are both provably unreachable with the flag true,
    # which is why they are out of the listed set (evidence, not assertion):
    #   * ``scheduler_state_failure.py:514`` (``retry_downstream``) -- the same
    #     function returns None at ``:498`` when ``failure["permanent"]``, so the
    #     dict at ``:500`` is only ever built with the flag False.
    #   * ``scheduler_state_failure.py:1954`` (``retry_failed``) -- no in-function
    #     guard; the guard is at the call site, ``scheduler_state_decision.py:385``
    #     returns the permanent ``blocked`` decision BEFORE the ``retry_failed``
    #     return point at ``:412``, so a permanent candidate never reaches it.
    #   * that same ``_failure_retry()`` evidence also feeds the missing-forcing
    #     channel (``scheduler_state_decision.py:373``), whose four return points all
    #     go through ``_artifact_blocker_evidence`` (``scheduler_state_failure.py:909``):
    #     it writes its own ``decision`` (``:927``) and its own
    #     ``manual_retry_required: False`` (``:947``) and inherits neither, so the
    #     production main path (every live blocked candidate measured on node-22)
    #     does not leak through it either.
    expression_decisions, expression_unresolved = _written_manual_action_decisions(literal_true=False)

    assert expression_unresolved == []
    assert expression_decisions == {"retry_downstream", "retry_failed"}
    assert not expression_decisions & set(operator_action_listing.OPERATOR_ACTION_DECISIONS)


def test_the_help_text_names_every_decision_and_every_non_evaluating_reason() -> None:
    """R2-04: the operator's only online index of this surface must enumerate it.

    The decisions are read from :data:`OPERATOR_ACTION_DECISIONS` rather than
    typed, so a sixth one the closure pin above forces into the module also has to
    reach the help.  ``scope_unknown`` is pinned because the help never mentioned
    it at all, which left an operator holding that reason with nowhere to look it
    up.  ENUMERATION ONLY: the prose logic is not asserted here -- that is what the
    exit-code tests are for.
    """

    from services.orchestrator import operator_action_listing

    help_text = operator_action_listing.LIST_OPERATOR_ACTIONS_HELP

    for decision in operator_action_listing.OPERATOR_ACTION_DECISIONS:
        assert decision in help_text, decision
    for reason in (
        operator_action_listing.SCOPE_NARROWED_REASON,
        operator_action_listing.SCOPE_UNKNOWN_REASON,
        operator_action_listing.STATUS_NOT_EVALUATING_REASON,
        operator_action_listing.SIZE_FALLBACK_NON_EVALUATING_REASON,
        # #2402: the second size-fallback reason -- an operator holding
        # ``size_fallback_source_cycles_summarized`` must be able to look it up.
        operator_action_listing.SIZE_FALLBACK_SUMMARIZED_NON_EVALUATING_REASON,
        operator_action_listing.NO_MODELS_EVALUATED_REASON,
    ):
        assert reason in help_text, reason
    # The six scope keys the reason split is keyed on, and the four documented
    # boundaries of exit 0 (time window, single oldest-cycle slot, inactive models,
    # discovery retraction).
    for key in ("backfill", "operator_filters", "sources", "cycle_window", "counts", "runtime_config"):
        assert key in help_text, key
    assert "--lookback-hours 0" in help_text
    assert "allowed_cycle_hours_utc" in help_text
    # Both closed sets are rendered FROM the constants, not re-typed into the
    # prose: a help string that spells a stale ``gfs/IFS`` or ``(0, 12)`` is the
    # same hand-copy defect one layer out.
    assert "/".join(operator_action_listing.SCOPE_COMPLETE_SOURCES) in help_text
    assert ", ".join(str(hour) for hour in operator_action_listing.SCOPE_COMPLETE_CYCLE_HOURS_UTC) in help_text
    assert "backfill_deferred_waiting_for_prior_cycle" in help_text
    # The inactive-model boundary names the two numbers an operator has to compare,
    # because `exclusions` structurally can not show that gap.
    assert "model_count" in help_text
    assert "active_model_count" in help_text
    # The fourth boundary, and the qualifier it puts on the second.  Without the
    # qualifier, (2) reads as an unconditional promise that an unresolved action is
    # re-listed every pass -- which is false for a cycle the current configuration
    # no longer discovers.  Enumeration only, as above: the prose is not asserted.
    assert "discovery retraction" in help_text
    assert "Four known boundaries" in help_text


def _only_string_tuple_literal(node: ast.AST, where: str) -> tuple[str, ...]:
    tuples = [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Tuple)
        and item.elts
        and all(isinstance(element, ast.Constant) and isinstance(element.value, str) for element in item.elts)
    ]
    assert len(tuples) == 1, f"{where}: expected exactly one string tuple literal, found {len(tuples)}"
    return tuple(element.value for element in tuples[0].elts)  # type: ignore[attr-defined]


def test_the_three_spellings_of_the_production_source_set_agree() -> None:
    """Drift in any of the three spellings of the production source set must be loud.

    The authority is ``scheduler.py``'s ``DEFAULT_PRODUCTION_SOURCES``.  The
    listing surface no longer keeps a copy of it -- ``SCOPE_COMPLETE_SOURCES`` is
    an alias, asserted by identity elsewhere in this file -- but ``cli.py``'s
    ``resolved_sources`` fallback IS an independent literal, and it is what a pass
    run without ``--source``/``NHMS_SCHEDULER_SOURCES`` actually records.  Both are
    read with ``ast`` rather than imported so this pin still fails loudly if either
    literal is edited to a different tuple.
    """

    from services.orchestrator import operator_action_listing

    scheduler_tree = ast.parse((_ORCHESTRATOR_DIR / "scheduler.py").read_text(encoding="utf-8"))
    default_sources = [
        node
        for node in scheduler_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DEFAULT_PRODUCTION_SOURCES" for target in node.targets)
    ]
    assert len(default_sources) == 1

    cli_tree = ast.parse((_ORCHESTRATOR_DIR / "cli.py").read_text(encoding="utf-8"))
    resolved_sources = [
        node
        for node in ast.walk(cli_tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "resolved_sources" for target in node.targets)
    ]
    assert len(resolved_sources) == 1

    assert _only_string_tuple_literal(default_sources[0], "scheduler.py DEFAULT_PRODUCTION_SOURCES") == ("gfs", "IFS")
    assert _only_string_tuple_literal(resolved_sources[0], "cli.py resolved_sources fallback") == ("gfs", "IFS")
    assert tuple(operator_action_listing.SCOPE_COMPLETE_SOURCES) == ("gfs", "IFS")
    assert tuple(_SCOPE_COMPLETE_SOURCES) == ("gfs", "IFS")


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
