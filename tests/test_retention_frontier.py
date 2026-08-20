"""Decision-table tests for the pass-receipt frontier read (issue #1407).

Every cell of design D3's table is pinned here: the reason enumeration is the
fail-closed contract of the out-of-pass deletion surfaces, so an unlisted or
drifting reason is a defect, not a detail.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator.retention_frontier import (
    DEFAULT_MAX_AGE_HOURS,
    FRONTIER_MAX_AGE_ENV,
    max_age_from_env,
    read_latest_pass_frontier,
)

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=24)
BOUND = datetime(2026, 5, 20, 6, 0, tzinfo=UTC)


def _receipt_payload(
    pass_id: str,
    *,
    started_at: datetime | str | None,
    retention: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"pass_id": pass_id}
    if started_at is not None:
        payload["started_at"] = (
            started_at
            if isinstance(started_at, str)
            else started_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
    if retention is not None:
        payload["retention"] = retention
    return payload


def _write_receipt(
    evidence_dir: Path,
    pass_id: str,
    *,
    started_at: datetime | str | None = NOW,
    retention: dict[str, Any] | None = None,
    name: str | None = None,
) -> Path:
    path = evidence_dir / (name or f"{pass_id}.json")
    path.write_text(
        json.dumps(_receipt_payload(pass_id, started_at=started_at, retention=retention)),
        encoding="utf-8",
    )
    return path


def _completed(bound: Any, source: str | None = "scheduler_pass") -> dict[str, Any]:
    frontier: dict[str, Any] = {
        "active_lower_bound": bound.isoformat() if isinstance(bound, datetime) else bound,
        "source": None if bound is None else source,
        "protected_count": 0,
    }
    return {"status": "completed", "enabled": True, "frontier": frontier}


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    path = tmp_path / "evidence"
    path.mkdir()
    return path


def _read(evidence_dir: Path, *, now: datetime = NOW, max_age: timedelta = MAX_AGE):
    return read_latest_pass_frontier(evidence_dir, now=now, max_age=max_age)


# ---------------------------------------------------------------------------
# ok cells
# ---------------------------------------------------------------------------
def test_fresh_receipt_with_bound_is_ok(evidence_dir: Path) -> None:
    path = _write_receipt(evidence_dir, "pass-1", retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.reason is None
    assert result.active_lower_bound == BOUND
    assert result.source == "receipt:scheduler_pass"
    assert result.receipt_path == str(path)
    assert result.receipt_started_at == NOW


def test_fresh_receipt_with_null_bound_is_ok_and_mirrors_the_pass(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=_completed(None))

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.active_lower_bound is None
    # The mirror label; the receipt's own frontier block records a null source.
    assert result.source == "receipt:none"


def test_bound_present_without_source_label_still_reads_ok(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=_completed(BOUND, source=None))

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.active_lower_bound == BOUND
    assert result.source == "receipt:none"


def test_bound_with_offset_timezone_is_normalised_to_utc(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=_completed("2026-05-20T08:00:00+02:00"))

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.active_lower_bound == BOUND


# ---------------------------------------------------------------------------
# unavailable cells
# ---------------------------------------------------------------------------
def test_missing_evidence_dir_is_unavailable(tmp_path: Path) -> None:
    result = _read(tmp_path / "absent")

    assert (result.status, result.reason) == ("unavailable", "evidence_dir_missing")
    assert result.receipt_path is None


def test_empty_evidence_dir_has_no_readable_receipt(evidence_dir: Path) -> None:
    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "no_readable_receipt")


def test_unparseable_and_started_at_less_receipts_are_not_selected(evidence_dir: Path) -> None:
    (evidence_dir / "broken.json").write_text("{not json", encoding="utf-8")
    _write_receipt(evidence_dir, "pass-no-time", started_at=None, retention=_completed(BOUND))
    _write_receipt(evidence_dir, "pass-bad-time", started_at="not-a-time", retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "no_readable_receipt")


def test_oversized_receipt_is_skipped(evidence_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.orchestrator.retention_frontier.MAX_EVIDENCE_BYTES", 32)
    _write_receipt(evidence_dir, "pass-1", retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "no_readable_receipt")


def test_stale_receipt_is_unavailable(evidence_dir: Path) -> None:
    path = _write_receipt(
        evidence_dir,
        "pass-1",
        started_at=NOW - timedelta(hours=25),
        retention=_completed(BOUND),
    )

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "receipt_stale")
    assert result.receipt_path == str(path)
    assert result.active_lower_bound is None


def test_future_dated_receipt_is_stale_not_trusted(evidence_dir: Path) -> None:
    """Freshness is two-sided (review round-1 B).

    A clock jump or a hand-copied receipt can carry a ``started_at`` in the
    future. Such a receipt always wins selection, so a one-sided check would
    let it stay "fresh" forever -- and its null bound would masquerade as a
    healthy pass mirror while wall-clock deletion runs unprotected.
    """
    path = _write_receipt(
        evidence_dir,
        "pass-future",
        started_at=NOW + timedelta(hours=25),
        retention=_completed(None),
    )

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "receipt_stale")
    assert result.receipt_path == str(path)
    assert result.active_lower_bound is None


def test_future_dated_receipt_within_the_cap_is_still_fresh(evidence_dir: Path) -> None:
    """Small forward skew is tolerated symmetrically, not treated as a fault."""
    _write_receipt(
        evidence_dir,
        "pass-skewed",
        started_at=NOW + timedelta(hours=1),
        retention=_completed(BOUND),
    )

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.active_lower_bound == BOUND


def test_future_receipt_is_selected_then_staled_not_skipped(evidence_dir: Path) -> None:
    """A future receipt must not silently fall back to an older one.

    Skipping it at selection time would hide the clock fault behind a
    plausible-looking older bound; selecting it and calling it stale surfaces
    the fault as a blocker.
    """
    _write_receipt(evidence_dir, "pass-genuine", started_at=NOW, retention=_completed(BOUND))
    future = _write_receipt(
        evidence_dir,
        "pass-future",
        started_at=NOW + timedelta(days=3),
        retention=_completed(None),
    )

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "receipt_stale")
    assert result.receipt_path == str(future)


def test_receipt_exactly_at_the_freshness_cap_is_still_fresh(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", started_at=NOW - MAX_AGE, retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert result.status == "ok"


def test_receipt_without_retention_key_lacks_the_frontier_block(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=None)

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "frontier_block_missing")


def test_completed_retention_without_frontier_block_is_block_missing(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention={"status": "completed", "enabled": True})

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "frontier_block_missing")


def test_disabled_pass_retention_wins_over_missing_block(evidence_dir: Path) -> None:
    """Priority anchor (design D1/N2).

    The disabled form has a ``retention`` key and no ``frontier`` block, so both
    branches match; the not-run reason must win or the operator recovery path
    (re-run a pass with retention enabled) is never surfaced.
    """
    _write_receipt(evidence_dir, "pass-1", retention={"status": "disabled", "enabled": False})

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "pass_retention_not_run")


def test_errored_pass_retention_is_not_run(evidence_dir: Path) -> None:
    _write_receipt(
        evidence_dir,
        "pass-1",
        retention={"status": "error", "enabled": True, "error": "boom"},
    )

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "pass_retention_not_run")


def test_malformed_bound_is_invalid_not_silently_null(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=_completed("2026-13-99T99:00:00Z"))

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "frontier_bound_invalid")
    assert result.active_lower_bound is None


def test_non_string_bound_is_invalid(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=_completed(1234567890))

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "frontier_bound_invalid")


def test_unexpected_error_is_wrapped_not_raised(
    evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_entries: list[Path]) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("services.orchestrator.retention_frontier._select_latest_receipt", _boom)
    _write_receipt(evidence_dir, "pass-1", retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "frontier_read_error")


def test_evidence_dir_derivation_failure_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``evidence_dir_unresolved`` is produced by the CLI wrapper layer (D1/N3)."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(tmp_path / "outside" / "evidence"))

    result, evidence_dir = cli._cleanup_frontier(now=NOW)

    assert (result.status, result.reason) == ("unavailable", "evidence_dir_unresolved")
    assert result.active_lower_bound is None
    # Nothing to disclose: the derivation itself failed (#1503).
    assert evidence_dir is None


# ---------------------------------------------------------------------------
# selection rules
# ---------------------------------------------------------------------------
def test_latest_is_chosen_by_started_at_not_mtime(evidence_dir: Path) -> None:
    newer = _write_receipt(
        evidence_dir,
        "pass-a",
        started_at=NOW,
        retention=_completed(BOUND),
    )
    older = _write_receipt(
        evidence_dir,
        "pass-b",
        started_at=NOW - timedelta(hours=2),
        retention=_completed(BOUND - timedelta(days=5)),
    )
    # Touch the older receipt last so mtime order contradicts started_at order.
    os.utime(newer, (1_700_000_000, 1_700_000_000))
    os.utime(older, (1_800_000_000, 1_800_000_000))

    result = _read(evidence_dir)

    assert result.receipt_path == str(newer)
    assert result.active_lower_bound == BOUND


def test_started_at_tie_breaks_on_lexically_greatest_filename(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-a", retention=_completed(BOUND - timedelta(days=5)))
    winner = _write_receipt(evidence_dir, "pass-z", retention=_completed(BOUND))

    result = _read(evidence_dir)

    assert result.receipt_path == str(winner)
    assert result.active_lower_bound == BOUND


def test_pre_execution_reservation_never_shadows_the_final_receipt(evidence_dir: Path) -> None:
    final = _write_receipt(evidence_dir, "pass-1", retention=_completed(BOUND))
    # Same started_at, no retention block; its name sorts after "pass-1.json".
    _write_receipt(evidence_dir, "pass-1", retention=None, name="pass-1.pre_execution.json")

    result = _read(evidence_dir)

    assert result.status == "ok"
    assert result.receipt_path == str(final)
    assert result.active_lower_bound == BOUND


def test_only_pre_execution_reservations_read_as_no_readable_receipt(evidence_dir: Path) -> None:
    _write_receipt(evidence_dir, "pass-1", retention=None, name="pass-1.pre_execution.json")

    result = _read(evidence_dir)

    assert (result.status, result.reason) == ("unavailable", "no_readable_receipt")


# ---------------------------------------------------------------------------
# freshness cap configuration
# ---------------------------------------------------------------------------
def test_max_age_defaults_to_24_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FRONTIER_MAX_AGE_ENV, raising=False)

    assert max_age_from_env() == timedelta(hours=DEFAULT_MAX_AGE_HOURS)


@pytest.mark.parametrize("value", ["", "0", "-3", "abc", "9" * 30])
def test_max_age_falls_back_to_the_default_for_unusable_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Parsing is total: an hour count past timedelta's range is unusable, not fatal.

    ``9`` * 30 parses as an int but overflows ``timedelta`` -- an OverflowError
    is not a ValueError, so an unguarded construction escapes both CLI
    entrypoints as a bare traceback (review round-1 A).
    """
    monkeypatch.setenv(FRONTIER_MAX_AGE_ENV, value)

    assert max_age_from_env() == timedelta(hours=DEFAULT_MAX_AGE_HOURS)


def test_max_age_reads_configured_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FRONTIER_MAX_AGE_ENV, "6")

    assert max_age_from_env() == timedelta(hours=6)
