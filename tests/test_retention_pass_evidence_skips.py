"""Scheduler pass evidence counts within-window retention skips instead of listing them (#3223).

The pass-evidence ``retention`` block keeps every skip whose reason is not
``within_retention_window`` in ``skipped``, adds ``skipped_summary`` (one
``{root, reason, count}`` row per root and reason over ALL skips) and
``skipped_omits``; the standalone ``RetentionResult.to_dict()`` receipt keeps
listing every entry, and the over-limit compaction keeps the summary.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_evidence, scheduler_evidence_payload  # noqa: F401 - load order
from services.orchestrator.retention import RetentionConfig, run_retention
from tests.retention_test_helpers import NOW, _cycle_name, _pass_scheduler, _seed_pass_env, _write

WINDOW = "within_retention_window"
FRONTIER = "pipeline_frontier_exempt"

# A cycle inside the 14-day primary window (and the 30-day additional window).
_FRESH = _cycle_name(NOW - timedelta(days=1))
# A cycle past the 14-day primary cutoff but at the frontier bound below.
_IN_FLIGHT = _cycle_name(NOW - timedelta(days=20))
_BOUND = NOW - timedelta(days=25)


def _seed_skip_only_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Two roots whose every key is skipped (nothing is collectable).

    Object store (primary, 14 d): 4 window keys, 4 frontier-exempt keys and an
    unparseable cycle name. Workspace (additional + Slurm log lane, 30 d): a
    window run workspace, a window gateway cycle directory and an unparseable
    run name.
    """
    store = tmp_path / "object-store"
    workspace = tmp_path / "ws"
    for cycle in (_FRESH, _IN_FLIGHT):
        for prefix in ("raw", "canonical", "forcing"):
            _write(store, f"{prefix}/gfs/{cycle}/payload.nc")
        _write(store, f"runs/fcst_gfs_{cycle}_model_a/output/out.nc")
    _write(store, "raw/gfs/not-a-cycle/payload.nc")
    _write(workspace, f"runs/fcst_gfs_{_IN_FLIGHT}_model_a/output/out.nc")
    _write(workspace, "runs/not_a_run/output/out.nc")
    _write(workspace, f"gfs_{_FRESH}/manifests/forcing_index.json")
    return store.resolve(), workspace.resolve()


def _pass_retention(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict, Path, Path]:
    _seed_pass_env(monkeypatch)
    store, workspace = _seed_skip_only_tree(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", raising=False)
    scheduler = _pass_scheduler(
        db_free_journal_root=tmp_path / "journal", workspace_root=str(workspace), object_store_root=str(store)
    )
    payload = scheduler._run_retention(NOW, active_lower_bound=_BOUND, active_lower_bound_source="candidates")
    assert payload["status"] == "completed"
    return payload, store, workspace


def test_pass_evidence_lists_every_non_window_skip_and_counts_the_window_ones_per_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, store, workspace = _pass_retention(monkeypatch, tmp_path)
    s, w = str(store), str(workspace)

    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 12, "failed": 0}
    assert payload["schema_version"] == "nhms.production_scheduler.retention.v2"
    assert payload["skipped_omits"] == [WINDOW]
    assert sorted((entry["root"], entry["key"], entry["reason"]) for entry in payload["skipped"]) == sorted(
        [
            (s, "raw/gfs/not-a-cycle", "unparseable_cycle_name"),
            (s, f"raw/gfs/{_IN_FLIGHT}", FRONTIER),
            (s, f"canonical/gfs/{_IN_FLIGHT}", FRONTIER),
            (s, f"forcing/gfs/{_IN_FLIGHT}", FRONTIER),
            (s, f"runs/fcst_gfs_{_IN_FLIGHT}_model_a", FRONTIER),
            (w, "runs/not_a_run", "unparseable_run_cycle"),
        ]
    )
    assert sorted((row["root"], row["reason"], row["count"]) for row in payload["skipped_summary"]) == sorted(
        [
            (s, WINDOW, 4),
            (s, FRONTIER, 4),
            (s, "unparseable_cycle_name", 1),
            # the run workspace (``runs/`` lane) and the gateway cycle directory
            # (root-level Slurm log lane) are both inside the 30-day window
            (w, WINDOW, 2),
            (w, "unparseable_run_cycle", 1),
        ]
    )
    assert sum(row["count"] for row in payload["skipped_summary"]) == payload["counts"]["skipped"]
    # the frontier block is still derived from every skip, window or not
    assert payload["frontier"]["protected_count"] == 4


