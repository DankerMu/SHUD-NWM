"""Heartbeat-refreshed reservation mtime x evidence retention on ONE root (#2564).

The pass's lease heartbeat touches ``<pass_id>.pre_execution.json`` while the
pass lives, and a touch can land between the terminal write and the heartbeat
stop -- so a reservation can end up NEWER than its own terminal file.  The
evidence retention size pass deletes oldest first, so in that inverted order it
removes the terminal file before the reservation.  These cases run the REAL
retention policy (``scripts/node22_scheduler_evidence_retention.run_retention``)
and then the REAL reader (``list_operator_actions``) on the same root, and pin
that no ordering degrades into a false ``exit 0``.

All mtimes are set with ``os.utime``: no sleep, no patched clock.  They sit two
hours back -- past the retention safety window (3600 s), far inside the 90-day
age window -- so only the byte budget decides what goes, and ``max_bytes`` is one
byte under the root's total so the size pass deletes exactly one file.

Every ``services.*`` / ``tests.*`` import is function-local on purpose: this file
must not join the frozen top-level importer set of
``tests.test_production_scheduler`` (``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PASS_ID = "scheduler_2026052112_heartbeat01"
_CRASHED_ID = "scheduler_2026052113_crashed0001"
_OLDER_ID = "scheduler_2026052112_olderpass01"
_RESERVATION_SUFFIX = ".pre_execution.json"


def _reserve(root: Path, pass_id: str, *, reserved_at: str, mtime: float) -> Path:
    """Reserve through the REAL writer, then record the design's ``ttl_seconds=60`` lease."""

    from tests.test_operator_action_reservation_lease import _write_reservation

    path = _write_reservation(root, pass_id, reserved_at=reserved_at)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["lease"] = {**persisted["lease"], "ttl_seconds": 60}
    path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _terminal(evidence_dir: Path, pass_id: str, *, mtime: float, with_action: bool) -> Path:
    """An evaluating, scope-complete terminal pass; ``with_action`` adds one operator action.

    ``started_at`` is added because the writer always records it and the orphan
    rule orders reservations against it.
    """

    from tests.test_operator_action_listing import _permanent_failure_row, _write_pass

    blocked = [_permanent_failure_row()] if with_action else []
    path = _write_pass(evidence_dir, f"{pass_id}.json", mtime=int(mtime), blocked=blocked)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["started_at"] = "2026-05-21T12:00:00Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _retain_one_file(evidence_dir: Path) -> list[str]:
    """Run the real retention policy with a budget that forces exactly one size deletion."""

    from scripts import node22_scheduler_evidence_retention as retention

    total = sum(path.stat().st_size for path in evidence_dir.iterdir() if path.is_file())
    receipt = retention.run_retention(
        retention.SchedulerEvidenceRetentionConfig(
            evidence_root=evidence_dir,
            retention_days=90,
            max_bytes=total - 1,
            receipt_retention_days=180,
            whitelist_globs=(),
            summary_path=None,
        ),
        now=datetime.now(UTC),
    )
    assert receipt["partial_failure"] is False
    assert all(entry["pass"] == "size" for entry in receipt["deleted_paths"])
    return [Path(entry["path"]).name for entry in receipt["deleted_paths"]]


def _listing(evidence_dir: Path) -> tuple[dict[str, Any], int]:
    from services.orchestrator.operator_action_listing import list_operator_actions

    return list_operator_actions(evidence_root=str(evidence_dir), passes=6)


def _files(evidence_dir: Path) -> list[str]:
    return sorted(path.name for path in evidence_dir.iterdir() if path.is_file())


def _two_hours_ago() -> int:
    return int(time.time()) - 7_200


def _pass_with_reservation(tmp_path: Path, *, reservation_offset: int) -> tuple[Path, Path, Path]:
    """``P.pre_execution.json`` + ``P.json`` as the only two files; the reservation is written first."""

    base = _two_hours_ago()
    reservation = _reserve(
        tmp_path.resolve() / "root",
        _PASS_ID,
        reserved_at="2026-05-21T12:00:00Z",
        mtime=base + reservation_offset,
    )
    evidence_dir = reservation.parent
    terminal = _terminal(evidence_dir, _PASS_ID, mtime=base, with_action=True)
    assert _files(evidence_dir) == sorted([reservation.name, terminal.name])
    return evidence_dir, reservation, terminal


def test_normal_order_deletes_the_reservation_first_and_the_answer_stands(tmp_path: Path) -> None:
    """Reservation just before its terminal file: oldest first removes the reservation only."""

    evidence_dir, reservation, terminal = _pass_with_reservation(tmp_path, reservation_offset=-1)

    assert _retain_one_file(evidence_dir) == [reservation.name]

    receipt, code = _listing(evidence_dir)
    assert terminal.exists()
    assert code == 1
    assert [action["decision"] for action in receipt["operator_actions"]] == ["permanent_failure"]


def test_inverted_order_loses_the_terminal_file_and_stays_fail_closed(tmp_path: Path) -> None:
    """The heartbeat race: the reservation is 1 s NEWER than its own terminal file.

    Oldest first removes ``P.json`` -- the only pass carrying the operator action
    -- and leaves the reservation.  The reader must not read "nothing waits": no
    evaluating, scope-complete pass remains, so it answers ``exit 3``.  That is
    the trigger, not the orphan rule, which needs an evaluating pass to order a
    reservation against and so reports nothing here.
    """

    evidence_dir, reservation, terminal = _pass_with_reservation(tmp_path, reservation_offset=1)

    assert _retain_one_file(evidence_dir) == [terminal.name]

    receipt, code = _listing(evidence_dir)
    assert _files(evidence_dir) == [reservation.name]
    assert code == 3
    assert receipt["operator_actions"] == []
    assert receipt["orphan_reservations"] == []


def test_a_crashed_pass_under_size_pressure_does_not_fall_back_to_a_clean_older_pass(tmp_path: Path) -> None:
    """A crashed pass's reservation (no terminal file) beside an OLDER clean evaluating pass.

    Before retention the orphan rule answers ``exit 3`` (stale lease, reserved
    after the older pass started).  Oldest first then removes the older terminal
    file -- the only pass the orphan rule could order against -- and the reader
    must still answer ``exit 3``, never fall back to ``exit 0``.
    """

    base = _two_hours_ago()
    reservation = _reserve(
        tmp_path.resolve() / "root",
        _CRASHED_ID,
        reserved_at="2026-05-21T13:00:00Z",
        mtime=base + 10,
    )
    evidence_dir = reservation.parent
    older = _terminal(evidence_dir, _OLDER_ID, mtime=base, with_action=False)
    assert _files(evidence_dir) == sorted([reservation.name, older.name])
    before, before_code = _listing(evidence_dir)
    assert (before_code, before["operator_actions"]) == (3, [])
    assert [row["reservation"] for row in before["orphan_reservations"]] == [reservation.name]

    assert _retain_one_file(evidence_dir) == [older.name]

    receipt, code = _listing(evidence_dir)
    assert _files(evidence_dir) == [reservation.name]
    assert code == 3
    assert receipt["operator_actions"] == []
