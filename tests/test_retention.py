"""Requirement-driven tests for forecast data retention cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.orchestrator.retention import (
    RetentionConfig,
    plan_retention,
    run_retention,
)

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def _cycle_name(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H")


def _write(root: Path, rel: str, content: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Fake object store with old + new cycles, static grid, and published tiles."""
    root = tmp_path / "object-store"
    old = _cycle_name(NOW - timedelta(days=20))
    new = _cycle_name(NOW - timedelta(days=3))
    # raw old + new
    _write(root, f"raw/gfs/{old}/gfs.f000.nc")
    _write(root, f"raw/gfs/{new}/gfs.f000.nc")
    # canonical old + static grid
    _write(root, f"canonical/gfs/{old}/wind/p.nc")
    _write(root, "canonical/gfs/grid/gfs_0p25/grid.json")
    # forcing old
    _write(root, f"forcing/gfs/{old}/basin_v1/model_a/forcing.nc")
    # per-run workspace (old, cycle embedded in run id)
    _write(root, f"runs/fcst_gfs_{old}_model_a/output/out.nc")
    # published tiles (must survive)
    _write(root, f"tiles/hydro/gfs_{old}/q-down/manifest.json")
    return root


def _keys(entries: list[dict]) -> set[str]:
    return {entry["key"] for entry in entries}


def test_old_cycle_selected_new_cycle_retained(store: Path) -> None:
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=True, retention_days=14),
    )
    old = _cycle_name(NOW - timedelta(days=20))
    new = _cycle_name(NOW - timedelta(days=3))
    planned = _keys(result.planned)
    assert f"raw/gfs/{old}" in planned
    assert f"raw/gfs/{new}" not in planned
    skipped = _keys(result.skipped)
    assert f"raw/gfs/{new}" in skipped


def test_published_and_static_assets_retained(store: Path) -> None:
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    # tiles never appear in plan/delete; grid skipped as static asset.
    all_planned = _keys(result.planned)
    assert not any(key.startswith("tiles/") for key in all_planned)
    assert (store / "tiles/hydro").exists()
    assert (store / "canonical/gfs/grid").exists()
    assert "canonical/gfs/grid" in _keys(result.skipped)


def test_unparseable_cycle_name_skipped(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _write(root, "raw/gfs/not-a-cycle/file.nc")
    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=True, retention_days=14),
    )
    assert "raw/gfs/not-a-cycle" not in _keys(result.planned)
    assert "raw/gfs/not-a-cycle" in _keys(result.skipped)
    # never-break: the directory still exists
    assert (root / "raw/gfs/not-a-cycle").exists()


def test_dry_run_plans_but_does_not_delete(store: Path) -> None:
    old = _cycle_name(NOW - timedelta(days=20))
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=True, retention_days=14),
    )
    assert result.planned, "dry-run should still produce a non-empty plan"
    assert not result.deleted
    assert (store / f"raw/gfs/{old}").exists()


def test_execute_deletes_aged_cycles(store: Path) -> None:
    old = _cycle_name(NOW - timedelta(days=20))
    new = _cycle_name(NOW - timedelta(days=3))
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    assert result.deleted
    assert not (store / f"raw/gfs/{old}").exists()
    assert not (store / f"runs/fcst_gfs_{old}_model_a").exists()
    assert (store / f"raw/gfs/{new}").exists()
    assert result.freed_bytes >= 0


def test_single_delete_failure_recorded_others_continue(store: Path, monkeypatch) -> None:
    import services.orchestrator.retention as retention_mod

    old = _cycle_name(NOW - timedelta(days=20))
    failing_key = f"raw/gfs/{old}"
    real_rmtree = retention_mod.shutil.rmtree

    def fake_rmtree(path, *args, **kwargs):
        if Path(path).as_posix().endswith(failing_key):
            raise OSError("boom")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(retention_mod.shutil, "rmtree", fake_rmtree)
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    assert any(entry["key"] == failing_key for entry in result.failed)
    assert (store / failing_key).exists()  # failed one survives
    # others still deleted
    assert not (store / f"forcing/gfs/{old}").exists()


def test_retention_days_configurable(store: Path) -> None:
    new = _cycle_name(NOW - timedelta(days=3))
    # 1-day retention selects the 3-day-old cycle that 14-day retention kept.
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=True, retention_days=1),
    )
    assert f"raw/gfs/{new}" in _keys(result.planned)


