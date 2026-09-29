"""#2627: the MVT retention runner's basemap stage.

`<root>/basemap/tianditu/<layer>/<z>/<x>/` is shared with yd-viewer, so the
stage prunes only the documented shapes, keeps unknown names and symlinks,
deletes only behind its own `NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE=1`
switch, and a failure in it turns the run red without stopping the `.pbf` lane.
"""

from __future__ import annotations

import json
import os
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes import basemap as basemap_route
from scripts import node27_mvt_cache_retention as runner

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
REFERENCE = "2026-09-28T12:00:00Z"
DAY = 86400.0
AGED_TILE = NOW.timestamp() - 31 * DAY
FRESH_TILE = NOW.timestamp() - 29 * DAY
AGED_TMP = NOW.timestamp() - 2 * DAY
FRESH_TMP = NOW.timestamp() - 3600
AGED_PBF = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
HEX32 = "0123456789abcdef" * 2

_ENV_NAMES = (
    "NHMS_MVT_FILE_CACHE_DIR",
    "NODE27_MVT_CACHE_RETENTION_DAYS",
    "NODE27_MVT_CACHE_RETENTION_ENABLED",
    "NODE27_MVT_CACHE_RETENTION_PLAN_ONLY",
    "NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS",
    "NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tile")
    os.utime(path, (mtime, mtime))
    return path


class Tree:
    """One cache root with every shape the stage must tell apart."""

    def __init__(self, root: Path) -> None:
        self.root = root
        provider = root / "basemap" / "tianditu"
        self.provider = provider
        (root / runner.LOCKS_DIR_NAME).mkdir(parents=True)
        self.pbf = _touch(root / "ab" / ("a" + "0" * 63 + ".pbf"), AGED_PBF)
        # <x> directory that empties completely -> x and z directories removed.
        self.lone_tile = _touch(provider / "vec" / "7" / "100" / "50", AGED_TILE)
        # Mixed <x> directory: only the aged tile and the aged temp files go.
        mixed = provider / "img" / "9" / "400"
        self.aged_tile = _touch(mixed / "200", AGED_TILE)
        self.fresh_tile = _touch(mixed / "201", FRESH_TILE)
        self.nwm_tmp = _touch(mixed / f".202.4242.139871.{HEX32}.tmp", AGED_TMP)
        self.yd_tmp = _touch(mixed / f"tmp-{HEX32}", AGED_TMP)
        self.young_tmp = _touch(mixed / f"tmp-{'f' * 32}", FRESH_TMP)
        self.unknown = _touch(mixed / "203.png", AGED_TILE)
        self.link_target = _touch(root / "elsewhere" / "target", AGED_TILE)
        self.link = mixed / "204"
        self.link.symlink_to(self.link_target)
        # Unknown layer and non-decimal levels are never entered.
        self.foreign_layer = _touch(provider / "osm" / "1" / "1" / "1", AGED_TILE)
        self.odd_level = _touch(provider / "vec" / "zz" / "1" / "1", AGED_TILE)

    def exists(self, *paths: Path) -> list[bool]:
        return [path.exists() or path.is_symlink() for path in paths]


def _config(root: Path, **overrides: Any) -> runner.MvtCacheRetentionConfig:
    values: dict[str, Any] = {"cache_root": root, "retention_days": 14, "summary_path": None}
    values.update(overrides)
    return runner.MvtCacheRetentionConfig(**values)


def _paths(entries: list[dict[str, Any]], kind: str | None = None) -> set[str]:
    return {entry["path"] for entry in entries if kind is None or entry["kind"] == kind}


