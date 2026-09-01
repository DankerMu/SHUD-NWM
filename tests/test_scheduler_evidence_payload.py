"""Focused coverage for normal scheduler receipt size compaction."""

from __future__ import annotations

from types import SimpleNamespace

# The production façade establishes the evidence compatibility imports before
# callers reach this leaf module.
from services.orchestrator import scheduler as _scheduler  # noqa: F401
from services.orchestrator import scheduler_evidence_payload as payload_module


def _large_terminal_skip(index: int) -> dict[str, object]:
    return {
        "candidate_id": f"gfs:2026082900:model_{index}",
        "source": "gfs",
        "basin_id": f"basin_{index}",
        "reason": "terminal_hydro_success",
        "state_evidence": {
            "decision": "skip_terminal",
            "pipeline_events": [{"details": "terminal-history-" * 400}],
        },
    }


def test_size_pressure_compacts_terminal_skips_and_retention_without_blocking_pass() -> None:
    active_skip = {
        "candidate_id": "gfs:2026082900:active_model",
        "source": "gfs",
        "basin_id": "active_basin",
        "reason": "active_slurm_job",
        "state_evidence": {"active_jobs": [{"job_id": "42", "details": "active-history-" * 400}]},
    }
    payload = {
        "status": "submitted",
        "skipped_candidates": [*(_large_terminal_skip(index) for index in range(6)), active_skip],
        "retention": {
            "status": "completed",
            "enabled": True,
            "dry_run": True,
            "counts": {"planned": 0, "deleted": 0, "skipped": 48, "failed": 0},
            "skipped": [
                {"key": f"runs/{index}", "root": "/workspace", "reason": "within_retention_window"}
                for index in range(48)
            ],
        },
    }

    compacted, serialized = payload_module._serialized_evidence_within_limit(
        SimpleNamespace(max_evidence_bytes=8_000),
        payload,
        artifact_path=None,
    )

    assert len(serialized.encode("utf-8")) <= 8_000
    assert compacted["status"] == "submitted"
    assert compacted["evidence_compaction"] == {
        "status": "applied",
        "reason": "pre_write_size_pressure",
        "terminal_skipped_candidates_compacted": 6,
        "retention_entry_counts_compacted": {"skipped": 48},
    }
    for row in compacted["skipped_candidates"][:6]:
        assert row["reason"] == "terminal_hydro_success"
        assert "state_evidence" not in row
        assert row["decision"] == "skip_terminal"
    assert compacted["skipped_candidates"][-1] == active_skip
    assert compacted["retention"]["skipped_count"] == 48
    assert "skipped" not in compacted["retention"]


def test_size_pressure_projection_is_not_used_when_full_payload_already_fits() -> None:
    payload = {
        "status": "planned",
        "skipped_candidates": [_large_terminal_skip(1)],
        "retention": {"status": "disabled", "enabled": False},
    }

    written, serialized = payload_module._serialized_evidence_within_limit(
        SimpleNamespace(max_evidence_bytes=100_000),
        payload,
        artifact_path=None,
    )

    assert written is payload
    assert "evidence_compaction" not in written
    assert "state_evidence" in written["skipped_candidates"][0]
    assert len(serialized.encode("utf-8")) < 100_000
