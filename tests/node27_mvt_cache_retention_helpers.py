"""Shared fixtures and builders of the #2032 MVT tile file-cache retention suites.

Non-collectible support module for the three partitions of the runner/wrapper
suite (#2490 split, pure move): ``tests/test_node27_mvt_cache_retention.py``
(the three pruned shapes and the lock lane),
``tests/test_node27_mvt_cache_retention_enumeration.py`` (enumeration races,
config gates and the summary receipt) and
``tests/test_node27_mvt_cache_retention_wrapper.py`` (units, env template and
the ``_once.sh`` wrapper). ``_clean_env`` is autouse: every partition imports it
so the display-process env never leaks into a case.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_mvt_cache_retention as runner

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WRAPPER_PATH = _REPO_ROOT / "scripts/node27_mvt_cache_retention_once.sh"
_SERVICE_PATH = _REPO_ROOT / "infra/systemd/nhms-node27-mvt-cache-retention.service"
_TIMER_PATH = _REPO_ROOT / "infra/systemd/nhms-node27-mvt-cache-retention.timer"
_ENV_EXAMPLE_PATH = _REPO_ROOT / "infra/env/node27-mvt-cache-retention.example"

_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
_AGED = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC).timestamp()
_FRESH = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC).timestamp()

_SHA_A = "a" + "0" * 63
_SHA_B = "b" + "1" * 63
_SHA_C = "c" + "2" * 63
_SHA_D = "d" + "3" * 63
_SHA_E = "e" + "4" * 63

_ENV_NAMES = (
    "NHMS_MVT_FILE_CACHE_DIR",
    "NODE27_MVT_CACHE_RETENTION_DAYS",
    "NODE27_MVT_CACHE_RETENTION_ENABLED",
    "NODE27_MVT_CACHE_RETENTION_PLAN_ONLY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _touch(path: Path, *, mtime: float, content: bytes = b"tile") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def _config(root: Path, **overrides: Any) -> runner.MvtCacheRetentionConfig:
    defaults: dict[str, Any] = {
        "cache_root": root,
        "retention_days": 14,
        "summary_path": None,
    }
    defaults.update(overrides)
    return runner.MvtCacheRetentionConfig(**defaults)


def _paths(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["path"]) for entry in entries]


def _reasons(entries: list[dict[str, Any]]) -> set[str]:
    return {str(entry["reason"]) for entry in entries}


def _all_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["planned"] + payload["deleted"] + payload["failed"] + payload["skipped"]


def _code_only(text: str) -> str:
    """Source with docstrings and `#` comments stripped, so a prose mention of a
    flag cannot satisfy (or break) a source-level pin."""
    lines = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        lines.append(stripped)
    body = "\n".join(lines)
    parts = body.split('"""')
    return "".join(parts[::2])


# ---------------------------------------------------------------------------
# Scenario: aged tile, intermediate and lock files are pruned; all else survives
# ---------------------------------------------------------------------------
def _mixed_tree(root: Path) -> dict[str, Path]:
    """One cache root holding the three targets and every near-miss decoy."""
    tree = {
        "aged_pbf": _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED),
        "aged_tmp": _touch(root / "ab" / f".{_SHA_B}.pbf.123.tmp", mtime=_AGED),
        "aged_lock": _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b""),
        # Decoys, every one of them aged unless the name says otherwise.
        "fresh_pbf": _touch(root / "cd" / f"{_SHA_C}.pbf", mtime=_FRESH),
        "precip_png": _touch(root / "precip" / "IFS" / "2026060100" / "x.png", mtime=_AGED),
        "non_hex_dir_pbf": _touch(root / "zz" / f"{_SHA_A}.pbf", mtime=_AGED),
        "unshaped_name": _touch(root / "ab" / "notes.txt", mtime=_AGED),
        "too_deep": _touch(root / "ab" / "cd" / f"{_SHA_E}.pbf", mtime=_AGED),
        "pbf_in_locks": _touch(root / ".locks" / "ab" / f"{_SHA_C}.pbf", mtime=_AGED),
        "lock_in_tile_lane": _touch(root / "ab" / f"{_SHA_D}.lock", mtime=_AGED),
        "root_level_pbf": _touch(root / f"{_SHA_A}.pbf", mtime=_AGED),
    }
    # A symlink wearing a target name, pointing outside the cache root.
    outside = root.parent / "outside.pbf"
    _touch(outside, mtime=_AGED)
    symlink = root / "ab" / f"{_SHA_C}.pbf"
    symlink.symlink_to(outside)
    tree["symlink_pbf"] = symlink
    tree["symlink_victim"] = outside
    # A DIRECTORY wearing a target name.
    directory = root / "ab" / f"{_SHA_D}.pbf"
    directory.mkdir()
    os.utime(directory, (_AGED, _AGED))
    tree["directory_pbf"] = directory
    return tree


# ---------------------------------------------------------------------------
# Scenario: a directory that cannot be enumerated is a FAILURE, never silence
# ---------------------------------------------------------------------------
_ENUMERATION_FAILURE_KEYS = {"path", "kind", "reason", "error", "error_type"}