def test_disabled_does_not_delete(store: Path) -> None:
    old = _cycle_name(NOW - timedelta(days=20))
    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=RetentionConfig(enabled=False, dry_run=False, retention_days=14),
    )
    assert not result.deleted
    assert (store / f"raw/gfs/{old}").exists()


def test_config_from_env_defaults_safe(monkeypatch) -> None:
    for name in ("NHMS_RETENTION_ENABLED", "NHMS_RETENTION_DRY_RUN", "NHMS_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    config = RetentionConfig.from_env()
    assert config.enabled is False
    assert config.dry_run is True
    assert config.retention_days == 14


def test_plan_retention_handles_missing_root(tmp_path: Path) -> None:
    result = plan_retention(
        object_store_root=tmp_path / "does-not-exist",
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=True,
    )
    assert result.planned == []
    assert result.deleted == []


def test_scheduler_skips_retention_when_disabled(monkeypatch, tmp_path: Path) -> None:
    """NHMS_RETENTION_ENABLED=false => scheduler _run_retention is a no-op."""
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    monkeypatch.delenv("NHMS_RETENTION_ENABLED", raising=False)
    config = ProductionSchedulerConfig(workspace_root=str(tmp_path / "ws"))
    scheduler = ProductionScheduler(
        config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None
    )
    payload = scheduler._run_retention(NOW)
    assert payload["status"] == "disabled"
    assert payload["enabled"] is False


def _make_scheduler(tmp_path: Path):
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    config = ProductionSchedulerConfig(workspace_root=str(tmp_path / "ws"))
    return ProductionScheduler(
        config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None
    )


def test_scheduler_retention_exception_does_not_break_pass(monkeypatch, tmp_path: Path) -> None:
    """[HIGH] retention failure must never abort scheduling: caught, reported as error."""
    import services.orchestrator.scheduler as scheduler_mod

    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")

    def boom(*args, **kwargs):
        raise RuntimeError("retention exploded")

    monkeypatch.setattr(scheduler_mod, "run_retention", boom)
    scheduler = _make_scheduler(tmp_path)

    # (a) must not raise upward
    payload = scheduler._run_retention(NOW)

    # (b) error status, still enabled
    assert payload["status"] == "error"
    assert payload["enabled"] is True
    # (c) carries error info
    assert "retention exploded" in payload["error"]


def test_disabled_noop_leaves_expired_cycle_on_disk(monkeypatch, tmp_path: Path) -> None:
    """[MED] disabled is a physical no-op: an expired cycle dir survives untouched."""
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "false")
    root = tmp_path / "object-store"
    old = _cycle_name(NOW - timedelta(days=30))
    expired = _write(root, f"raw/gfs/{old}/gfs.f000.nc").parent

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=False, dry_run=False, retention_days=14),
    )

    # Disabled => a plan may still be computed, but nothing is deleted.
    assert not result.deleted
    # physical assertion: the expired directory still exists
    assert expired.exists()
    assert (root / f"raw/gfs/{old}/gfs.f000.nc").exists()


def test_published_artifact_root_protected_not_deleted(tmp_path: Path) -> None:
    """[MED] paths under published_artifact_root are protected even if cycle-aged."""
    root = tmp_path / "object-store"
    published = root / "published"
    old = _cycle_name(NOW - timedelta(days=20))
    # An expired cycle that physically lives under the published artifact root.
    protected = _write(root, f"raw/gfs/{old}/gfs.f000.nc").parent
    # Point published root at raw/gfs so the aged cycle resolves under it.
    published_root = root / "raw" / "gfs"

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
        published_artifact_root=published_root,
    )

    assert f"raw/gfs/{old}" not in _keys(result.planned)
    assert f"raw/gfs/{old}" not in _keys(result.deleted)
    # protected_path reason recorded in skipped
    reasons = {(e["key"], e["reason"]) for e in result.skipped}
    assert (f"raw/gfs/{old}", "protected_path") in reasons
    # physical assertion: directory survives
    assert protected.exists()
    assert published.parent.exists()


