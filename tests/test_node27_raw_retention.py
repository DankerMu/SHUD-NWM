from __future__ import annotations

import dataclasses
import errno
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_raw_retention


def _write_raw_cycle(root: Path, source: str, cycle: str, *, name: str = "manifest.json") -> Path:
    path = root / "raw" / source / cycle
    path.mkdir(parents=True)
    (path / name).write_text("payload", encoding="utf-8")
    return path


def _write_canonical_cycle(root: Path, storage_source: str, cycle: str) -> Path:
    """One mirrored precipitation cycle in the live layout (`services/precip/mirror.py`)."""
    path = root / "canonical" / storage_source / cycle / "prcp_rate_or_amount"
    path.mkdir(parents=True)
    (path / f"{storage_source.lower()}_{cycle}_f003.nc").write_text("slice", encoding="utf-8")
    return path.parent


def _write_canonical_grid(root: Path, storage_source: str, grid_id: str, *, mtime: float | None = None) -> Path:
    """A grid definition, which retention must never touch at any age."""
    path = root / "canonical" / storage_source / "grid" / grid_id / "grid.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"grid_id": "%s"}' % grid_id, encoding="utf-8")
    if mtime is not None:
        for target in (path, path.parent, path.parent.parent):
            os.utime(target, (mtime, mtime))
    return path


def _write_cache_cycle(cache_root: Path, storage_source: str, cycle: str) -> Path:
    """One rendered-PNG cache directory (`services/precip/cache.py::cache_file_path`)."""
    path = cache_root / "precip" / storage_source / cycle
    path.mkdir(parents=True)
    (path / "2026-06-01T03:00:00Z.cma24h6-abcdef01.0123456789ab.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def _config(
    root: Path, *, precip_cache_root: Path | None = None
) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
        precip_cache_root=precip_cache_root,
    )


def _gated_config(
    root: Path,
    *,
    enabled: bool = True,
    dry_run: bool = False,
    precip_cache_root: Path | None = None,
) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
        enabled=enabled,
        dry_run=dry_run,
        precip_cache_root=precip_cache_root,
    )


def _keys(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["key"]) for entry in entries]


def _reasons(entries: list[dict[str, Any]]) -> set[str]:
    return {str(entry["reason"]) for entry in entries}


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


def test_schema_version_is_v5_and_summary_discloses_both_new_roots(tmp_path: Path) -> None:
    """v4: `deleted[]` may hold canonical/PNG-cache dirs; v5: typed `lock_failure` entries."""
    assert node27_raw_retention.SCHEMA_VERSION == "nhms.node27_raw_retention.production.v5"

    (tmp_path / "raw").mkdir()
    unconfigured = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )
    configured = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=tmp_path / "cache"),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert unconfigured["schema_version"] == "nhms.node27_raw_retention.production.v5"
    assert unconfigured["canonical_root"] == str(tmp_path / "canonical")
    assert unconfigured["precip_cache_root"] is None
    assert configured["precip_cache_root"] == str(tmp_path / "cache")
    # The `disabled` payload spreads the same base, so both keys appear there too.
    disabled = node27_raw_retention.run_retention(
        _gated_config(tmp_path, enabled=False, precip_cache_root=tmp_path / "cache"),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )
    assert disabled["canonical_root"] == str(tmp_path / "canonical")
    assert disabled["precip_cache_root"] == str(tmp_path / "cache")


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


# ---------------------------------------------------------------------------
# Issue #2011 - canonical mirror lane and precipitation PNG cache lane
#
# Governing invariant: a cycle's mirror directory and its PNG cache directory
# live and die together -- `canonical/<S>/<K>` and `<cache>/precip/<S>/<K>` are
# enumerated and deleted in the SAME run on the SAME cutoff, `<S>` is always a
# `normalize_source_id` product, and neither `canonical/<S>/grid/**` nor
# anything under the cache root outside `precip/` is ever a target.
# ---------------------------------------------------------------------------
def test_canonical_and_raw_cycles_of_one_source_are_pruned_in_one_run(tmp_path: Path) -> None:
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "gfs", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert _keys(result["planned"]) == ["raw/gfs/2026060100", "canonical/gfs/2026060100"]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "canonical/gfs/2026060100"]
    assert [item["reason"] for item in result["planned"]] == [
        "raw_cycle_aged_out",
        "canonical_cycle_aged_out",
    ]
    assert result["counts"]["failed"] == 0
    assert not raw_cycle.exists()
    assert not canonical_cycle.exists()


def test_configured_lowercase_ifs_prunes_the_uppercase_canonical_and_cache_directories(
    tmp_path: Path,
) -> None:
    """`ifs` in the env file must address `IFS` in both new lanes, never `ifs`."""
    cache = tmp_path / "cache"
    raw_cycle = _write_raw_cycle(tmp_path, "ifs", "2026083012")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026083012")
    cache_cycle = _write_cache_cycle(cache, "IFS", "2026083012")

    targets, _ = node27_raw_retention.collect_targets(
        _config(tmp_path, precip_cache_root=cache), now=datetime(2026, 9, 20, 12, tzinfo=UTC)
    )
    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 9, 20, 12, tzinfo=UTC),
    )

    assert sorted(target.key for target in targets) == [
        "canonical/IFS/2026083012",
        "precip-cache/IFS/2026083012",
        "raw/ifs/2026083012",
    ]
    assert [target.source for target in targets if not target.key.startswith("raw/")] == [
        "IFS",
        "IFS",
    ]
    assert _keys(result["planned"]) == [
        "raw/ifs/2026083012",
        "canonical/IFS/2026083012",
        "precip-cache/IFS/2026083012",
    ]
    assert _keys(result["deleted"]) == _keys(result["planned"])
    for entry in result["planned"] + result["deleted"] + result["skipped"]:
        key = str(entry.get("key"))
        path = str(entry.get("path", ""))
        assert "canonical/ifs/" not in key and "canonical/ifs/" not in path
        assert "precip-cache/ifs/" not in key and "/precip/ifs/" not in path
    assert not raw_cycle.exists()
    assert not canonical_cycle.exists()
    assert not cache_cycle.exists()


