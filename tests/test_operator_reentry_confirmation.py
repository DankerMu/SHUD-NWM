"""``confirm-operator-reentry`` (#1555 + #1768): the pinned one-shot operator confirmation.

Write side through the shipped CLI entry ``cli.main([...])``; read side through
the repository accessor and the real scheduler seams.  Journals are REAL file
journals seeded with the §8.7 breaker geometry the backfill suite already pins
(``tests.test_scheduler_backfill``), so every precondition reads live values.
The fixture helpers here are shared with the scheduler-pass closed-loop tests in
``tests/test_production_scheduler.py``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.journal_root_authority import JOURNAL_ROOT_INVALID_MESSAGE
from workers.data_adapters.base import CycleDiscovery

BREAKER_DECISION = "blocked_journal_predecessor_identity_quarantine"
BUDGET_DECISION = "blocked_strict_warm_start_init_state_mismatch"
CONFIRMATION_EVENT_TYPE = "operator_reentry_confirmation"

#: The oldest (and only) cycle of the breaker geometry; cadence 0/6/12/18 means
#: the expected predecessor lead is 6h and a lead-12 token is a POSITIVE mismatch.
BREAKER_CYCLE = "2026-05-21T00:00:00Z"
BREAKER_NOW = "2026-05-21T06:00:00Z"

#: Local "caller said nothing" sentinel for :func:`breaker_scheduler`; forwarding
#: it would shadow the test subclass's own OMITTED sentinel, so the kwarg is
#: simply not passed when the caller leaves it alone.
_BREAKER_CANONICAL_READINESS_PROVIDER_UNSET = object()


def _dt(value: str) -> datetime:
    from tests.test_production_scheduler import _dt as production_dt

    return production_dt(value)


def stale_token(model_id: str = "model_a", *, lead_hours: int = 12) -> str:
    from tests.test_scheduler_backfill import _init_state_id_for

    return _init_state_id_for(BREAKER_CYCLE, lead_hours=lead_hours, model_id=model_id)


def _basin_id(model_id: str) -> str:
    return f"basin_{model_id.removeprefix('model_')}"


def real_rerun(
    tmp_path: Path,
    root: Path,
    basins: list[dict[str, Any]],
    *,
    recorded_tokens: dict[str, str] | None = None,
    slurm_client: Any | None = None,
    job_timeout_seconds: float = 120.0,
    terminal_stage: str | None = None,
) -> Any:
    """Run a scheduler handoff through the REAL forecast orchestrator + reservation path on ``root``.

    Each basin re-selects ``recorded_tokens[model_id]`` (default: its stale token),
    which is what the non-convergent §8.7 geometry does.  Only Slurm is faked.
    """

    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    tokens = recorded_tokens or {}
    handoff = [
        {
            **basin,
            "init_state_id": tokens.get(basin["model_id"], stale_token(basin["model_id"])),
            "init_state_uri": f"s3://nhms/states/{basin['model_id']}/2026052100/state.cfg.ic",
            "init_state_checksum": "sha256:state",
            "init_state_valid_time": BREAKER_CYCLE,
        }
        for basin in basins
    ]
    workspaces = tmp_path / "rerun-workspaces"
    index = len(list(workspaces.iterdir())) if workspaces.exists() else 0
    client = slurm_client if slurm_client is not None else FakeCycleSlurmClient()
    # Distinct Slurm ids per rerun, as a real cluster would hand out, so a later
    # rerun never overwrites an earlier rerun's reconciled per-task row.
    client.next_job = 2000 + 1000 * index
    orchestrator = _orchestrator(
        workspaces / str(index),
        FileOrchestrationJournalRepository(root),
        client,
        job_timeout_seconds=job_timeout_seconds,
        terminal_stage=terminal_stage,
    )
    return orchestrator.orchestrate_cycle("gfs", _dt(BREAKER_CYCLE), handoff)


def seed_breaker_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_ids: tuple[str, ...] = ("model_a",),
    breaker_engaged: bool = True,
) -> Path:
    """Drive the REAL lifecycle to the §8.7 breaker on an initially empty journal.

    Only the object store is hand-seeded (each model's forcing package, sidecar
    and run manifest).  Then, through real scheduler passes and the real
    reservation path: a fresh run records the stale token; the next pass
    quarantines it and submits the rerun; that rerun (stamped with quarantine
    provenance) re-records the same token, so occurrences == threshold == 1.
    ``breaker_engaged=False`` stops after the fresh run (quarantine armed, breaker not).
    """

    from tests.test_production_scheduler import FakeProductionOrchestrator, _seed_recorded_forcing_packages

    root = tmp_path / "journal"
    root.mkdir(parents=True)
    object_store_root = tmp_path / "object-store"
    package_dirs = {model_id: f"forcing/gfs/2026052100/{_basin_id(model_id)}_v1/{model_id}" for model_id in model_ids}
    store = _seed_recorded_forcing_packages(
        monkeypatch,
        object_store_root,
        *(f"s3://nhms/{package_dir}/forcing_package.json" for package_dir in package_dirs.values()),
    )
    for model_id, package_dir in package_dirs.items():
        store.write_bytes_atomic(
            f"{package_dir}/forcing_version_record.json",
            json.dumps(
                {
                    "forcing_package_uri": f"s3://nhms/{package_dir}/",
                    "forcing_version_id": f"forc_gfs_2026052100_{model_id}",
                }
            ).encode("utf-8"),
        )
        run_manifest = object_store_root / "runs" / f"fcst_gfs_2026052100_{model_id}" / "input" / "manifest.json"
        run_manifest.parent.mkdir(parents=True, exist_ok=True)
        run_manifest.write_text(
            json.dumps({"initial_state": {"quality": "fresh", "state_id": stale_token(model_id)}}), encoding="utf-8"
        )

    def _submitted_basins(expected_decision: str | None) -> list[dict[str, Any]]:
        orchestrator = FakeProductionOrchestrator()
        result = breaker_scheduler(tmp_path, root, orchestrator, model_ids=model_ids).run_once()
        assert result.evidence["counts"]["submitted_count"] == len(model_ids), result.evidence["counts"]
        decisions = [(item.get("state_evidence") or {}).get("decision") for item in result.evidence["candidates"]]
        assert decisions == [expected_decision] * len(model_ids), decisions
        (call,) = orchestrator.calls
        return [dict(basin) for basin in call["basins"]]

    assert real_rerun(tmp_path, root, _submitted_basins(None)).status == "complete"
    if breaker_engaged:
        rerun_basins = _submitted_basins("retry_journal_predecessor_identity_mismatch")
        assert real_rerun(tmp_path, root, rerun_basins).status == "complete"
    return root


def breaker_scheduler(
    tmp_path: Path,
    root: Path,
    orchestrator: Any,
    *,
    model_ids: tuple[str, ...] = ("model_a",),
    repository: Any | None = None,
    canonical_readiness_provider: Any = _BREAKER_CANONICAL_READINESS_PROVIDER_UNSET,
    **config_overrides: Any,
) -> Any:
    """A FRESH non-dry-run scheduler over a FRESH repository, backfill enabled, single cycle.

    ``config_overrides`` exists because the defaults below (``backfill_enabled=True``,
    ``lookback_hours=12``) are rejected outright by
    ``scheduler_config/config.py:496-500`` when ``repair_missing_forcing`` is on
    (it demands an exact-cycle, single-cycle, backfill-disabled invocation).
    Round-4's ``breaker_repair_flag_on`` family overrides them here rather than
    forking a second copy of this fixture.  ``canonical_readiness_provider``
    defaults to the test subclass's OMITTED sentinel, exactly as before.
    """

    from tests.test_production_scheduler import FakeAdapter, FakeRegistry, ProductionScheduler, _config, _model

    provider_kwargs: dict[str, Any] = {}
    if canonical_readiness_provider is not _BREAKER_CANONICAL_READINESS_PROVIDER_UNSET:
        provider_kwargs["canonical_readiness_provider"] = canonical_readiness_provider
    return ProductionScheduler(
        _config(
            tmp_path,
            **{
                "now": _dt(BREAKER_NOW),
                "dry_run": False,
                "backfill_enabled": True,
                "max_cycles_per_source": 1,
                "lookback_hours": 12,
                **config_overrides,
            },
        ),
        registry=FakeRegistry([_model(model_id, _basin_id(model_id)) for model_id in model_ids]),
        adapters={"gfs": FakeAdapter("gfs", [(BREAKER_CYCLE, True)])},
        active_repository=repository if repository is not None else FileOrchestrationJournalRepository(root),
        orchestrator_factory=lambda _source_id: orchestrator,
        **provider_kwargs,
    )


def run_confirm(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any] | None, str]:
    # Drop whatever earlier scheduler passes printed (pass timing lines).
    capsys.readouterr()
    try:
        code = cli.main(["confirm-operator-reentry", *argv])
    except SystemExit as error:
        code = int(error.code or 0)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def breaker_confirm_argv(
    root: Path,
    *,
    model_id: str = "model_a",
    pin: int = 1,
    recorded_init_state_id: str | None = None,
    attest: bool = True,
    operator: str = "ops-oncall",
    reason: str = "stale lineage re-recorded; forcing and state re-verified",
) -> list[str]:
    argv = [
        "--journal-root",
        str(root),
        "--source-id",
        "gfs",
        "--cycle-time",
        BREAKER_CYCLE,
        "--model-id",
        model_id,
        "--decision",
        BREAKER_DECISION,
        "--pin",
        str(pin),
        "--recorded-init-state-id",
        recorded_init_state_id if recorded_init_state_id is not None else stale_token(model_id),
        "--operator",
        operator,
        "--reason",
        reason,
    ]
    if attest:
        argv.append("--attest")
    return argv


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _confirmations(root: Path, *, model_id: str = "model_a", decision: str = BREAKER_DECISION) -> list[dict[str, Any]]:
    return FileOrchestrationJournalRepository(root).operator_reentry_confirmations(
        source_id="gfs",
        cycle_time=_dt(BREAKER_CYCLE),
        model_id=model_id,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# D.1 write side
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_no_journal_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch)
    before = _tree_bytes(root)

    code, receipt, _err = run_confirm(breaker_confirm_argv(root, attest=False), capsys)

    assert code == 0
    assert receipt is not None
    assert receipt["decision"] == "dry_run"
    assert receipt["pin"] == 1
    # The pin is the model-level quarantine rerun count; occurrences shows engagement.
    assert receipt["live"] == {"occurrences": 1, "quarantine_rerun_count": 1}
    assert receipt["target"] == {
        "source_id": "gfs",
        "cycle_time": BREAKER_CYCLE,
        "model_id": "model_a",
        "decision": BREAKER_DECISION,
    }
    assert "request_id" not in receipt
    assert _tree_bytes(root) == before


def test_attest_records_a_dedicated_confirmation_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch)

    code, receipt, _err = run_confirm(breaker_confirm_argv(root), capsys)

    assert code == 0
    assert receipt is not None
    assert receipt["decision"] == "recorded"
    assert re.fullmatch(r"[0-9a-f]{32}", receipt["request_id"])
    (details,) = _confirmations(root)
    assert details == {
        "model_id": "model_a",
        "decision": BREAKER_DECISION,
        "pin": 1,
        "operator": "ops-oncall",
        "reason": "stale lineage re-recorded; forcing and state re-verified",
        "request_id": receipt["request_id"],
        "recorded_init_state_id": stale_token(),
    }
    # The stored event type is the dedicated one, on the forecast_cycle entity.
    events = FileOrchestrationJournalRepository(root)._cycle_rows(
        source_id="gfs", cycle_time=_dt(BREAKER_CYCLE), model_id=None
    ).pipeline_events
    (event,) = [event for event in events if event.get("details", {}).get("request_id") == receipt["request_id"]]
    assert event["event_type"] == CONFIRMATION_EVENT_TYPE
    assert event["entity_type"] == "forecast_cycle"
    assert event["entity_id"] == "gfs_2026052100"


def test_budget_confirmation_is_recorded_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=False)
    argv = [
        "--journal-root", str(root), "--source-id", "gfs", "--cycle-time", BREAKER_CYCLE,
        "--model-id", "model_a", "--decision", BUDGET_DECISION, "--pin", "0",
        "--operator", "ops-oncall", "--reason", "budget spent on a converging lineage", "--attest",
    ]  # fmt: skip

    code, receipt, _err = run_confirm(argv, capsys)

    assert code == 0
    assert receipt is not None
    assert receipt["decision"] == "recorded"
    # r3-02: the budget pin is the model's live budget re-entry count (no stamped re-entry yet).
    assert receipt["live"] == {"budget_reentry_count": 0}
    (details,) = _confirmations(root, decision=BUDGET_DECISION)
    assert details["pin"] == 0
    assert "recorded_init_state_id" not in details


@pytest.mark.parametrize(
    ("leg", "expected_reason"),
    [
        ("no_completed_identity", "completed_identity_absent"),
        ("pin_differs_from_live_occurrences", "pin_mismatch"),
        ("token_differs_from_live_token", "recorded_init_state_id_mismatch"),
        ("breaker_not_engaged", "breaker_not_engaged"),
        ("budget_pin_differs_from_live_reentry_count", "pin_mismatch"),
        ("budget_pin_negative", "pin_invalid"),
    ],
)
def test_refused_preconditions_exit_two_and_write_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    leg: str,
    expected_reason: str,
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch, breaker_engaged=leg != "breaker_not_engaged")
    if leg == "no_completed_identity":
        argv = breaker_confirm_argv(root)
        argv[argv.index(BREAKER_CYCLE)] = "2026-05-21T06:00:00Z"
    elif leg == "pin_differs_from_live_occurrences":
        argv = breaker_confirm_argv(root, pin=2)
    elif leg == "token_differs_from_live_token":
        argv = breaker_confirm_argv(root, recorded_init_state_id=stale_token(lead_hours=18))
    elif leg == "breaker_not_engaged":
        argv = breaker_confirm_argv(root)
    else:
        # No stamped budget re-entry yet: the live budget re-entry count is 0.
        argv = breaker_confirm_argv(root, pin=1 if leg == "budget_pin_differs_from_live_reentry_count" else -1)
        argv[argv.index(BREAKER_DECISION)] = BUDGET_DECISION
    before = _tree_bytes(root)

    code, receipt, _err = run_confirm(argv, capsys)

    assert code == 2
    assert receipt is not None
    assert receipt["decision"] == "refused"
    assert receipt["reason"] == expected_reason
    if leg == "budget_pin_differs_from_live_reentry_count":
        assert receipt["live"] == {"budget_reentry_count": 0}
    assert _tree_bytes(root) == before


@pytest.mark.parametrize("blank_flag", ["--operator", "--reason", "--model-id", "--recorded-init-state-id"])
def test_blank_required_argument_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blank_flag: str,
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch)
    argv = breaker_confirm_argv(root)
    argv[argv.index(blank_flag) + 1] = "   "
    before = _tree_bytes(root)

    code, receipt, _err = run_confirm(argv, capsys)

    assert code == 2
    assert receipt is not None
    assert receipt["decision"] == "refused"
    assert receipt["reason"] == "required_argument_blank"
    assert _tree_bytes(root) == before


def test_invalid_journal_root_is_a_typed_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path / "real-journal"
    real.mkdir()
    linked = tmp_path / "linked-journal"
    linked.symlink_to(real, target_is_directory=True)

    code, receipt, err = run_confirm(breaker_confirm_argv(linked), capsys)

    assert code == 2
    assert receipt is None
    assert err.strip() == f"FILE_JOURNAL_INVALID_ROOT: {JOURNAL_ROOT_INVALID_MESSAGE}"
    assert "Traceback" not in err
    assert not any(real.iterdir())


def test_argparse_entrypoint_records_the_same_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch)

    capsys.readouterr()
    code = cli._argparse_main(["confirm-operator-reentry", *breaker_confirm_argv(root)])
    receipt = json.loads(capsys.readouterr().out)

    assert code == 0
    assert receipt["decision"] == "recorded"
    assert [item["request_id"] for item in _confirmations(root)] == [receipt["request_id"]]


# ---------------------------------------------------------------------------
# D.2 marker isolation
# ---------------------------------------------------------------------------


def _breaker_candidate_decisions(tmp_path: Path, root: Path) -> tuple[list[Any], list[Any]]:
    """Candidate-side decision through ``_build_candidates`` (no discovery slot release)."""

    from tests.test_production_scheduler import FakeProductionOrchestrator, _model

    scheduler = breaker_scheduler(tmp_path, root, FakeProductionOrchestrator())
    candidates, blocked, _skipped, _dup, _sync = scheduler._build_candidates(
        models=[scheduler_module._coerce_registered_model(_model("model_a", "basin_a"))],
        cycles=[
            scheduler_module.SchedulerSourceCycle(
                discovery=CycleDiscovery(
                    cycle_id="gfs_2026052100",
                    source_id="gfs",
                    cycle_time=_dt(BREAKER_CYCLE),
                    cycle_hour=0,
                    available=True,
                    status="discovered",
                ),
                horizon={},
            )
        ],
    )
    return candidates, blocked


def test_confirmation_is_never_adopted_as_a_manual_retry_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.test_production_scheduler import FakeProductionOrchestrator

    root = seed_breaker_journal(tmp_path, monkeypatch)
    code, _receipt, _err = run_confirm(breaker_confirm_argv(root), capsys)
    assert code == 0
    # A second stamped rerun lands: occurrences 2, so the pin-1 confirmation is stale.
    confirmed_orchestrator = FakeProductionOrchestrator()
    confirmed = breaker_scheduler(tmp_path, root, confirmed_orchestrator).run_once()
    assert confirmed.evidence["counts"]["submitted_count"] == 1
    assert real_rerun(tmp_path, root, confirmed_orchestrator.calls[0]["basins"]).status == "complete"

    # Marker isolation itself is pinned by the exact stored shape in
    # ``test_attest_records_a_dedicated_confirmation_event`` (dedicated event
    # type, details without ``trigger``/``manual_retry_marker``).  A
    # ``_manual_retry_requested(candidate_state)`` read here could not bite
    # (round 1 cand-05): the marker reader needs ``trigger: "manual"`` too, so
    # even an event type flipped to ``retry`` stays unadopted; verified by
    # mutation, then removed.  What remains is the decision the marker would
    # have changed.
    candidates, blocked = _breaker_candidate_decisions(tmp_path, root)
    assert candidates == []
    (entry,) = blocked
    assert entry.state_evidence["decision"] == BREAKER_DECISION
    assert entry.state_evidence["journal_predecessor_identity"]["occurrences"] == 2
    assert "operator_reentry_confirmation" not in entry.state_evidence


# ---------------------------------------------------------------------------
# D.3 accessor
# ---------------------------------------------------------------------------


def _insert_confirmation(repository: FileOrchestrationJournalRepository, **details: Any) -> None:
    repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id="gfs_2026052100",
        event_type=CONFIRMATION_EVENT_TYPE,
        status_from=None,
        status_to="confirmed",
        details=details,
    )


def test_accessor_filters_exactly_by_model_and_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch, model_ids=("model_a", "model_b"))
    repository = FileOrchestrationJournalRepository(root)
    _insert_confirmation(repository, model_id="model_a", decision=BREAKER_DECISION, pin=1, request_id="a-breaker")
    _insert_confirmation(repository, model_id="model_b", decision=BREAKER_DECISION, pin=1, request_id="b-breaker")
    _insert_confirmation(repository, model_id="model_a", decision=BUDGET_DECISION, pin=3, request_id="a-budget")
    # Same model and decision, but a different event type: not a confirmation.
    repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id="gfs_2026052100",
        event_type="retry",
        status_from=None,
        status_to="confirmed",
        details={"model_id": "model_a", "decision": BREAKER_DECISION, "pin": 1, "request_id": "not-a-confirmation"},
    )

    assert [item["request_id"] for item in _confirmations(root)] == ["a-breaker"]
    assert [item["request_id"] for item in _confirmations(root, model_id="model_b")] == ["b-breaker"]
    assert [item["request_id"] for item in _confirmations(root, decision=BUDGET_DECISION)] == ["a-budget"]
    assert _confirmations(root, model_id="model_c") == []


def test_accessor_reads_through_terminal_forecast_cycle_status_and_past_the_event_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 + bounded read: the model-scoped view would drop these events; the accessor must not."""

    from services.orchestrator.scheduler_state_types import DEFAULT_CANDIDATE_STATE_EVENT_LIMIT

    root = seed_breaker_journal(tmp_path, monkeypatch)
    repository = FileOrchestrationJournalRepository(root)
    _insert_confirmation(repository, model_id="model_a", decision=BREAKER_DECISION, pin=1, request_id="first")
    for index in range(DEFAULT_CANDIDATE_STATE_EVENT_LIMIT + 5):
        repository.insert_pipeline_event(
            entity_type="forecast_cycle",
            entity_id="gfs_2026052100",
            event_type="status_note",
            status_from=None,
            status_to="noted",
            details={"index": index},
        )
    _insert_confirmation(repository, model_id="model_a", decision=BREAKER_DECISION, pin=1, request_id="second")

    for status in ("complete", "published"):
        repository.update_forecast_cycle_status(source_id="gfs", cycle_time=_dt(BREAKER_CYCLE), status=status)
        # Ordered oldest first, so a consumer can take the newest.
        assert [item["request_id"] for item in _confirmations(root)] == ["first", "second"]