def test_object_store_root_none_is_empty_noop() -> None:
    """[LOW] object_store_root=None => empty plan, no error, no deletion."""
    plan = plan_retention(
        object_store_root=None,
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=False,
    )
    assert plan.planned == []
    assert plan.deleted == []
    assert plan.skipped == []

    result = run_retention(
        object_store_root=None,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    assert result.planned == []
    assert result.deleted == []


def test_run_without_cycle_token_skipped_not_deleted(tmp_path: Path) -> None:
    """[LOW] a run dir with no parseable %Y%m%d%H token is skipped, not deleted."""
    root = tmp_path / "object-store"
    run_dir = _write(root, "runs/manual_run_x/output/out.nc").parent.parent

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )

    assert "runs/manual_run_x" not in _keys(result.planned)
    reasons = {(e["key"], e["reason"]) for e in result.skipped}
    assert ("runs/manual_run_x", "unparseable_run_cycle") in reasons
    # physical assertion: untouched
    assert run_dir.exists()
    assert (root / "runs/manual_run_x/output/out.nc").exists()


def test_states_prefix_protected_not_deleted(tmp_path: Path) -> None:
    """[LOW] the states/ prefix is always protected even with aged-cycle content."""
    root = tmp_path / "object-store"
    old = _cycle_name(NOW - timedelta(days=20))
    states_dir = _write(root, f"states/{old}/state.nc").parent

    result = run_retention(
        object_store_root=root,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )

    assert not any(key.startswith("states/") for key in _keys(result.planned))
    assert not any(key.startswith("states/") for key in _keys(result.deleted))
    # physical assertion: states content survives
    assert states_dir.exists()
    assert (root / f"states/{old}/state.nc").exists()


def _make_scheduler_with_store(tmp_path: Path, *, dry_run: bool):
    """Build a scheduler bound to a fresh object store + one expired cycle dir."""
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    store = tmp_path / "object-store"
    old = _cycle_name(NOW - timedelta(days=20))
    expired = _write(store, f"raw/gfs/{old}/gfs.f000.nc").parent
    config = ProductionSchedulerConfig(
        workspace_root=str(tmp_path / "ws"),
        object_store_root=str(store),
        dry_run=dry_run,
    )
    scheduler = ProductionScheduler(
        config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None
    )
    return scheduler, expired


def test_scheduler_dry_run_forces_retention_dry_run(monkeypatch, tmp_path: Path) -> None:
    """[HIGH] scheduler dry_run must override env-enabled real deletion.

    With NHMS_RETENTION_ENABLED=true + NHMS_RETENTION_DRY_RUN=false but the
    scheduler pass in dry_run, an expired cycle must survive (planning only)
    and the payload must record the forced downgrade.
    """
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)

    scheduler, expired = _make_scheduler_with_store(tmp_path, dry_run=True)
    payload = scheduler._run_retention(NOW)

    # (1) the expired dir was planned but NOT physically deleted
    assert expired.exists()
    # (2) payload records the forced downgrade and a completed dry-run plan
    assert payload["forced_dry_run_by_scheduler"] is True
    assert payload["status"] == "completed"
    assert payload["dry_run"] is True
    assert not payload["deleted"]
    assert payload["planned"]


def test_scheduler_non_dry_run_follows_env_and_deletes(monkeypatch, tmp_path: Path) -> None:
    """[HIGH] non-dry-run pass + env-enabled deletion => expired dir is deleted."""
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)

    scheduler, expired = _make_scheduler_with_store(tmp_path, dry_run=False)
    payload = scheduler._run_retention(NOW)

    # (1) the expired dir is physically gone
    assert not expired.exists()
    # (2) no forced downgrade flag; real deletion happened
    assert "forced_dry_run_by_scheduler" not in payload
    assert payload["status"] == "completed"
    assert payload["dry_run"] is False
    assert payload["deleted"]


def test_scheduler_dry_run_retention_disabled_still_noop(monkeypatch, tmp_path: Path) -> None:
    """[MED] disabled retention stays a no-op even under a dry-run scheduler pass."""
    monkeypatch.delenv("NHMS_RETENTION_ENABLED", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_DRY_RUN", raising=False)

    scheduler, expired = _make_scheduler_with_store(tmp_path, dry_run=True)
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "disabled"
    assert payload["enabled"] is False
    assert "forced_dry_run_by_scheduler" not in payload
    # physical assertion: nothing removed
    assert expired.exists()
