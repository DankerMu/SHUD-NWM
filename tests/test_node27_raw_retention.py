from __future__ import annotations

import json
import os
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


def test_schema_version_is_v4_and_summary_discloses_both_new_roots(tmp_path: Path) -> None:
    """v3 -> v4: `deleted[]` may now hold canonical and PNG-cache directories."""
    assert node27_raw_retention.SCHEMA_VERSION == "nhms.node27_raw_retention.production.v4"

    (tmp_path / "raw").mkdir()
    unconfigured = node27_raw_retention.run_retention(
        _config(tmp_path),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )
    configured = node27_raw_retention.run_retention(
        _config(tmp_path, precip_cache_root=tmp_path / "cache"),
        now=datetime(2026, 6, 27, 12, tzinfo=UTC),
    )

    assert unconfigured["schema_version"] == "nhms.node27_raw_retention.production.v4"
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
    """Production shape (node-27, 2026-09-06): `canonical/<S>/` is not writable by `nwm`.

    Every canonical target then fails with ``PermissionError``; the obligation is
    a stable, distinguishable failure, not a fix (the remedy lives in the mirror
    producers or an ops group change).
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
    invariant. The label fix is tracked by #2104.
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
    assert _keys(payload["deleted"]) == ["precip-cache/IFS/2026060100"]
    assert payload["counts"]["failed"] == 0
    assert not aged_cache.exists()
    assert (store / "raw" / "gfs" / "2026060100").is_dir()
    assert (store / "canonical" / "IFS" / "2026060100").is_dir()
