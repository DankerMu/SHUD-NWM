"""Response models for ``apps/api/routes/state_snapshots.py`` (#2348).

Both routes return bare bodies (no ``_ok`` envelope) built by
``packages/common/state_manager._snapshot_to_dict``, whose timestamps are
already ``_format_time`` strings.
"""

from __future__ import annotations

from apps.api.response_models.envelope import OpenModel


class StateSnapshot(OpenModel):
    state_id: str
    model_id: str
    run_id: str
    valid_time: str
    state_uri: str
    checksum: str
    usable_flag: bool
    created_at: str | None
    source_id: str | None
    cycle_id: str | None
    lead_hours: int | None
    model_package_version: str | None
    model_package_checksum: str | None
    original_shud_filename: str | None
    cloned_from_state_id: str | None
    cloned_from_model_id: str | None
    clone_gate_fingerprint: str | None
    clone_gate_kind: str | None


class StateSnapshotPage(OpenModel):
    total_count: int
    items: list[StateSnapshot]
    limit: int
    offset: int
