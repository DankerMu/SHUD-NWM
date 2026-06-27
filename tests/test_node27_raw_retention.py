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