def test_delete_mode_prunes_only_the_documented_shapes(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "cache")

    payload = runner.run_retention(_config(tree.root, basemap_delete=True), now=NOW)
    stage = payload["basemap"]

    assert stage["mode"] == "delete"
    assert tree.exists(tree.lone_tile, tree.aged_tile, tree.nwm_tmp, tree.yd_tmp) == [False] * 4
    kept = (tree.fresh_tile, tree.young_tmp, tree.unknown, tree.link, tree.link_target)
    kept += (tree.foreign_layer, tree.odd_level)
    assert tree.exists(*kept) == [True] * len(kept)
    assert not (tree.provider / "vec" / "7").exists()
    assert (tree.provider / "vec").is_dir() and (tree.provider / "img" / "9" / "400").is_dir()
    assert _paths(stage["deleted"], runner.KIND_BASEMAP_TILE) == {str(tree.lone_tile), str(tree.aged_tile)}
    assert _paths(stage["deleted"], runner.KIND_BASEMAP_TMP) == {str(tree.nwm_tmp), str(tree.yd_tmp)}
    assert _paths(stage["deleted"], runner.KIND_BASEMAP_DIR) == {
        str(tree.provider / "vec" / "7" / "100"),
        str(tree.provider / "vec" / "7"),
    }
    assert stage["counts"] == {
        "tile": 2,
        "tmp": 2,
        "dir": 2,
        "planned": 6,
        "deleted": 6,
        "skipped": 1,
        "symlinks_skipped": 1,
        "failed": 0,
    }
    assert stage["freed_bytes"] == 4 * len(b"tile")
    assert stage["retention_days"] == 30
    # The two lanes never share a path.
    assert _paths(payload["deleted"]) == {str(tree.pbf)}
    assert not any("/basemap/" in entry["path"] for entry in payload["planned"] + payload["skipped"])


def test_without_the_explicit_switch_the_stage_only_counts(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "cache")
    reference = Tree(tmp_path / "reference")
    deleted = runner.run_retention(_config(reference.root, basemap_delete=True), now=NOW)["basemap"]

    payload = runner.run_retention(_config(tree.root), now=NOW)
    stage = payload["basemap"]

    assert stage["mode"] == "dry_run"
    assert stage["deleted"] == [] and stage["freed_bytes"] == 0
    assert tree.exists(tree.lone_tile, tree.aged_tile, tree.nwm_tmp, tree.yd_tmp) == [True] * 4
    relative = {Path(p).relative_to(tree.root).as_posix() for p in _paths(stage["planned"])}
    assert relative == {Path(p).relative_to(reference.root).as_posix() for p in _paths(deleted["deleted"])}
    # The .pbf lane is not gated by the basemap switch.
    assert _paths(payload["deleted"]) == {str(tree.pbf)}


def test_plan_only_and_disabled_win_over_the_switch(tmp_path: Path) -> None:
    tree = Tree(tmp_path / "cache")

    planned = runner.run_retention(_config(tree.root, basemap_delete=True, plan_only=True), now=NOW)
    disabled = runner.run_retention(_config(tree.root, basemap_delete=True, enabled=False), now=NOW)

    assert planned["basemap"]["mode"] == "dry_run"
    assert planned["basemap"]["counts"]["planned"] == 6 and planned["basemap"]["deleted"] == []
    assert disabled["basemap"]["mode"] == "disabled"
    assert disabled["basemap"]["counts"]["planned"] == 0
    assert tree.exists(tree.lone_tile, tree.aged_tile, tree.nwm_tmp, tree.yd_tmp, tree.pbf) == [True] * 5


def test_an_absent_basemap_subtree_is_a_skip_not_a_blocker(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    (root / runner.LOCKS_DIR_NAME).mkdir(parents=True)
    pbf = _touch(root / "ab" / ("a" + "0" * 63 + ".pbf"), AGED_PBF)

    payload = runner.run_retention(_config(root, basemap_delete=True), now=NOW)

    assert payload["status"] == "completed"
    assert _paths(payload["deleted"]) == {str(pbf)}
    assert payload["basemap"]["skipped"] == [
        {"path": str(root / "basemap"), "kind": None, "reason": "subtree_absent"}
    ]
    assert payload["basemap"]["failed"] == []


@pytest.mark.parametrize("linked", ["basemap", "basemap/tianditu"])
def test_a_symlinked_basemap_component_is_never_walked(tmp_path: Path, linked: str) -> None:
    """A link at `basemap/` or `basemap/tianditu/` would carry deletions outside the cache root."""
    outside = Tree(tmp_path / "outside")
    root = tmp_path / "cache"
    (root / runner.LOCKS_DIR_NAME).mkdir(parents=True)
    link = root / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside.root / linked)

    stage = runner.run_retention(_config(root, basemap_delete=True), now=NOW)["basemap"]

    assert stage["planned"] == [] and stage["deleted"] == []
    assert stage["skipped"] == [{"path": str(link), "kind": None, "reason": "symlink"}]
    assert outside.exists(outside.lone_tile, outside.aged_tile, outside.nwm_tmp, outside.yd_tmp) == [True] * 4