def test_grid_definitions_are_never_targets_at_any_age(tmp_path: Path) -> None:
    """Grid definitions cannot be regenerated on node-27, so no age makes them targets."""
    ancient = datetime(2001, 1, 1, tzinfo=UTC).timestamp()
    gfs_grid = _write_canonical_grid(tmp_path, "gfs", "gfs_0p25", mtime=ancient)
    ifs_grid = _write_canonical_grid(tmp_path, "IFS", "ifs_0p25", mtime=ancient)
    aged_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert _keys(result["planned"]) == ["canonical/IFS/2026060100"]
    for entry in result["planned"] + result["deleted"]:
        assert "grid" not in str(entry["key"]).split("/")
    assert {"canonical/gfs/grid", "canonical/IFS/grid"} <= set(_keys(result["skipped"]))
    assert "grid_definitions_preserved" in _reasons(result["skipped"])
    assert gfs_grid.exists() and gfs_grid.read_text(encoding="utf-8")
    assert ifs_grid.exists() and ifs_grid.read_text(encoding="utf-8")
    assert not aged_cycle.exists()


def test_cache_root_siblings_outside_precip_are_never_targets(tmp_path: Path) -> None:
    """The MVT tile cache shares the cache root; the lane only descends into `precip/`."""
    cache = tmp_path / "cache"
    ancient = datetime(2001, 1, 1, tzinfo=UTC).timestamp()
    siblings = [
        cache / "mvt" / "IFS" / "2026060100",
        cache / "mvt" / "river-network-national" / "6" / "50",
        cache / "2026060100",
    ]
    for sibling in siblings:
        sibling.mkdir(parents=True)
        (sibling / "payload.bin").write_bytes(b"tile")
        os.utime(sibling, (ancient, ancient))
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert _keys(result["planned"]) == ["precip-cache/IFS/2026060100"]
    for entry in result["planned"] + result["deleted"]:
        assert str(entry["path"]).startswith(str(cache / "precip") + "/")
    for sibling in siblings:
        assert sibling.exists()
        assert (sibling / "payload.bin").exists()
    assert not aged_cache.exists()


def test_cache_and_canonical_cycle_are_pruned_together_and_a_fresh_pair_survives(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    aged_canonical = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    fresh_canonical = _write_canonical_cycle(tmp_path, "IFS", "2026062612")
    fresh_cache = _write_cache_cycle(cache, "IFS", "2026062612")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert _keys(result["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert [item["reason"] for item in result["deleted"]] == [
        "canonical_cycle_aged_out",
        "precip_cache_aged_out",
    ]
    assert not aged_canonical.exists()
    assert not aged_cache.exists()
    assert fresh_canonical.exists()
    assert fresh_cache.exists()
    within = {
        entry["key"]
        for entry in result["skipped"]
        if entry["reason"] == "within_retention_window"
    }
    assert within == {"canonical/IFS/2026062612", "precip-cache/IFS/2026062612"}


def test_an_aged_orphan_cache_cycle_is_still_pruned(tmp_path: Path) -> None:
    """Name-for-name pruning is what makes "no cache without a mirror" eventually true."""
    cache = tmp_path / "cache"
    (tmp_path / "canonical" / "IFS").mkdir(parents=True)
    orphan = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert _keys(result["deleted"]) == ["precip-cache/IFS/2026060100"]
    assert not orphan.exists()


def test_a_within_window_orphan_cache_cycle_is_left_untouched(tmp_path: Path) -> None:
    """Known limit pinned on purpose: this run implements no orphan sweep."""
    cache = tmp_path / "cache"
    (tmp_path / "canonical" / "IFS").mkdir(parents=True)
    orphan = _write_cache_cycle(cache, "IFS", "2026062612")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["planned"] == []
    assert result["deleted"] == []
    assert orphan.exists()
    assert {"key": "precip-cache/IFS/2026062612", "reason": "within_retention_window"} in result[
        "skipped"
    ]


def test_unconfigured_cache_root_skips_only_the_cache_lane(tmp_path: Path) -> None:
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=None),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert {"key": "precip-cache", "reason": "precip_cache_root_unconfigured"} in result["skipped"]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "canonical/IFS/2026060100"]
    assert not raw_cycle.exists()
    assert not canonical_cycle.exists()


def test_a_cache_root_without_a_precip_subdirectory_skips_only_the_cache_lane(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    (cache / "mvt").mkdir(parents=True)
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    missing = [entry for entry in result["skipped"] if entry["reason"] == "precip_cache_root_missing"]
    assert missing == [
        {
            "key": "precip-cache",
            "reason": "precip_cache_root_missing",
            "path": str(cache / "precip"),
        }
    ]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "canonical/IFS/2026060100"]
    assert not raw_cycle.exists()
    assert not canonical_cycle.exists()


def test_a_missing_canonical_root_skips_only_the_canonical_lane(tmp_path: Path) -> None:
    """The shape of every pre-#2011 tmp root: only `raw/` exists."""
    cache = tmp_path / "cache"
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert {
        "key": "canonical",
        "reason": "canonical_root_missing",
        "path": str(tmp_path / "canonical"),
    } in result["skipped"]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    assert not raw_cycle.exists()
    assert not aged_cache.exists()