def test_standalone_receipt_of_the_same_plan_still_lists_every_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload, store, workspace = _pass_retention(monkeypatch, tmp_path)

    receipt = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig.from_env(),
        active_lower_bound=_BOUND,
        active_lower_bound_source="candidates",
        runs_only_roots=(str(workspace),),
        slurm_log_root=str(workspace),
    ).to_dict()

    assert len(receipt["skipped"]) == receipt["counts"]["skipped"] == 12
    assert sum(1 for entry in receipt["skipped"] if entry["reason"] == WINDOW) == 6
    assert "skipped_summary" not in receipt
    assert "skipped_omits" not in receipt
    # pass evidence keeps the receipt's non-window entries, in receipt order
    assert payload["skipped"] == [entry for entry in receipt["skipped"] if entry["reason"] != WINDOW]


def test_run_once_pass_evidence_carries_the_projected_retention_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from tests.test_operator_reentry_confirmation import db_free_cold_start_env
    from tests.test_production_scheduler import (
        FakeAdapter,
        FakeForcingProducer,
        FakeProductionOrchestrator,
        FakeRegistry,
        ProductionScheduler,
        _AlwaysReadyCanonicalReadinessProvider,
        _config,
        _model,
    )

    # A real pass: the full DB-free env rooted at ``tmp_path``, over an empty
    # file journal.  Its object store is the seeded ``tmp_path/object-store``;
    # the copyback root the DB-free preflight requires is that same root, so
    # retention sweeps no additional copyback root (``extra_roots`` stays the
    # workspace alone, as without the env).
    journal_root = db_free_cold_start_env(tmp_path, monkeypatch, generated_at=NOW)
    _seed_pass_env(monkeypatch)
    store, workspace = _seed_skip_only_tree(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    orchestrator = FakeProductionOrchestrator()
    scheduler = ProductionScheduler(
        _config(tmp_path / "workspace", db_free=True, now=NOW, object_store_root=store),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-06-03T06:00:00Z", True)])},
        active_repository=FileOrchestrationJournalRepository(journal_root),
        canonical_readiness_provider=_AlwaysReadyCanonicalReadinessProvider(),
        forcing_producer=FakeForcingProducer(),
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()
    persisted = json.loads(Path(result.artifact_path or "").read_text(encoding="utf-8"))

    for evidence in (result.evidence, persisted):
        retention = evidence["retention"]
        assert retention["status"] == "completed"
        assert retention["skipped_omits"] == [WINDOW]
        assert [entry for entry in retention["skipped"] if entry["reason"] == WINDOW] == []
        assert {entry["reason"] for entry in retention["skipped"]} >= {
            "unparseable_cycle_name",
            "unparseable_run_cycle",
        }
        window_rows = {row["root"]: row["count"] for row in retention["skipped_summary"] if row["reason"] == WINDOW}
        assert window_rows[str(store)] >= 4
        assert window_rows[str(workspace)] >= 2
        assert sum(row["count"] for row in retention["skipped_summary"]) == retention["counts"]["skipped"]


def _payload_with(retention: dict) -> dict:
    return {"status": "planned", "retention": json.loads(json.dumps(retention))}


def test_over_limit_compaction_keeps_the_skip_summary_and_omitted_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    retention, _store, _workspace = _pass_retention(monkeypatch, tmp_path)
    listed = len(retention["skipped"])
    assert listed == 6, "compaction only rewrites a retention block that still lists entries"
    summary = [dict(row) for row in retention["skipped_summary"]]

    compacted, serialized = scheduler_evidence_payload._serialized_evidence_within_limit(
        SimpleNamespace(max_evidence_bytes=3_000),
        _payload_with(retention),
        artifact_path=Path("scheduler_evidence.json"),
    )
    bounded = scheduler_module._bounded_evidence_payload(
        _payload_with(retention),
        reason="evidence_size_limit_exceeded",
        max_evidence_bytes=2_800,
    )

    assert len(serialized.encode("utf-8")) <= 3_000
    assert compacted["evidence_compaction"]["retention_entry_counts_compacted"] == {"skipped": listed}
    for block in (compacted["retention"], bounded["retention"]):
        assert "skipped" not in block
        assert block["skipped_count"] == listed
        assert block["counts"]["skipped"] == 12
        assert block["skipped_summary"] == summary
        assert block["skipped_omits"] == [WINDOW]
