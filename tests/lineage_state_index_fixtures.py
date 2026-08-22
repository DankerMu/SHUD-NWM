"""Shared file state-snapshot-index builders for the #1735 lineage suites.

Consumed by :file:`tests/test_scheduler_lineage.py` (resolver semantics),
:file:`tests/test_scheduler_backfill.py` (completion scoping),
:file:`tests/test_scheduler_generation.py` and
:file:`tests/test_state_manager_generation_history.py` (the untouched
history-existence signal).  Extracted into its own non-suite module (house
style: :file:`tests/state_clone_recalibration_fixtures.py`) so every lineage
suite publishes REAL index entries through the production
``publish_state_snapshot_index`` write path instead of hand-rolling four
drifting copies — and so no suite has to import a sibling test module just to
reach a builder.

Entries are built the way the state-clone hook writes them: clone provenance
(``cloned_from_model_id`` / ``clone_gate_fingerprint`` / ``clone_gate_kind``)
rides as pass-through fields, which is exactly what
``_normalize_state_index_entry``'s ``{**row, ...}`` preserves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    publish_state_snapshot_index,
)

#: Package checksum shared by every entry these builders publish.
PACKAGE_CHECKSUM = "c" * 64

DEFAULT_CLONE_GATE_FINGERPRINT = "sha256:" + "d" * 64


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def valid_ic_bytes(seed: bytes) -> bytes:
    """A minimal well-formed ``cfg.ic`` body so object verification passes."""

    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [
        f"2\t1\t{minute:.6f}",
        "1\t0.1\t0.1\t0.1\t0.1\t0.1",
        "2\t0.1\t0.1\t0.1\t0.1\t0.1",
        "1\t0.5",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def index_entry(
    *,
    object_root: Path,
    model_id: str,
    valid_time: str,
    source_id: str = "gfs",
    cloned_from_model_id: str | None = None,
    clone_gate_kind: str | None = "state_compatibility",
    clone_gate_fingerprint: str | None = DEFAULT_CLONE_GATE_FINGERPRINT,
    created_at: str | None = None,
    lead_hours: int = 12,
    state_id: str | None = None,
    usable_flag: bool = True,
) -> dict[str, Any]:
    """One index row; a clone row when ``cloned_from_model_id`` is given.

    ``usable_flag`` defaults to how the clone hook writes a fresh row, but is
    settable because a clone row is MUTABLE after birth: ``run_qc`` and
    ``mark_init_state_corrupted`` flip it to ``False`` on any ``state_id``,
    and the clone row is exactly the init state the first post-cutover cycle
    warm-starts from.  The lineage resolvers deliberately do NOT filter on it
    (#1735), so the db-free plane must be able to express the case.
    """

    stamp = valid_time.replace("-", "").replace(":", "").replace("T", "")[:10]
    resolved_state_id = state_id or f"state_{source_id}_{model_id}_{stamp}"
    content = valid_ic_bytes(resolved_state_id.encode("utf-8"))
    store = LocalObjectStore(object_root, "s3://nhms")
    state_uri = store.write_bytes_atomic(
        f"states/{source_id}/{model_id}/{stamp}/{resolved_state_id}.cfg.ic",
        content,
    )
    producer_cycle_time = parse_utc(valid_time).timestamp() - lead_hours * 3600
    producer_cycle_id = (
        f"{source_id}_"
        + datetime.fromtimestamp(producer_cycle_time, tz=UTC).strftime("%Y%m%d%H")
    )
    entry: dict[str, Any] = {
        "state_id": resolved_state_id,
        "model_id": model_id,
        "run_id": f"analysis_{producer_cycle_id}_{model_id}",
        "source_id": source_id,
        "valid_time": valid_time,
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": bool(usable_flag),
        "cycle_id": producer_cycle_id,
        "lead_hours": lead_hours,
        "model_package_version": f"s3://nhms/models/{model_id}/package/",
        "model_package_checksum": PACKAGE_CHECKSUM,
    }
    if created_at is not None:
        entry["created_at"] = created_at
    if cloned_from_model_id is not None:
        entry["cloned_from_model_id"] = cloned_from_model_id
        entry["cloned_from_state_id"] = f"state_of_{cloned_from_model_id}"
        entry["clone_gate_fingerprint"] = clone_gate_fingerprint
        entry["clone_gate_kind"] = clone_gate_kind
    return entry


def index_repository(
    root: Path,
    entries: list[dict[str, Any]],
    *,
    generated_at: str = "2026-08-22T00:00:00Z",
    now: str = "2026-08-22T06:00:00Z",
) -> FileStateSnapshotIndexRepository:
    """Publish ``entries`` under ``root`` and return a reader over them."""

    root.mkdir(parents=True, exist_ok=True)
    object_root = root / "objects"
    object_root.mkdir(parents=True, exist_ok=True)
    index_path = root / "state-index.json"
    publish_state_snapshot_index(
        entries,
        index_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=parse_utc(generated_at),
    )
    return FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=parse_utc(now),
    )
