"""Pre-execution reservation lease and orphan reservations (#2405, design D4).

A pass that crashed between reserving its evidence slot and writing the terminal
artifact leaves a ``<pass_id>.pre_execution.json`` behind and nothing else.  The
reservation is excluded from the pass scan, so before this change
``list-operator-actions`` answered ``exit 0`` off the older decidable pass and
said "nothing waits" about a submit pass whose outcome nobody knows.

Option L2 (design D4): the reservation payload embeds its own
``lease = {ttl_seconds, heartbeat_interval_seconds}`` and the pass's existing
``_LeaseHeartbeat`` -- opt-in, registered only after the reservation is
``reserved`` -- refreshes the reservation file's mtime while the pass lives.  The
reader then judges staleness the way ``scheduler_lease`` judges a lock: mtime
age over twice the recorded ttl (or no ``lease`` block at all, the legacy
writer) is an orphan, ordered by ``reserved_at`` against the newest evaluating
and scope-complete pass's payload ``started_at`` -- NEVER by file mtimes, which
the heartbeat now moves.

Every ``services.*`` / ``tests.*`` import is function-local on purpose: this file
must not join the frozen top-level importer set of
``tests.test_production_scheduler`` (``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

_PASS_ID = "scheduler_2026052112_decidable01"
_RESERVATION_SUFFIX = ".pre_execution.json"
_PASS_STARTED_AT = "2026-05-21T12:00:00Z"


# ---------------------------------------------------------------------------
# Writer-produced fixtures (no hand-written evidence payloads)
# ---------------------------------------------------------------------------


def _evidence_context(root: Path) -> tuple[Any, Path]:
    from tests.test_production_scheduler import _config, _dt, _scheduler_evidence_test_context

    root.mkdir(parents=True, exist_ok=True)
    config = _config(root, now=_dt(_PASS_STARTED_AT))
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return _scheduler_evidence_test_context(config), evidence_dir


def _write_pass(root: Path, payload: dict[str, Any], *, mtime: float | None = None) -> Path:
    """Write ``payload`` through the REAL ``write_evidence`` under ``root``."""

    from services.orchestrator import scheduler_evidence

    context, evidence_dir = _evidence_context(root)
    scheduler_evidence.write_evidence(context, str(payload["pass_id"]), dict(payload))
    path = evidence_dir / f"{payload['pass_id']}.json"
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_reservation(
    root: Path,
    pass_id: str,
    *,
    reserved_at: str,
    started_at: str = _PASS_STARTED_AT,
    mtime: float | None = None,
    drop_lease: bool = False,
) -> Path:
    """Reserve through the REAL ``reserve_pre_execution_evidence``."""

    from services.orchestrator import scheduler_evidence
    from tests.test_production_scheduler import _dt

    context, evidence_dir = _evidence_context(root)
    payload = scheduler_evidence.reserve_pre_execution_evidence(
        context,
        pass_id,
        _dt(started_at),
        1,
        now=_dt(reserved_at),
    )
    assert payload["status"] == "reserved", payload
    path = evidence_dir / f"{pass_id}{_RESERVATION_SUFFIX}"
    if drop_lease:
        # Exactly what a pre-#2405 writer left behind: the same payload, no lease.
        persisted = json.loads(path.read_text(encoding="utf-8"))
        persisted.pop("lease", None)
        path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(scope="module")
def decidable_pass_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A REAL ``run_once()`` pass that reads evaluating, scope-complete and actionless.

    ``sources`` is widened to the whole production set afterwards for the same
    reason the #1905 suite does it: the fixture scheduler is a single-source
    geometry and would otherwise be read ``scope_narrowed``.
    """

    from tests.test_production_scheduler import (
        FakeAdapter,
        FakeRegistry,
        ProductionScheduler,
        _config,
        _dt,
        _model,
    )

    tmp_path = tmp_path_factory.mktemp("decidable_pass").resolve()
    result = ProductionScheduler(
        _config(
            tmp_path,
            now=_dt(_PASS_STARTED_AT),
            backfill_enabled=True,
            max_cycles_per_source=1,
        ),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    ).run_once()
    evidence = json.loads(json.dumps(result.evidence))
    evidence["sources"] = ["gfs", "IFS"]
    evidence["pass_id"] = _PASS_ID
    return evidence


