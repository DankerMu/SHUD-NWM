from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import node27_raw_retention


def _write_raw_cycle(root: Path, source: str, cycle: str, *, name: str = "manifest.json") -> Path:
    path = root / "raw" / source / cycle
    path.mkdir(parents=True)
    (path / name).write_text("payload", encoding="utf-8")
    return path


def _config(root: Path) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
    )


def _gated_config(
    root: Path, *, enabled: bool = True, dry_run: bool = False
) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
        enabled=enabled,
        dry_run=dry_run,
    )


def test_node27_raw_retention_production_deletes_aged_targets(tmp_path: Path) -> None:
    old_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    _write_raw_cycle(tmp_path, "IFS", "2026062612")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["status"] == "completed"
    assert result["execution_mode"] == "production_execute"
    assert result["counts"]["planned"] == 1
    assert result["counts"]["deleted"] == 1
    assert result["planned"][0]["key"] == "raw/gfs/2026060100"
    assert not old_cycle.exists()


def test_raw_retention_age_uses_display_watermark_not_wall_clock(tmp_path: Path) -> None:
    eligible = _write_raw_cycle(tmp_path, "gfs", "2026062000")
    protected = _write_raw_cycle(tmp_path, "gfs", "2026063000")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 7, 22, 0, tzinfo=UTC),
        reference_time=datetime(2026, 7, 11, 12, tzinfo=UTC),
    )

    assert result["started_at"] == "2026-07-22T00:00:00Z"
    assert result["reference_time"] == "2026-07-11T12:00:00Z"
    assert result["cutoff"] == "2026-06-27T12:00:00Z"
    assert not eligible.exists()
    assert protected.exists()


def test_node27_raw_retention_execute_deletes_only_aged_enabled_sources(tmp_path: Path) -> None:
    old_gfs = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    fresh_ifs = _write_raw_cycle(tmp_path, "IFS", "2026062612")
    disabled = _write_raw_cycle(tmp_path, "era5", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["counts"]["planned"] == 1
    assert result["counts"]["deleted"] == 1
    assert result["counts"]["failed"] == 0
    assert not old_gfs.exists()
    assert fresh_ifs.exists()
    assert disabled.exists()
    assert any(item["reason"] == "source_not_enabled" for item in result["skipped"])


def test_node27_raw_retention_skips_non_cycle_and_symlink_targets(tmp_path: Path) -> None:
    _write_raw_cycle(tmp_path, "gfs", "not-a-cycle")
    real = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    link = tmp_path / "raw" / "gfs" / "2026050100"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["counts"]["deleted"] == 1
    assert link.is_symlink()
    assert any(item["key"] == "raw/gfs/not-a-cycle" for item in result["skipped"])


def test_node27_raw_retention_preflight_rejects_unsafe_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", "/")
    config, blockers = node27_raw_retention.config_from_env(
        node27_raw_retention.build_parser().parse_args([])
    )

    assert config is None
    assert any(item["reason"] == "path_is_root" for item in blockers)


def test_node27_raw_retention_dry_run_cli_is_removed() -> None:
    with pytest.raises(SystemExit):
        node27_raw_retention.build_parser().parse_args(["--dry-run"])


# ---------------------------------------------------------------------------
# Issue #1407 - env gates (CLI flags stay removed) and anchor disclosure
# ---------------------------------------------------------------------------
def test_default_env_keeps_execute_only_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NODE27_RAW_RETENTION_ENABLED", raising=False)
    monkeypatch.delenv("NODE27_RAW_RETENTION_PLAN_ONLY", raising=False)
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(tmp_path))
    (tmp_path / "raw").mkdir()

    config, blockers = node27_raw_retention.config_from_env(
        node27_raw_retention.build_parser().parse_args([])
    )

    assert blockers == []
    assert config is not None
    assert config.enabled is True
    assert config.dry_run is False