def test_an_unsafe_lane_root_skips_only_that_lane(tmp_path: Path) -> None:
    """A symlinked lane root is a blocker-grade skip for its own lane only."""
    cache = tmp_path / "cache"
    elsewhere = tmp_path / "elsewhere"
    _write_canonical_cycle(elsewhere, "IFS", "2026060100")
    try:
        (tmp_path / "canonical").symlink_to(elsewhere / "canonical", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert {
        "key": "canonical",
        "reason": "canonical_root_unsafe",
        "path": str(tmp_path / "canonical"),
        "detail": "path_is_symlink",
    } in result["skipped"]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    assert (elsewhere / "canonical" / "IFS" / "2026060100").exists()
    assert not raw_cycle.exists()
    assert not aged_cache.exists()


def test_a_missing_raw_root_still_prunes_the_canonical_and_cache_lanes(tmp_path: Path) -> None:
    """The mirror-root shape of the joint route test: no `raw/` at all."""
    cache = tmp_path / "cache"
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert {
        "key": "raw",
        "reason": "raw_root_missing",
        "path": str(tmp_path / "raw"),
    } in result["skipped"]
    assert _keys(result["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert not canonical_cycle.exists()
    assert not aged_cache.exists()


def test_an_unmappable_configured_source_is_skipped_not_fatal(tmp_path: Path) -> None:
    """`_split_sources` takes free text; `normalize_source_id` rejects it per source."""
    cache = tmp_path / "cache"
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    bogus_canonical = tmp_path / "canonical" / "bogus" / "2026060100"
    bogus_canonical.mkdir(parents=True)
    config = node27_raw_retention.RawRetentionConfig(
        object_store_root=tmp_path,
        retention_days=14,
        sources=frozenset({"ifs", "bogus"}),
        summary_path=None,
        precip_cache_root=cache,
    )

    result = node27_raw_retention.run_retention(
        config, now=datetime(2026, 6, 27, 12, tzinfo=UTC)
    )

    unmappable = [entry for entry in result["skipped"] if entry["reason"] == "source_unmappable"]
    assert sorted(_keys(unmappable)) == ["canonical/bogus", "precip-cache/bogus"]
    assert _keys(result["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert result["counts"]["failed"] == 0
    assert not canonical_cycle.exists()
    assert not aged_cache.exists()
    assert bogus_canonical.exists()


@pytest.mark.parametrize(
    ("enabled", "dry_run", "status", "execution_mode"),
    [
        (False, False, "disabled", "disabled"),
        (True, True, "completed", "plan_only"),
    ],
)
def test_the_env_gates_stop_all_three_lanes(
    tmp_path: Path, enabled: bool, dry_run: bool, status: str, execution_mode: str
) -> None:
    cache = tmp_path / "cache"
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")

    result = node27_raw_retention.run_retention(
        _gated_config(tmp_path, enabled=enabled, dry_run=dry_run, precip_cache_root=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["status"] == status
    assert result["execution_mode"] == execution_mode
    assert result["counts"]["deleted"] == 0
    assert result["deleted"] == []
    assert result["freed_bytes"] == 0
    assert raw_cycle.exists()
    assert canonical_cycle.exists()
    assert aged_cache.exists()


def test_an_undeletable_canonical_target_fails_without_stopping_the_other_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `canonical/<S>/` this account cannot write: one failed target, two live lanes.

    Since #2100 this is an incident shape, not the production steady state --
    the mirror is `2775` with a group this account is in, so the canonical lane
    deletes. It survives as the fail-closed case the rollout deliberately keeps
    reachable (an unswept storage source, or a producer mode regression): the
    obligation is a stable, distinguishable failure that stops neither the raw
    nor the precip-cache lane.

    Which step denies is not the same here as in production, and the trailing
    assertion pins the difference: with only `canonical/IFS` at `0o555` the
    `.nc` is still unlinked and `prcp_rate_or_amount/` still `rmdir`'ed, and
    only the parent-owned `rmdir <cycle>/` fails, so the cycle directory
    survives with bytes gone. That is a different on-disk sequence, kept here as
    is; the unswept-source shape the delta spec promises -- denied at the FIRST
    `unlink` with zero bytes removed -- is
    `test_an_unswept_canonical_source_denies_the_first_unlink_and_removes_nothing`.
    Both reach the receipt as one `PermissionError` entry in `failed[]`.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes, so the failure cannot be simulated")
    cache = tmp_path / "cache"
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    for name in (
        "NODE27_RAW_RETENTION_ENABLED",
        "NODE27_RAW_RETENTION_PLAN_ONLY",
        "NODE27_RAW_RETENTION_SUMMARY_PATH",
        "NODE27_RAW_RETENTION_DAYS",
        "NODE27_RAW_RETENTION_SOURCES",
        "NODE27_RAW_RETENTION_LANES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(cache))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(tmp_path))
    unwritable_parent = tmp_path / "canonical" / "IFS"
    unwritable_parent.chmod(0o555)
    try:
        exit_code = node27_raw_retention.main(
            ["--sources", "gfs,ifs", "--reference-time", "2026-06-27T12:00:00Z"]
        )
        payload = json.loads(capsys.readouterr().out)
    finally:
        unwritable_parent.chmod(0o755)

    assert exit_code == 1
    assert payload["counts"]["failed"] == 1
    failure = payload["failed"][0]
    assert failure["key"] == "canonical/IFS/2026060100"
    assert failure["error_type"] == "PermissionError"
    assert failure["error"]
    assert failure["reason"] == "canonical_cycle_aged_out"
    assert _keys(payload["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    assert "canonical/IFS/2026060100" not in _keys(payload["deleted"])
    assert not raw_cycle.exists()
    assert not aged_cache.exists()
    # rmtree removes the children it can before the parent-owned rmdir fails.
    assert canonical_cycle.exists()


def test_an_unswept_canonical_source_denies_the_first_unlink_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production unswept-source shape: denied at the FIRST `unlink`, zero bytes gone.

    #2100 promises that a storage source added to `NODE27_RAW_RETENTION_SOURCES`
    before its `canonical/<S>` sweep fails closed rather than half-deleting. The
    sibling test above denies one step later (only `canonical/<S>/` is
    unwritable, so the `.nc` is already gone when the `rmdir` fails); here
    `prcp_rate_or_amount/` itself is not writable, which is what an unswept tree
    looks like, and `shutil.rmtree` with no `onerror` re-raises on that first
    `unlink`. The file, its bytes, and both directories must all survive, and
    the cycle's size must not appear in the top-level `freed_bytes`
    (`counts` carries planned/deleted/skipped/failed only -- no byte total).

    Mode half only. The production denial is mode AND gid (`0644`/`0755` under
    gid 1078 vs the runner's 1107); an unprivileged test cannot chgrp, so the
    gid half is not reproducible here and is not asserted.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes, so the failure cannot be simulated")
    cache = tmp_path / "cache"
    raw_cycle = _write_raw_cycle(tmp_path, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(tmp_path, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    unswept_products = canonical_cycle / "prcp_rate_or_amount"
    product = unswept_products / "ifs_2026060100_f003.nc"
    assert product.is_file()
    for name in (
        "NODE27_RAW_RETENTION_ENABLED",
        "NODE27_RAW_RETENTION_PLAN_ONLY",
        "NODE27_RAW_RETENTION_SUMMARY_PATH",
        "NODE27_RAW_RETENTION_DAYS",
        "NODE27_RAW_RETENTION_SOURCES",
        "NODE27_RAW_RETENTION_LANES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(cache))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(tmp_path))
    # Traversable and writable down to `<cycle>/`, exactly as an unswept tree is:
    # only the directory holding the products denies the unlink.
    unswept_products.chmod(0o555)
    try:
        exit_code = node27_raw_retention.main(
            ["--sources", "gfs,ifs", "--reference-time", "2026-06-27T12:00:00Z"]
        )
        payload = json.loads(capsys.readouterr().out)
    finally:
        unswept_products.chmod(0o755)

    assert exit_code == 1
    assert payload["counts"]["failed"] == 1
    assert len(payload["failed"]) == 1
    failure = payload["failed"][0]
    assert failure["key"] == "canonical/IFS/2026060100"
    assert failure["error_type"] == "PermissionError"
    assert failure["error"]
    assert failure["reason"] == "canonical_cycle_aged_out"
    assert failure["size_bytes"] > 0
    # Nothing was removed: the first `unlink` is the one that was denied.
    assert product.is_file()
    assert product.read_text(encoding="utf-8") == "slice"
    assert unswept_products.is_dir()
    assert canonical_cycle.is_dir()
    # The other two lanes still ran to completion.
    assert _keys(payload["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    assert not raw_cycle.exists()
    assert not aged_cache.exists()
    # Top-level `freed_bytes` counts only what was actually deleted, so the
    # denied cycle's bytes are excluded from it.
    assert payload["freed_bytes"] == sum(int(entry["size_bytes"]) for entry in payload["deleted"])
    assert payload["freed_bytes"] < sum(int(entry["size_bytes"]) for entry in payload["planned"])


def test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-traversable lane-root ancestor retires that lane, never the run.

    On CPython 3.11-3.13 `pathlib` swallows only ENOENT/ENOTDIR/EBADF/ELOOP
    (`_IGNORED_ERRNOS`), so `is_symlink()` on `<object-store>/raw` raises EACCES
    when the object store itself is not traversable by this uid -- the 0750
    `frd_muziyao:nfsdata` shape. Because `collect_targets` completes before the
    first `rmtree`, an escaping error would zero out ALL THREE lanes and skip
    the summary write, leaving the `--summary-path` receipt silently stale from
    the previous tick. That is the invariant the assertions below pin.

    That errno set is version-scoped. From CPython 3.14 `exists`/`is_dir`/
    `is_symlink` route through `os.path.*` and swallow every `OSError`, so the
    unreadable root is reported as `raw_root_missing` rather than
    `raw_root_unsafe` / `path_unavailable`. This test is therefore pinned to the
    repo's 3.11 interpreter (root `.python-version`) and WILL fail under
    `uv run --python 3.14 pytest` for that version reason, not for a defect
    (measured on 3.14.2). The assertions stay exact on purpose: widening them
    to accept either receipt would merge two different meanings and delete the
    invariant. The 3.14 label is a documented limit that #2104 deliberately did
    NOT fix (it is on that change's non-goal list, as it was on #2099's), so no
    open issue promises a different spelling there.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    _write_raw_cycle(store, "gfs", "2026060100")
    _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    for name in (
        "NODE27_RAW_RETENTION_ENABLED",
        "NODE27_RAW_RETENTION_PLAN_ONLY",
        "NODE27_RAW_RETENTION_DAYS",
        "NODE27_RAW_RETENTION_SOURCES",
        "NODE27_RAW_RETENTION_LANES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(cache))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(store))
    monkeypatch.setenv("NODE27_RAW_RETENTION_SUMMARY_PATH", str(summary_path))

    store.chmod(0o000)
    try:
        exit_code = node27_raw_retention.main(
            ["--sources", "gfs,ifs", "--reference-time", "2026-06-27T12:00:00Z"]
        )
        payload = json.loads(capsys.readouterr().out)
    finally:
        store.chmod(0o755)

    assert exit_code == 0
    assert payload["status"] == "completed"
    assert json.loads(summary_path.read_text(encoding="utf-8")) == payload
    skipped = {
        (str(entry["reason"]), str(entry.get("detail")))
        for entry in payload["skipped"]
    }
    assert ("raw_root_unsafe", "path_unavailable") in skipped
    assert ("canonical_root_unsafe", "path_unavailable") in skipped
    # #2104 item 3: the errno the probe saw is additive on those same entries.
    unavailable = [
        entry for entry in payload["skipped"] if entry.get("detail") == "path_unavailable"
    ]
    assert sorted(_keys(unavailable)) == ["canonical", "raw"]
    for entry in unavailable:
        assert entry["error"]
        assert entry["error_type"] == "PermissionError"
    assert _keys(payload["deleted"]) == ["precip-cache/IFS/2026060100"]
    assert payload["counts"]["failed"] == 0
    assert not aged_cache.exists()
    assert (store / "raw" / "gfs" / "2026060100").is_dir()
    assert (store / "canonical" / "IFS" / "2026060100").is_dir()


# ---------------------------------------------------------------------------
# Issue #2104 - lane / source locality past the lane-root gate.
#
# Every probe in `_resolve_lane_root` and `_safe_resolved_dir` stats the lane
# root FROM ITS PARENT, so the gate only ever needs `x` on `<object-store>`.
# The first syscalls that need `x` on the lane root itself live one step later
# (`_collect_mapped_lane`'s source probe and the `is_dir()` comprehension in
# `_iter_dirs`); before #2104 an `EACCES` there escaped `collect_targets`,
# which completes BEFORE the first `rmtree`, so all three lanes ended with zero
# deletions and `main()` never reached `_write_summary`.
#
# These tests are pinned to the repo's 3.11 interpreter exactly like
# `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes`: from
# CPython 3.14 `pathlib` swallows every `OSError` on those predicates, so the
# same on-disk state surfaces as an empty listing there, not as a skip.
# ---------------------------------------------------------------------------
_RETENTION_ENV_EXAMPLE = (
    Path(__file__).resolve().parents[1] / "infra" / "env" / "node27-raw-retention.example"
)


def _production_env(
    monkeypatch: pytest.MonkeyPatch, *, store: Path, cache: Path, summary_path: Path
) -> None:
    """The env of a node-27 production tick: both rollout gates unset, receipt on."""
    for name in (
        "NODE27_RAW_RETENTION_ENABLED",
        "NODE27_RAW_RETENTION_PLAN_ONLY",
        "NODE27_RAW_RETENTION_DAYS",
        "NODE27_RAW_RETENTION_SOURCES",
        "NODE27_RAW_RETENTION_LANES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(cache))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(store))
    monkeypatch.setenv("NODE27_RAW_RETENTION_SUMMARY_PATH", str(summary_path))


def _production_tick(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    exit_code = node27_raw_retention.main(
        ["--sources", "gfs,ifs", "--reference-time", "2026-06-27T12:00:00Z"]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _entries(payload: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    return [entry for entry in payload["skipped"] if str(entry["reason"]) == reason]


def test_an_untraversable_canonical_root_retires_only_that_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`canonical/` itself at 0o000: the lane-root gate passes, the source probe raises.

    This is the #2100 shape -- a mode change on the mirror tree that removes
    this uid's traversal of `canonical/` while `<object-store>` stays 775. The
    obligation is that raw and precip-cache still prune on the SAME tick and the
    receipt is written, i.e. that the failure retires one lane, not the run.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    canonical_root = store / "canonical"
    canonical_root.chmod(0o000)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        canonical_root.chmod(0o755)

    assert exit_code == 0
    assert payload["status"] == "completed"
    assert json.loads(summary_path.read_text(encoding="utf-8")) == payload
    assert _keys(payload["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    unsafe = _entries(payload, "canonical_source_unsafe")
    # Both configured sources are reported: the probe raises before existence
    # is known, so the runner cannot tell which of them has a directory here.
    assert sorted(_keys(unsafe)) == ["canonical/IFS", "canonical/gfs"]
    for entry in unsafe:
        assert entry["detail"] == "path_unavailable"
        assert entry["path"].startswith(str(canonical_root) + "/")
        assert entry["error"]
        assert entry["error_type"] == "PermissionError"
    assert payload["counts"]["failed"] == 0
    assert not raw_cycle.exists()
    assert not aged_cache.exists()
    assert canonical_cycle.exists()


def test_a_readable_but_untraversable_raw_root_retires_only_the_raw_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`raw/` at 0o444: `iterdir()` succeeds and the first `is_dir()` raises EACCES."""
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    raw_root = store / "raw"
    raw_root.chmod(0o444)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        raw_root.chmod(0o755)

    assert exit_code == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == payload
    unsafe = _entries(payload, "raw_root_unsafe")
    assert _keys(unsafe) == ["raw"]
    assert unsafe[0]["detail"] == "path_unavailable"
    assert unsafe[0]["path"] == str(raw_root)
    assert unsafe[0]["error"]
    assert unsafe[0]["error_type"] == "PermissionError"
    # A listing that raised is never reported as an empty raw lane.
    assert _keys(payload["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert payload["counts"]["failed"] == 0
    assert not canonical_cycle.exists()
    assert not aged_cache.exists()
    assert raw_cycle.exists()


def test_an_untraversable_canonical_source_root_retires_only_that_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One source root inside a live lane: `canonical/gfs` still prunes."""
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    gfs_cycle = _write_canonical_cycle(store, "gfs", "2026060100")
    ifs_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    ifs_root = store / "canonical" / "IFS"
    ifs_root.chmod(0o444)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        ifs_root.chmod(0o755)

    assert exit_code == 0
    unsafe = _entries(payload, "canonical_source_unsafe")
    assert _keys(unsafe) == ["canonical/IFS"]
    assert unsafe[0]["detail"] == "path_unavailable"
    assert unsafe[0]["path"] == str(ifs_root)
    assert unsafe[0]["error"]
    assert unsafe[0]["error_type"] == "PermissionError"
    assert _keys(payload["deleted"]) == [
        "raw/gfs/2026060100",
        "canonical/gfs/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert payload["counts"]["failed"] == 0
    assert not raw_cycle.exists()
    assert not gfs_cycle.exists()
    assert not aged_cache.exists()
    assert ifs_cycle.exists()


def test_an_untraversable_raw_source_root_retires_only_that_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The raw lane's per-source caller keys off the ON-DISK name, unlike the mapped lanes."""
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    gfs_root = store / "raw" / "gfs"
    gfs_root.chmod(0o444)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        gfs_root.chmod(0o755)

    assert exit_code == 0
    unsafe = _entries(payload, "raw_source_unsafe")
    assert _keys(unsafe) == ["raw/gfs"]
    assert unsafe[0]["detail"] == "path_unavailable"
    assert unsafe[0]["path"] == str(gfs_root)
    assert unsafe[0]["error"]
    assert unsafe[0]["error_type"] == "PermissionError"
    assert _keys(payload["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert payload["counts"]["failed"] == 0
    assert raw_cycle.exists()
    assert not canonical_cycle.exists()
    assert not aged_cache.exists()


def test_an_unreadable_precip_cache_source_root_retires_only_that_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The third lane's per-source leg: `<cache>/precip/IFS` at 0o444 keeps `gfs` pruning.

    Same `_collect_mapped_lane` code path as the canonical source test, but the
    lane the display API writes -- a PNG cache directory left mode-crippled must
    cost its own source's cycles, not the cache lane and not the run.
    """
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    gfs_cache = _write_cache_cycle(cache, "gfs", "2026060100")
    ifs_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    ifs_cache_root = cache / "precip" / "IFS"
    ifs_cache_root.chmod(0o444)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        ifs_cache_root.chmod(0o755)

    assert exit_code == 0
    unsafe = _entries(payload, "precip_cache_source_unsafe")
    assert _keys(unsafe) == ["precip-cache/IFS"]
    assert unsafe[0]["detail"] == "path_unavailable"
    assert unsafe[0]["path"] == str(ifs_cache_root)
    assert unsafe[0]["error"]
    assert unsafe[0]["error_type"] == "PermissionError"
    assert _keys(payload["deleted"]) == [
        "raw/gfs/2026060100",
        "canonical/IFS/2026060100",
        "precip-cache/gfs/2026060100",
    ]
    assert payload["failed"] == []
    assert payload["counts"]["failed"] == 0
    assert not raw_cycle.exists()
    assert not canonical_cycle.exists()
    assert not gfs_cache.exists()
    assert ifs_cache.exists()


def test_iter_dirs_reports_the_listing_error_instead_of_raising(tmp_path: Path) -> None:
    """`([], error)` and `([], None)` are different answers: unavailable vs empty."""
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "manifest.json").write_text("payload", encoding="utf-8")
    link = parent / "link"
    try:
        link.symlink_to(child, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    readable, no_error = node27_raw_retention._iter_dirs(parent)

    assert readable == [child]
    assert no_error is None

    parent.chmod(0o444)
    try:
        entries, error = node27_raw_retention._iter_dirs(parent)
    finally:
        parent.chmod(0o755)

    assert entries == []
    assert isinstance(error, PermissionError)
    assert error.errno == errno.EACCES


def test_a_stale_lane_root_handle_is_reported_with_its_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ESTALE on an NFS lane root: same local skip, and the errno reaches the receipt.

    `<object-store>` itself going stale is a preflight blocker (rc=2); only a
    LANE root lands in `_resolve_lane_root`. The branch pinned here is that
    function's FORWARDING branch, not its `except OSError`: only `Path.resolve`
    is patched, and the `is_symlink`/`exists`/`is_dir` predicates ahead of it
    run on lstat/stat, so the ESTALE is raised and caught inside
    `_safe_resolved_dir`, which returns a `path_unavailable` blocker whose
    `error`/`error_type` the `resolved is None` branch copies onto the skip
    entry. `_resolve_lane_root`'s own `except OSError` is covered by
    `test_an_unreadable_object_store_ancestor_skips_only_its_two_lanes`, where
    the store at 0o000 makes the first predicate's lstat raise. ESTALE maps to
    no `OSError` subclass, so `error_type` is the base class name -- the field
    is the exception class, never a reason.
    """
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    raw_cycle = _write_raw_cycle(store, "gfs", "2026060100")
    canonical_cycle = _write_canonical_cycle(store, "IFS", "2026060100")
    aged_cache = _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    real_resolve = Path.resolve
    stale = {str(store / "raw"), str(store.resolve() / "raw")}

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if str(self) in stale:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    exit_code, payload = _production_tick(capsys)

    assert exit_code == 0
    assert payload["status"] == "completed"
    unsafe = _entries(payload, "raw_root_unsafe")
    assert _keys(unsafe) == ["raw"]
    assert unsafe[0]["detail"] == "path_unavailable"
    assert "Stale file handle" in unsafe[0]["error"]
    assert unsafe[0]["error_type"] == "OSError"
    assert _keys(payload["deleted"]) == [
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert payload["counts"]["failed"] == 0
    assert raw_cycle.exists()
    assert not canonical_cycle.exists()
    assert not aged_cache.exists()


def _documented_operator_jq_program() -> str:
    """The `jq -e '...'` program as the env example teaches it, not a copy of it."""
    lines = _RETENTION_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = next((index for index, line in enumerate(lines) if "jq -e '" in line), None)
    assert start is not None, f"no `jq -e '` opening marker in {_RETENTION_ENV_EXAMPLE}"
    end = next(
        (
            index
            for index, line in enumerate(lines)
            if index > start and "' \"$(ls -t" in line
        ),
        None,
    )
    assert end is not None, f"no `' \"$(ls -t` closing marker in {_RETENTION_ENV_EXAMPLE}"
    body = [lines[index].lstrip().lstrip("#").strip() for index in range(start + 1, end)]
    return "\n".join(body)


def test_documented_operator_check_goes_red_on_an_unsafe_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A per-source skip leaves the receipt fresh and `failed[]` empty.

    Before #2104 widened clause 4 from `endswith("_root_unsafe")` to
    `endswith("_unsafe")` that state would have turned "found after 26h" (the
    crashed tick tripped the freshness clause) into "never found": fresh file,
    `production_execute`, no failure, and a reason clause 4 did not match.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; it is present on node-27")
    if os.geteuid() == 0:
        pytest.skip("root traverses any directory mode, so the failure cannot be simulated")
    program = _documented_operator_jq_program()
    assert 'endswith("_unsafe")' in program
    assert "_root_unsafe" not in program
    assert "production_execute" in program
    # #2100: the whitelist that let a canonical `PermissionError` pass is gone.
    assert "PermissionError" not in program

    store = tmp_path / "store"
    cache = tmp_path / "cache"
    unsafe_summary = tmp_path / "summaries" / "unsafe.json"
    _write_raw_cycle(store, "gfs", "2026060100")
    _write_canonical_cycle(store, "IFS", "2026060100")
    _write_cache_cycle(cache, "IFS", "2026060100")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=unsafe_summary)
    canonical_root = store / "canonical"
    canonical_root.chmod(0o000)
    try:
        exit_code, payload = _production_tick(capsys)
    finally:
        canonical_root.chmod(0o755)

    assert exit_code == 0
    assert payload["failed"] == []
    assert "canonical_source_unsafe" in _reasons(payload["skipped"])
    unsafe_check = subprocess.run(
        ["jq", "-e", program, str(unsafe_summary)], capture_output=True, text=True
    )
    assert unsafe_check.returncode == 1, unsafe_check.stderr

    healthy_store = tmp_path / "healthy-store"
    healthy_cache = tmp_path / "healthy-cache"
    healthy_summary = tmp_path / "summaries" / "healthy.json"
    _write_raw_cycle(healthy_store, "gfs", "2026060100")
    _write_canonical_cycle(healthy_store, "IFS", "2026060100")
    _write_cache_cycle(healthy_cache, "IFS", "2026060100")
    _production_env(
        monkeypatch, store=healthy_store, cache=healthy_cache, summary_path=healthy_summary
    )

    healthy_exit, healthy_payload = _production_tick(capsys)

    assert healthy_exit == 0
    assert healthy_payload["execution_mode"] == "production_execute"
    assert healthy_payload["failed"] == []
    assert [
        entry for entry in healthy_payload["skipped"] if str(entry["reason"]).endswith("_unsafe")
    ] == []
    healthy_check = subprocess.run(
        ["jq", "-e", program, str(healthy_summary)], capture_output=True, text=True
    )
    assert healthy_check.returncode == 0, healthy_check.stderr

    # (iii) #2100: a fresh `production_execute` tick whose only defect is one
    # canonical `PermissionError` is RED. Before #2100 clause 3 selected those
    # entries out and this same summary exited 0.
    denied_store = tmp_path / "denied-store"
    denied_cache = tmp_path / "denied-cache"
    denied_summary = tmp_path / "summaries" / "denied.json"
    _write_raw_cycle(denied_store, "gfs", "2026060100")
    _write_canonical_cycle(denied_store, "IFS", "2026060100")
    _write_cache_cycle(denied_cache, "IFS", "2026060100")
    _production_env(
        monkeypatch, store=denied_store, cache=denied_cache, summary_path=denied_summary
    )
    # The cycle directory is listed and aged, but its parent denies the `rmdir`.
    # That reproduces the summary SHAPE an unswept storage source (or a producer
    # mode regression) leaves behind -- one canonical `PermissionError` in
    # `failed[]` -- not its on-disk sequence, which denies one step earlier; the
    # production sequence is pinned by
    # `test_an_unswept_canonical_source_denies_the_first_unlink_and_removes_nothing`.
    unwritable_parent = denied_store / "canonical" / "IFS"
    unwritable_parent.chmod(0o555)
    try:
        denied_exit, denied_payload = _production_tick(capsys)
    finally:
        unwritable_parent.chmod(0o755)

    assert denied_exit == 1
    assert denied_payload["execution_mode"] == "production_execute"
    assert [entry["error_type"] for entry in denied_payload["failed"]] == ["PermissionError"]
    assert [
        entry for entry in denied_payload["skipped"] if str(entry["reason"]).endswith("_unsafe")
    ] == []
    denied_check = subprocess.run(
        ["jq", "-e", program, str(denied_summary)], capture_output=True, text=True
    )
    assert denied_check.returncode == 1, denied_check.stderr


# ---------------------------------------------------------------------------
# Issue #2360 - `NODE27_RAW_RETENTION_LANES` lane selection
#
# Contract (openspec `node27-raw-retention`, "A retention run SHALL prune only
# the lanes its operator selected"): unset selects raw, canonical and
# precip-cache exactly as before; a set value names lanes from that vocabulary
# (whitespace-trimmed) or blocks the run at preflight with a `lanes` blocker;
# an unselected lane is ONE `{"key": <lane>, "reason": "lane_not_selected"}`
# skip, its root never probed or listed; the summary carries the sorted lanes.
# ---------------------------------------------------------------------------
_ALL_LANES_SORTED = ["canonical", "precip-cache", "raw"]


def _lanes_config(root: Path, *, lanes: set[str], cache: Path | None) -> node27_raw_retention.RawRetentionConfig:
    return node27_raw_retention.RawRetentionConfig(
        object_store_root=root,
        retention_days=14,
        sources=frozenset({"gfs", "ifs"}),
        summary_path=None,
        precip_cache_root=cache,
        lanes=frozenset(lanes),
    )


def _three_aged_lanes(store: Path, cache: Path) -> dict[str, Path]:
    return {
        "raw": _write_raw_cycle(store, "gfs", "2026060100"),
        "canonical": _write_canonical_cycle(store, "IFS", "2026060100"),
        "precip-cache": _write_cache_cycle(cache, "IFS", "2026060100"),
    }


def _config_for_env(monkeypatch: pytest.MonkeyPatch, store: Path, lanes: str | None) -> Any:
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(store))
    if lanes is None:
        monkeypatch.delenv("NODE27_RAW_RETENTION_LANES", raising=False)
    else:
        monkeypatch.setenv("NODE27_RAW_RETENTION_LANES", lanes)
    return node27_raw_retention.config_from_env(node27_raw_retention.build_parser().parse_args([]))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, {"raw", "canonical", "precip-cache"}),
        ("raw,precip-cache", {"raw", "precip-cache"}),
        (" raw ,  precip-cache ", {"raw", "precip-cache"}),
        ("canonical", {"canonical"}),
        ("canonical,canonical", {"canonical"}),
    ],
)
def test_lanes_env_is_parsed_into_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None, expected: set[str]
) -> None:
    config, blockers = _config_for_env(monkeypatch, tmp_path, value)

    assert blockers == []
    assert config is not None
    assert config.lanes == frozenset(expected)


def test_the_config_dataclass_defaults_to_all_three_lanes(tmp_path: Path) -> None:
    """Keyword constructors elsewhere (the MVT cache suite) keep today's run."""
    assert _config(tmp_path).lanes == frozenset({"raw", "canonical", "precip-cache"})


def test_unset_lanes_selects_every_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec scenario "unset selects every lane"."""
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    _three_aged_lanes(store, cache)
    config, blockers = _config_for_env(monkeypatch, store, None)
    assert blockers == [] and config is not None
    assert config.lanes == frozenset({"raw", "canonical", "precip-cache"})
    config = dataclasses.replace(config, precip_cache_root=cache, sources=frozenset({"gfs", "ifs"}))

    targets, skipped = node27_raw_retention.collect_targets(config, now=datetime(2026, 6, 27, 12, tzinfo=UTC))

    assert [target.key for target in targets] == [
        "raw/gfs/2026060100",
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert "lane_not_selected" not in _reasons(skipped)


def test_unset_lanes_summary_differs_from_the_pre_2360_shape_only_by_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MP3: with the variable unset, plan, deletions, skips and rc are today's.

    The expected values are written out from the pre-#2360 contract (the three
    lanes in raw -> canonical -> precip-cache order, the within-window and
    grid skips); the key set is the v5 summary's plus `lanes` and nothing else.
    """
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    aged = _three_aged_lanes(store, cache)
    fresh = _write_raw_cycle(store, "gfs", "2026062612")
    _write_canonical_grid(store, "IFS", "grid-a")
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)

    exit_code, payload = _production_tick(capsys)

    assert exit_code == 0
    assert set(payload) - {"lanes"} == {
        "schema_version",
        "started_at",
        "reference_time",
        "object_store_root",
        "raw_root",
        "canonical_root",
        "precip_cache_root",
        "sources",
        "retention_days",
        "cutoff",
        "enabled",
        "dry_run",
        "anchor",
        "status",
        "finished_at",
        "execution_mode",
        "counts",
        "planned",
        "deleted",
        "skipped",
        "failed",
        "copyback_lock_failures",
        "freed_bytes",
    }
    assert payload["lanes"] == _ALL_LANES_SORTED
    assert _keys(payload["planned"]) == [
        "raw/gfs/2026060100",
        "canonical/IFS/2026060100",
        "precip-cache/IFS/2026060100",
    ]
    assert _keys(payload["deleted"]) == _keys(payload["planned"])
    assert payload["skipped"] == [
        {"key": "raw/gfs/2026062612", "reason": "within_retention_window"},
        {"key": "canonical/IFS/grid", "reason": "grid_definitions_preserved"},
    ]
    assert payload["counts"] == {"planned": 3, "deleted": 3, "skipped": 2, "failed": 0}
    assert all(not path.exists() for path in aged.values())
    assert fresh.exists()
    assert json.loads(summary_path.read_text(encoding="utf-8")) == payload


def test_the_canonical_unit_prunes_only_canonical(tmp_path: Path) -> None:
    """Spec scenario "the canonical unit prunes only canonical"."""
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    aged = _three_aged_lanes(store, cache)

    result = node27_raw_retention.run_retention(
        _lanes_config(store, lanes={"canonical"}, cache=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["lanes"] == ["canonical"]
    assert _keys(result["planned"]) == ["canonical/IFS/2026060100"]
    assert _keys(result["deleted"]) == ["canonical/IFS/2026060100"]
    assert result["skipped"] == [
        {"key": "raw", "reason": "lane_not_selected"},
        {"key": "precip-cache", "reason": "lane_not_selected"},
    ]
    assert result["failed"] == []
    assert not aged["canonical"].exists()
    assert aged["raw"].exists()
    assert aged["precip-cache"].exists()


def test_the_nwm_unit_lanes_leave_canonical_untouched(tmp_path: Path) -> None:
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    aged = _three_aged_lanes(store, cache)

    result = node27_raw_retention.run_retention(
        _lanes_config(store, lanes={"raw", "precip-cache"}, cache=cache),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert result["lanes"] == ["precip-cache", "raw"]
    assert _keys(result["deleted"]) == ["raw/gfs/2026060100", "precip-cache/IFS/2026060100"]
    assert result["skipped"] == [{"key": "canonical", "reason": "lane_not_selected"}]
    assert aged["canonical"].is_dir()
    assert not aged["raw"].exists()
    assert not aged["precip-cache"].exists()


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("raw,canon", "unknown_lane"),
        ("RAW", "unknown_lane"),
        ("precip_cache", "unknown_lane"),
        ("", "empty"),
        ("   ", "empty"),
        (" , ,", "empty"),
    ],
)
def test_an_unknown_or_empty_lane_selection_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
    reason: str,
) -> None:
    """Spec scenario "an unknown or empty selection fails closed"."""
    store = tmp_path / "store"
    cache = tmp_path / "cache"
    summary_path = tmp_path / "summaries" / "raw-retention.json"
    aged = _three_aged_lanes(store, cache)
    _production_env(monkeypatch, store=store, cache=cache, summary_path=summary_path)
    monkeypatch.setenv("NODE27_RAW_RETENTION_LANES", value)

    exit_code, payload = _production_tick(capsys)

    assert exit_code == 2
    assert payload["status"] == "preflight_blocked"
    lane_blockers = [blocker for blocker in payload["blockers"] if blocker["field"] == "lanes"]
    assert [blocker["reason"] for blocker in lane_blockers] == [reason]
    # The blocked payload has no config, so it carries no `lanes` (design D3).
    assert "lanes" not in payload
    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    assert all(path.is_dir() for path in aged.values())
    assert json.loads(summary_path.read_text(encoding="utf-8")) == payload


def test_an_unselected_lane_with_an_unusable_root_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec scenario "an unselected lane with an unusable root is not probed".

    Both unselected roots are unusable here: `raw` does not exist and the
    precip cache root is unconfigured. Neither may produce its usual skip, and
    no probe may be made on either path.
    """
    store = tmp_path / "store"
    canonical = _write_canonical_cycle(store, "IFS", "2026060100")
    assert not (store / "raw").exists()
    probed: list[Path] = []
    real_resolve = node27_raw_retention._resolve_lane_root
    real_iter_dirs = node27_raw_retention._iter_dirs

    def recording_resolve(root: Path, **kwargs: Any) -> Any:
        probed.append(root)
        return real_resolve(root, **kwargs)

    def recording_iter_dirs(parent: Path) -> Any:
        probed.append(parent)
        return real_iter_dirs(parent)

    monkeypatch.setattr(node27_raw_retention, "_resolve_lane_root", recording_resolve)
    monkeypatch.setattr(node27_raw_retention, "_iter_dirs", recording_iter_dirs)

    result = node27_raw_retention.run_retention(
        _lanes_config(store, lanes={"canonical"}, cache=None),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert "raw_root_missing" not in _reasons(result["skipped"])
    assert "raw_root_unsafe" not in _reasons(result["skipped"])
    assert "precip_cache_root_unconfigured" not in _reasons(result["skipped"])
    assert [entry for entry in result["skipped"] if entry["key"] in {"raw", "precip-cache"}] == [
        {"key": "raw", "reason": "lane_not_selected"},
        {"key": "precip-cache", "reason": "lane_not_selected"},
    ]
    assert not [path for path in probed if path == store / "raw" or store / "raw" in path.parents]
    assert _keys(result["deleted"]) == ["canonical/IFS/2026060100"]
    assert not canonical.exists()


def test_an_unselected_lane_with_an_unsafe_root_is_not_reported_unsafe(tmp_path: Path) -> None:
    """A regular file where the raw root should be is `raw_root_unsafe` today."""
    store = tmp_path / "store"
    _write_canonical_cycle(store, "IFS", "2026060100")
    (store / "raw").write_text("not a directory", encoding="utf-8")

    selected = node27_raw_retention.run_retention(
        _lanes_config(store, lanes={"raw", "canonical"}, cache=None),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )
    unselected = node27_raw_retention.run_retention(
        _lanes_config(store, lanes={"canonical"}, cache=None),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert "raw_root_unsafe" in _reasons(selected["skipped"])
    assert [entry for entry in unselected["skipped"] if entry["key"] == "raw"] == [
        {"key": "raw", "reason": "lane_not_selected"}
    ]


def test_disabled_and_plan_only_summaries_also_carry_the_lanes(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    disabled = node27_raw_retention.run_retention(
        node27_raw_retention.RawRetentionConfig(
            object_store_root=tmp_path,
            retention_days=14,
            sources=frozenset({"gfs"}),
            summary_path=None,
            enabled=False,
            lanes=frozenset({"canonical"}),
        ),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )
    plan_only = node27_raw_retention.run_retention(
        node27_raw_retention.RawRetentionConfig(
            object_store_root=tmp_path,
            retention_days=14,
            sources=frozenset({"gfs"}),
            summary_path=None,
            dry_run=True,
            lanes=frozenset({"raw", "precip-cache"}),
        ),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert disabled["lanes"] == ["canonical"]
    assert plan_only["lanes"] == ["precip-cache", "raw"]
    assert node27_raw_retention.SCHEMA_VERSION == "nhms.node27_raw_retention.production.v5"


def test_documented_operator_check_covers_both_units_and_stays_green_on_the_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2360: the documented check walks BOTH summary directories, and a healthy
    split tick (nwm unit `raw,precip-cache`, system unit `canonical`) is green in
    both -- `lane_not_selected` is not an `*_unsafe` reason.
    """
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; it is present on node-27")
    text = _RETENTION_ENV_EXAMPLE.read_text(encoding="utf-8")
    loop = next(line for line in text.splitlines() if "for d in " in line)
    assert "/home/nwm/node27-raw-retention-logs" in loop
    assert "/var/log/nhms-node27-canonical-retention" in loop
    program = _documented_operator_jq_program()

    store = tmp_path / "store"
    cache = tmp_path / "cache"
    aged = _three_aged_lanes(store, cache)
    summaries = {}
    for lanes in ("raw,precip-cache", "canonical"):
        summary = tmp_path / "summaries" / f"{lanes}.json"
        _production_env(monkeypatch, store=store, cache=cache, summary_path=summary)
        monkeypatch.setenv("NODE27_RAW_RETENTION_LANES", lanes)
        exit_code, payload = _production_tick(capsys)
        assert exit_code == 0
        summaries[lanes] = payload
        check = subprocess.run(["jq", "-e", program, str(summary)], capture_output=True, text=True)
        assert check.returncode == 0, (lanes, check.stderr)

    assert all(not path.exists() for path in aged.values())
    nwm, canonical = summaries["raw,precip-cache"], summaries["canonical"]
    assert (nwm["lanes"], canonical["lanes"]) == (["precip-cache", "raw"], ["canonical"])
    # One cutoff rule across the two units (design D3).
    for field in ("cutoff", "retention_days", "sources"):
        assert nwm[field] == canonical[field], field