def _run_main(monkeypatch: pytest.MonkeyPatch, root: Path, summary: Path, **env: str) -> tuple[int, dict[str, Any]]:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    rc = runner.main(["--reference-time", REFERENCE, "--summary-path", str(summary)])
    return rc, json.loads(summary.read_text(encoding="utf-8"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_basemap_failure_turns_the_run_red_and_the_pbf_lane_still_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = Tree(tmp_path / "cache")
    locked = tree.aged_tile.parent
    locked.chmod(0o555)  # another uid's directory: its files cannot be unlinked
    try:
        rc, payload = _run_main(
            monkeypatch, tree.root, tmp_path / "summary.json", NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE="1"
        )
    finally:
        locked.chmod(0o755)

    assert rc == 1
    assert payload["counts"]["failed"] == 0
    assert _paths(payload["deleted"]) == {str(tree.pbf)}
    assert {entry["error_type"] for entry in payload["basemap"]["failed"]} == {"PermissionError"}
    assert str(tree.aged_tile) in _paths(payload["basemap"]["failed"])
    assert not tree.lone_tile.exists()  # the rest of the stage still ran


@pytest.mark.parametrize("value", ["0", "-3", "thirty", "1.5"])
def test_an_invalid_basemap_age_blocks_preflight_with_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    tree = Tree(tmp_path / "cache")

    rc, payload = _run_main(
        monkeypatch, tree.root, tmp_path / "summary.json", NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS=value
    )

    assert rc == 2
    assert payload["status"] == "preflight_blocked"
    assert [blocker["field"] for blocker in payload["blockers"]] == ["NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS"]
    assert tree.exists(tree.pbf, tree.lone_tile) == [True, True]


@pytest.mark.parametrize(
    ("value", "deletes"), [("1", True), ("true", False), ("yes", False), (" 1", False), ("", False)]
)
def test_only_the_literal_one_enables_basemap_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, deletes: bool
) -> None:
    tree = Tree(tmp_path / "cache")

    rc, payload = _run_main(
        monkeypatch,
        tree.root,
        tmp_path / "summary.json",
        NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE=value,
        NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS="30",
    )

    assert rc == 0
    assert payload["basemap"]["mode"] == ("delete" if deletes else "dry_run")
    assert tree.lone_tile.exists() is not deletes


def test_the_basemap_age_comes_from_the_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = Tree(tmp_path / "cache")

    rc, payload = _run_main(
        monkeypatch, tree.root, tmp_path / "summary.json", NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS="28"
    )

    assert rc == 0
    assert payload["basemap"]["retention_days"] == 28
    assert str(tree.fresh_tile) in _paths(payload["basemap"]["planned"])


def test_a_tile_refreshed_after_the_scan_is_not_deleted(tmp_path: Path) -> None:
    tile = _touch(tmp_path / "7", NOW.timestamp())

    outcome, error = runner._remove_basemap_file(tile, AGED_TILE + DAY)

    assert (outcome, error) == ("refreshed_since_scan", None)
    assert tile.exists()


def test_the_known_layers_are_the_routes_layers() -> None:
    assert runner.BASEMAP_LAYERS == frozenset(typing.get_args(basemap_route.TiandituLayer))