def test_accessor_returns_empty_on_an_unreadable_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = seed_breaker_journal(tmp_path, monkeypatch)
    repository = FileOrchestrationJournalRepository(root)
    _insert_confirmation(repository, model_id="model_a", decision=BREAKER_DECISION, pin=1, request_id="present")
    assert len(_confirmations(root)) == 1
    # The forecast_cycle event lives in the cycle's journal log.
    (root / "journal" / "gfs" / "2026052100.jsonl").write_text("{not json\n", encoding="utf-8")

    assert _confirmations(root) == []
    assert (
        FileOrchestrationJournalRepository(root).operator_reentry_confirmations(
            source_id="not a source/..",
            cycle_time=_dt(BREAKER_CYCLE),
            model_id="model_a",
            decision=BREAKER_DECISION,
        )
        == []
    )


# ---------------------------------------------------------------------------
# D.7 repository without the accessor
# ---------------------------------------------------------------------------


class _NoConfirmationAccessorRepository(FileOrchestrationJournalRepository):
    """A repository double lacking the accessor (the DB plane's shape): ``getattr`` yields non-callable."""

    operator_reentry_confirmations = None  # type: ignore[assignment]


class _NoRerunCountAccessorRepository(FileOrchestrationJournalRepository):
    """Confirmations readable, but no live breaker pin to compare them with."""

    quarantine_rerun_count = None  # type: ignore[assignment]