def test_legacy_dry_run_env_name_stays_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover ``NODE27_RAW_RETENTION_DRY_RUN=true`` line must not disable deletion.

    The old variable defaulted to true; reusing the name would let stale node-27
    config silently return production to zero deletions (the failure 9c1625ee
    removed). The new gate is a different name, so the stale line is inert.
    """
    monkeypatch.setenv("NODE27_RAW_RETENTION_DRY_RUN", "true")
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(tmp_path))
    (tmp_path / "raw").mkdir()

    config, _ = node27_raw_retention.config_from_env(
        node27_raw_retention.build_parser().parse_args([])
    )

    assert config is not None
    assert config.dry_run is False


def test_env_gates_are_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE27_RAW_RETENTION_ENABLED", "false")
    monkeypatch.setenv("NODE27_RAW_RETENTION_PLAN_ONLY", "yes")
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(tmp_path))
    (tmp_path / "raw").mkdir()

    config, _ = node27_raw_retention.config_from_env(
        node27_raw_retention.build_parser().parse_args([])
    )

    assert config is not None
    assert config.enabled is False
    assert config.dry_run is True


def test_disabled_gate_yields_disabled_summary_with_zero_deletions(tmp_path: Path) -> None:
    aged = _write_raw_cycle(tmp_path, "gfs", "2026060100")

    result = node27_raw_retention.run_retention(
        _gated_config(tmp_path, enabled=False),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["status"] == "disabled"
    assert result["enabled"] is False
    assert result["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    assert result["freed_bytes"] == 0
    assert aged.exists()


def test_plan_only_gate_collects_targets_without_removing_trees(tmp_path: Path) -> None:
    aged = _write_raw_cycle(tmp_path, "gfs", "2026060100")

    result = node27_raw_retention.run_retention(
        _gated_config(tmp_path, dry_run=True),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["status"] == "completed"
    assert result["dry_run"] is True
    assert result["execution_mode"] == "plan_only"
    assert result["counts"]["planned"] == 1
    assert result["counts"]["deleted"] == 0
    assert result["planned"][0]["key"] == "raw/gfs/2026060100"
    assert result["freed_bytes"] == 0
    assert aged.exists()


def test_summary_discloses_the_watermark_anchor_decision(tmp_path: Path) -> None:
    _write_raw_cycle(tmp_path, "gfs", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
        reference_time=datetime(2026, 6, 27, 6, tzinfo=UTC),
    )

    anchor = result["anchor"]
    assert anchor["mode"] == "display_watermark"
    assert anchor["decision"] == "issue-1407-keep-watermark-anchor"
    assert anchor["residual_risk"] == (
        "backfill cycles older than watermark - retention_days are unprotected"
    )
    assert anchor["reference_time"] == "2026-06-27T06:00:00Z"
    # node-27 cannot reach the node-22 pass receipts, so no frontier bound exists.
    assert anchor["frontier_active_lower_bound"] is None


def test_disabled_summary_also_discloses_the_anchor(tmp_path: Path) -> None:
    result = node27_raw_retention.run_retention(
        _gated_config(tmp_path, enabled=False),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["anchor"]["decision"] == "issue-1407-keep-watermark-anchor"


def test_schema_version_is_v3() -> None:
    assert node27_raw_retention.SCHEMA_VERSION == "nhms.node27_raw_retention.production.v3"


_SYSTEMD_SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "systemd"
    / "nhms-node27-raw-retention.service"
)


def test_raw_retention_service_bootstraps_log_dir() -> None:
    service_text = _SYSTEMD_SERVICE_PATH.read_text(encoding="utf-8")
    assert (
        "ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs"
        in service_text
    )
    assert (
        "StandardOutput=append:/home/nwm/node27-raw-retention-logs/systemd.log"
        in service_text
    )
    lines = service_text.splitlines()
    pre_index = next(
        i for i, line in enumerate(lines) if line.startswith("ExecStartPre=")
    )
    start_index = next(
        i for i, line in enumerate(lines) if line.startswith("ExecStart=")
    )
    assert pre_index < start_index
