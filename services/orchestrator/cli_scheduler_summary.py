"""Scheduler stdout summary helpers for the orchestrator CLI.

The ``NHMS_SCHEDULER_STDOUT_SUMMARY_ONLY`` payload compaction is pure and
self-contained, so it lives here to keep the CLI module under the large-file
guard while its entrypoints keep calling ``_scheduler_stdout_payload`` (the
env-flag read stays in the CLI module, which owns ``_env_bool``).
"""

from __future__ import annotations

from collections.abc import Mapping

_SCHEDULER_STDOUT_SUMMARY_SCALAR_KEYS = (
    "pass_id",
    "status",
    "artifact_path",
    "started_at",
    "finished_at",
    "dry_run",
    "continuous",
    "readiness_interpretation",
    "execution_boundary",
    "scheduler_state_backend",
    "scheduler_registry_backend",
    "scheduler_state_index_backend",
    "scheduler_journal_backend",
)


def _is_json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_small_scalar_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 32
        and all(_is_json_scalar(item) for item in value)
    )


def _scheduler_pass_stdout_summary(payload: Mapping[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {
        key: payload[key]
        for key in _SCHEDULER_STDOUT_SUMMARY_SCALAR_KEYS
        if key in payload and _is_json_scalar(payload[key])
    }
    for key, value in payload.items():
        if key.endswith("_count") and _is_json_scalar(value):
            summary[key] = value
    for key in ("sources", "model_ids", "basin_ids", "selected_cycle_ids"):
        value = payload.get(key)
        if _is_small_scalar_list(value):
            summary[key] = value
    if "status" not in summary:
        summary["status"] = "unknown"
    return summary


def _scheduler_stdout_summary(payload: Mapping[str, object]) -> dict[str, object]:
    passes = payload.get("passes")
    if isinstance(passes, list):
        return {
            "status": payload.get("status", "unknown"),
            "passes": [
                _scheduler_pass_stdout_summary(item)
                for item in passes
                if isinstance(item, Mapping)
            ],
        }
    return _scheduler_pass_stdout_summary(payload)
