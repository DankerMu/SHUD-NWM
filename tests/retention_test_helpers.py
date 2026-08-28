"""Shared retention-test helpers (non-collectible support module)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from services.orchestrator.retention import RetentionConfig

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def _cycle_name(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H")


def _write(root: Path, rel: str, content: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _keys(entries: list[dict]) -> set[str]:
    return {entry["key"] for entry in entries}


def _seed_cycle(
    root: Path,
    cycle_time: datetime,
    *,
    prefixes: tuple[str, ...] = ("raw", "canonical", "forcing"),
    run: bool = False,
) -> dict[str, str]:
    """Seed one cycle's artifacts; returns {kind: object key}."""
    name = _cycle_name(cycle_time)
    keys: dict[str, str] = {}
    for prefix in prefixes:
        _write(root, f"{prefix}/gfs/{name}/payload.nc", b"payload-bytes")
        keys[prefix] = f"{prefix}/gfs/{name}"
    if run:
        _write(root, f"runs/fcst_gfs_{name}_model_a/output/out.nc", b"run-bytes")
        keys["runs"] = f"runs/fcst_gfs_{name}_model_a"
    return keys


def _reasons(entries: list[dict]) -> dict[str, str]:
    return {entry["key"]: entry["reason"] for entry in entries}


EXTRA_CONFIG = RetentionConfig(
    enabled=True,
    dry_run=True,
    retention_days=14,
    extra_roots_enabled=True,
    extra_roots_retention_days=30,
)


def _run_id(cycle_time: datetime) -> str:
    return f"fcst_gfs_{_cycle_name(cycle_time)}_model_a"


def _seed_run_workspace(root: Path, cycle_time: datetime) -> str:
    """Seed a full run workspace under ``<root>/runs``; return its key."""
    run_id = _run_id(cycle_time)
    for rel in (
        "input/manifest.json",
        "output/out.nc",
        "logs/shud.log",
        "state_checkpoint_recovery/checkpoint.json",
    ):
        _write(root, f"runs/{run_id}/{rel}", b"run-bytes")
    return f"runs/{run_id}"


def _entries_for(entries: list[dict], root: Path) -> set[str]:
    resolved = str(root.resolve())
    return {entry["key"] for entry in entries if entry["root"] == resolved}


def _seed_pass_env(monkeypatch) -> None:
    """Enable real additional-root retention for a scheduler pass."""
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", "true")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", raising=False)


def _pass_scheduler(**config_kwargs):
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    config = ProductionSchedulerConfig(dry_run=False, **config_kwargs)
    return ProductionScheduler(
        config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None
    )