def _pass_payload(base: dict[str, Any], *, pass_id: str, started_at: str) -> dict[str, Any]:
    payload = json.loads(json.dumps(base))
    payload["pass_id"] = pass_id
    payload["started_at"] = started_at
    return payload


def _listing(evidence_dir: Path) -> tuple[dict[str, Any], int]:
    from services.orchestrator.operator_action_listing import list_operator_actions

    return list_operator_actions(evidence_root=str(evidence_dir), passes=6)


def _stale_mtime(ttl_seconds: int = 3_600) -> float:
    return time.time() - (4 * ttl_seconds)


# ---------------------------------------------------------------------------
# 3.1 / 3.2 / 3.4 -- the reader rule
# ---------------------------------------------------------------------------


def test_a_decidable_pass_without_reservations_still_answers_zero(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """The baseline every test below moves away from: exit 0, no orphans."""

    path = _write_pass(tmp_path / "root", decidable_pass_evidence)
    receipt, code = _listing(path.parent)

    assert (code, receipt["operator_actions"]) == (0, [])
    assert receipt["orphan_reservations"] == []


def test_a_crashed_submit_pass_is_not_answered_by_an_older_pass(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.1: stale lease, no terminal artifact, newer than the decidable pass -> exit 3."""

    path = _write_pass(tmp_path / "root", decidable_pass_evidence)
    orphan_id = "scheduler_2026052112_crashed0001"
    _write_reservation(
        tmp_path / "root",
        orphan_id,
        reserved_at="2026-05-21T12:30:00Z",
        mtime=_stale_mtime(),
    )

    receipt, code = _listing(path.parent)

    assert code == 3
    assert receipt["operator_actions"] == []
    assert receipt["orphan_reservations"] == [
        {
            "reservation": f"{orphan_id}{_RESERVATION_SUFFIX}",
            "pass_id": orphan_id,
            "reserved_at": "2026-05-21T12:30:00Z",
            "reason": "lease_stale",
        }
    ]


def test_an_in_flight_reservation_does_not_add_noise(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.2: a heartbeat-fresh reservation leaves the receipt exactly as it was."""

    path = _write_pass(tmp_path / "root", decidable_pass_evidence)
    before = _listing(path.parent)
    _write_reservation(
        tmp_path / "root",
        "scheduler_2026052113_inflight001",
        reserved_at="2026-05-21T13:00:00Z",
    )

    assert _listing(path.parent) == before
    assert before[1] == 0


def test_a_reservation_whose_terminal_artifact_exists_produces_nothing(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.4: the pass finished; its reservation is history, whatever its mtime says.

    The reservation is written FIRST on purpose: ``reserve_pre_execution_evidence``
    refuses to reserve once the terminal artifact exists.
    """

    reservation = _write_reservation(
        tmp_path / "root",
        _PASS_ID,
        reserved_at="2026-05-21T12:30:00Z",
        mtime=_stale_mtime(),
    )
    path = _write_pass(tmp_path / "root", decidable_pass_evidence)

    receipt, code = _listing(path.parent)

    assert reservation.is_file()
    assert (code, receipt["orphan_reservations"]) == (0, [])


def test_a_legacy_reservation_without_a_lease_block_is_an_orphan(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.4: fail-safe -- a pre-#2405 reservation records no ttl, so freshness is unprovable."""

    path = _write_pass(tmp_path / "root", decidable_pass_evidence)
    legacy_id = "scheduler_2026052112_legacy000001"
    _write_reservation(
        tmp_path / "root",
        legacy_id,
        reserved_at="2026-05-21T12:30:00Z",
        drop_lease=True,
    )

    receipt, code = _listing(path.parent)

    assert code == 3
    assert [row["reason"] for row in receipt["orphan_reservations"]] == ["lease_absent"]
    assert receipt["orphan_reservations"][0]["pass_id"] == legacy_id


def test_ordering_reads_reserved_at_against_started_at_not_file_mtimes(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.4: the two clocks disagree on purpose.

    The mtime-NEWEST evaluating pass started at 12:00 while an older-by-mtime pass
    started at 14:00.  A reservation reserved at 13:00 is newer than the newest
    evaluating pass's ``started_at`` and so an orphan; reading the other pass's
    ``started_at``, or the files' mtimes, would call it older and answer ``exit 0``.
    A reservation reserved at 11:00 is older -- and stays a non-orphan even though
    its mtime is the freshest file in the root.
    """

    root = tmp_path / "root"
    _write_pass(
        root,
        _pass_payload(
            decidable_pass_evidence,
            pass_id="scheduler_2026052114_laterstart1",
            started_at="2026-05-21T14:00:00Z",
        ),
        mtime=time.time() - 600,
    )
    newest = _write_pass(
        root,
        _pass_payload(decidable_pass_evidence, pass_id=_PASS_ID, started_at=_PASS_STARTED_AT),
        mtime=time.time() - 300,
    )
    _write_reservation(
        root,
        "scheduler_2026052112_between0001",
        reserved_at="2026-05-21T13:00:00Z",
        mtime=_stale_mtime(),
    )
    _write_reservation(
        root,
        "scheduler_2026052111_before000001",
        reserved_at="2026-05-21T11:00:00Z",
    )

    receipt, code = _listing(newest.parent)

    assert code == 3
    assert [row["pass_id"] for row in receipt["orphan_reservations"]] == [
        "scheduler_2026052112_between0001"
    ]


def test_an_orphan_reservation_never_overrides_a_listed_action(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """Exit 1 still wins: the orphan is reported, the action is what the operator acts on."""

    from tests.test_production_scheduler import _dt

    payload = json.loads(json.dumps(decidable_pass_evidence))
    payload["blocked_candidates"] = [
        {
            "candidate_id": "gfs_2026052106_model_a",
            "source_id": "gfs",
            "cycle_time_utc": _dt("2026-05-21T06:00:00Z").isoformat().replace("+00:00", "Z"),
            "model_id": "model_a",
            "decision": "permanent_failure",
            "reason": "retry_limit_exhausted",
        }
    ]
    path = _write_pass(tmp_path / "root", payload)
    _write_reservation(
        tmp_path / "root",
        "scheduler_2026052112_crashed0002",
        reserved_at="2026-05-21T12:30:00Z",
        mtime=_stale_mtime(),
    )

    receipt, code = _listing(path.parent)

    assert code == 1
    assert len(receipt["operator_actions"]) == 1
    assert [row["reason"] for row in receipt["orphan_reservations"]] == ["lease_stale"]


def test_the_help_text_names_the_orphan_reservation_receipt_key() -> None:
    from services.orchestrator import operator_action_listing

    assert "orphan_reservations" in operator_action_listing.LIST_OPERATOR_ACTIONS_HELP


# ---------------------------------------------------------------------------
# 3.3 / 3.3a -- the writer half
# ---------------------------------------------------------------------------


def test_the_reservation_payload_carries_its_lease(tmp_path: Path) -> None:
    """3.3: ttl and heartbeat interval come from the config the pass runs with."""

    from services.orchestrator.scheduler_lease import lease_heartbeat_interval_seconds
    from tests.test_production_scheduler import _config, _dt

    pass_id = "scheduler_2026052112_lease0000001"
    path = _write_reservation(tmp_path / "root", pass_id, reserved_at=_PASS_STARTED_AT)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    config = _config(tmp_path / "root", now=_dt(_PASS_STARTED_AT))

    assert persisted["lease"] == {
        "ttl_seconds": config.lock_ttl_seconds,
        "heartbeat_interval_seconds": lease_heartbeat_interval_seconds(config.lock_ttl_seconds),
    }
    assert lease_heartbeat_interval_seconds(config.lock_ttl_seconds) * 3 <= config.lock_ttl_seconds
    assert persisted["pass_id"] == pass_id


class _AlwaysRenewingLease:
    def __init__(self) -> None:
        self.renewals = 0

    def renew(self, *, pass_id: str) -> bool:
        self.renewals += 1
        return True


def _wait_for(predicate: Any, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_heartbeat_refreshes_the_registered_reservation_mtime(tmp_path: Path) -> None:
    """3.3: the live pass's heartbeat is what keeps its reservation out of the orphan list."""

    from services.orchestrator.scheduler_lease import _LeaseHeartbeat

    path = tmp_path / f"scheduler_2026052112_touch0000001{_RESERVATION_SUFFIX}"
    path.write_text("{}", encoding="utf-8")
    stale = time.time() - 10_000
    os.utime(path, (stale, stale))

    heartbeat = _LeaseHeartbeat(_AlwaysRenewingLease(), "pass-1", 0.01)
    heartbeat.register_touch_path(path)
    heartbeat.start()
    try:
        refreshed = _wait_for(lambda: path.stat().st_mtime > stale + 1)
    finally:
        heartbeat.stop()

    assert refreshed
    assert heartbeat.lost is False


def test_a_heartbeat_without_a_registered_path_touches_nothing(tmp_path: Path) -> None:
    """3.3a: the touch is OPT-IN -- the migration and NFS-proof callers are unchanged."""

    from services.orchestrator.scheduler_lease import _LeaseHeartbeat

    path = tmp_path / f"scheduler_2026052112_untouched001{_RESERVATION_SUFFIX}"
    path.write_text("{}", encoding="utf-8")
    stale = time.time() - 10_000
    os.utime(path, (stale, stale))

    lease = _AlwaysRenewingLease()
    heartbeat = _LeaseHeartbeat(lease, "pass-1", 0.01)
    heartbeat.start()
    try:
        assert _wait_for(lambda: lease.renewals >= 3)
    finally:
        heartbeat.stop()

    assert path.stat().st_mtime == pytest.approx(stale, abs=1)


def test_a_touch_failure_neither_raises_nor_marks_the_lease_lost(tmp_path: Path) -> None:
    """3.3: the reservation is deleted under the heartbeat (retention does exactly this).

    The lease is still held, so the heartbeat must keep renewing and must NOT set
    ``lost`` -- a lost lease ends the pass, and a missing file is not a lost lease.
    """

    from services.orchestrator.scheduler_lease import _LeaseHeartbeat

    path = tmp_path / f"scheduler_2026052112_removed00001{_RESERVATION_SUFFIX}"
    path.write_text("{}", encoding="utf-8")

    lease = _AlwaysRenewingLease()
    heartbeat = _LeaseHeartbeat(lease, "pass-1", 0.01)
    heartbeat.register_touch_path(path)
    heartbeat.start()
    try:
        assert _wait_for(lambda: lease.renewals >= 2)
        path.unlink()
        renewals_after_unlink = lease.renewals
        assert _wait_for(lambda: lease.renewals >= renewals_after_unlink + 5)
    finally:
        heartbeat.stop()

    assert heartbeat.lost is False
    assert not path.exists()


def test_a_non_oserror_touch_failure_neither_raises_nor_marks_the_lease_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1 (N1): the promise is unconditional, so the catch must be too.

    ``_touch`` used to swallow only ``OSError`` while its docstring promised it
    would never raise out of the heartbeat thread nor set ``lost``.  Anything
    else -- a ``ValueError`` out of a path conversion, an instrumented
    ``os.utime`` -- killed the thread, which stops RENEWING as well, so the pass
    loses its lease over a failure of pure observability.
    """

    from services.orchestrator.scheduler_lease import _LeaseHeartbeat

    path = tmp_path / f"scheduler_2026052112_raising00001{_RESERVATION_SUFFIX}"
    path.write_text("{}", encoding="utf-8")
    attempts = {"count": 0}

    def _raise_runtime_error(*args: Any, **kwargs: Any) -> None:
        attempts["count"] += 1
        raise RuntimeError("utime is instrumented and angry")

    lease = _AlwaysRenewingLease()
    heartbeat = _LeaseHeartbeat(lease, "pass-1", 0.01)
    heartbeat.register_touch_path(path)
    monkeypatch.setattr(os, "utime", _raise_runtime_error)
    heartbeat.start()
    try:
        assert _wait_for(lambda: attempts["count"] >= 2)
        renewals_after_raise = lease.renewals
        assert _wait_for(lambda: lease.renewals >= renewals_after_raise + 5)
    finally:
        heartbeat.stop()

    assert heartbeat.lost is False


def test_run_once_registers_the_touch_path_only_after_the_reservation_is_reserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.3a: the heartbeat starts at pass start, BEFORE any reservation exists.

    Registering it there would point the touch at a path that does not exist yet
    (and, if the reservation is refused, never will).  A real submitting pass is
    driven here and the registration is observed together with the file's
    existence at that moment.
    """

    from services.orchestrator import scheduler as scheduler_module
    from tests.test_production_scheduler import (
        FakeActiveRepository,
        FakeAdapter,
        FakeProductionOrchestrator,
        FakeRegistry,
        ProductionScheduler,
        _AlwaysReadyCanonicalReadinessProvider,
        _config,
        _dt,
        _model,
    )

    registrations: list[tuple[Path, bool]] = []
    real_heartbeat = scheduler_module._LeaseHeartbeat

    class _ObservingHeartbeat(real_heartbeat):  # type: ignore[misc, valid-type]
        def register_touch_path(self, path: Any) -> None:
            registrations.append((Path(path), Path(path).is_file()))
            super().register_touch_path(path)

    monkeypatch.setattr(scheduler_module, "_LeaseHeartbeat", _ObservingHeartbeat)

    def _run(root: Path, *, dry_run: bool) -> Any:
        root.mkdir(parents=True, exist_ok=True)
        return ProductionScheduler(
            _config(root, now=_dt(_PASS_STARTED_AT), dry_run=dry_run),
            registry=FakeRegistry([_model("model_a", "basin_a")]),
            adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
            active_repository=FakeActiveRepository(active=False),
            canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
            orchestrator_factory=lambda _source_id: FakeProductionOrchestrator(),
        ).run_once()

    dry = _run(tmp_path / "dry", dry_run=True)
    # A dry run mutates nothing, so it reserves nothing: the key is omitted.
    assert "evidence_pre_execution" not in dry.evidence
    assert registrations == []

    result = _run(tmp_path / "live", dry_run=False)

    assert result.evidence["evidence_pre_execution"]["status"] == "reserved"
    assert len(registrations) == 1
    registered_path, existed_when_registered = registrations[0]
    assert registered_path.name == f"{result.pass_id}{_RESERVATION_SUFFIX}"
    assert existed_when_registered is True


def test_the_other_lease_heartbeat_callers_are_unchanged() -> None:
    """3.3a: only ``run_once`` registers a touch path; the two other users do not."""

    for relative in (
        "services/orchestrator/file_orchestration_migration.py",
        "scripts/m24_lease_nfs_proof.py",
    ):
        source = Path(relative).read_text(encoding="utf-8")
        assert "_LeaseHeartbeat" in source, relative
        assert "register_touch_path" not in source, relative


# ---------------------------------------------------------------------------
# 3.5 -- the reader stays root-local and importer-free
# ---------------------------------------------------------------------------


def test_the_reservation_scan_stays_inside_the_evidence_root(
    tmp_path: Path,
    decidable_pass_evidence: dict[str, Any],
) -> None:
    """3.5: reservations ABOVE the root, or nested under it, are never read.

    The whole surface is defined as "the top level of --evidence-root"; a scan
    that walked up or down would make the exit code depend on a directory the
    operator did not name -- both decoys here would otherwise be orphans (no
    terminal artifact, no lease, a ``reserved_at`` newer than any pass).
    """

    root = _write_pass(tmp_path / "root", decidable_pass_evidence).parent
    decoy = json.dumps({"pass_id": "scheduler_2999_decoy", "reserved_at": "2999-01-01T00:00:00Z"})
    (root.parent / f"scheduler_2999010100_outside00001{_RESERVATION_SUFFIX}").write_text(decoy, encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / f"scheduler_2999010100_nested000001{_RESERVATION_SUFFIX}").write_text(decoy, encoding="utf-8")

    receipt, code = _listing(root)

    assert (code, receipt["orphan_reservations"]) == (0, [])