class _UnreadableRerunCountRepository(FileOrchestrationJournalRepository):
    def quarantine_rerun_count(self, **_kwargs: Any) -> int | None:
        return None


class _RaisingRerunCountRepository(FileOrchestrationJournalRepository):
    def quarantine_rerun_count(self, **_kwargs: Any) -> int | None:
        raise RuntimeError("count unavailable")


@pytest.mark.parametrize(
    "repository_class",
    [
        _NoConfirmationAccessorRepository,
        _NoRerunCountAccessorRepository,
        _UnreadableRerunCountRepository,
        _RaisingRerunCountRepository,
    ],
)
def test_repository_without_the_accessor_keeps_the_breaker_fail_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repository_class: type[FileOrchestrationJournalRepository],
) -> None:
    """A confirmation is inert unless BOTH accessors answer: no confirmations, or no readable live rerun count."""

    from tests.test_production_scheduler import FakeProductionOrchestrator

    root = seed_breaker_journal(tmp_path, monkeypatch)
    code, _receipt, _err = run_confirm(breaker_confirm_argv(root), capsys)
    assert code == 0

    orchestrator = FakeProductionOrchestrator()
    result = breaker_scheduler(tmp_path, root, orchestrator, repository=repository_class(root)).run_once()

    assert result.evidence["counts"]["submitted_count"] == 0
    assert orchestrator.calls == []
    released = [
        item
        for item in result.evidence["source_cycles"]
        if item.get("selection_reason") == "journal_predecessor_identity_quarantine_breaker_engaged"
    ]
    assert [item["cycle_time_utc"] for item in released] == [BREAKER_CYCLE]

