"""#2032 MVT cache retention: enumeration races, config gates and the summary receipt.

Partition of ``tests/test_node27_mvt_cache_retention.py`` (#2490, pure move);
the governing invariant is stated there.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import services.tiles.mvt as mvt
from scripts import node27_mvt_cache_retention as runner
from scripts import node27_raw_retention
from tests.node27_mvt_cache_retention_helpers import (
    _AGED,
    _ENUMERATION_FAILURE_KEYS,
    _NOW,
    _SHA_A,
    _SHA_B,
    _SHA_C,
    _clean_env,  # noqa: F401 -- autouse
    _config,
    _mixed_tree,
    _paths,
    _touch,
)


def _replace_directory(directory: Path, replacement: str) -> None:
    """Really remove `directory`, or really swap a regular file in for it, so
    the `os.scandir` that follows raises the kernel's own errno."""
    shutil.rmtree(directory)
    if replacement == "file":
        directory.write_bytes(b"not a directory any more")


_REPLACEMENT_ERRORS = {"removed": FileNotFoundError, "file": NotADirectoryError}


def _racing_scandir(vanishing: Path, replacement: str) -> Any:
    """An `os.scandir` that lets the parent list `vanishing` and then removes
    it right before its own listing -- the interleaving of an operator's
    `rm -rf` landing mid-tick. Only the timing is injected; the error is real."""
    real_scandir = os.scandir

    def racing_scandir(path: Any = ".") -> Any:
        if str(path) == str(vanishing) and vanishing.is_dir():
            _replace_directory(vanishing, replacement)
        return real_scandir(path)

    return racing_scandir


@pytest.mark.parametrize("replacement", ["removed", "file"])
@pytest.mark.parametrize("lane", ["tile", "lock"])
def test_a_hex_directory_removed_mid_run_is_the_race_not_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lane: str,
    replacement: str,
) -> None:
    """The same race as a single entry vanishing mid-scan, one level up: a
    `<hh>` that is gone holds no aged files, so it is neither a target source
    nor a failure. rc stays 0 and the siblings still prune."""
    root = tmp_path / "cache"
    tile_in_ab = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    lock_in_ab = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    sibling_pbf = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    sibling_lock = _touch(root / ".locks" / "cd" / f"{_SHA_B}.lock", mtime=_AGED, content=b"")
    vanishing = root / "ab" if lane == "tile" else root / ".locks" / "ab"
    untouched = lock_in_ab if lane == "tile" else tile_in_ab
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", _racing_scandir(vanishing, replacement))
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert (vanishing.is_file() if replacement == "file" else not vanishing.exists())
    assert exit_code == 0
    assert payload["failed"] == []
    assert payload["counts"]["failed"] == 0
    assert sorted(_paths(payload["deleted"])) == sorted(
        [str(sibling_pbf), str(sibling_lock), str(untouched)]
    )
    assert not sibling_pbf.exists()
    assert not sibling_lock.exists()


@pytest.mark.parametrize("replacement", ["removed", "file"])
@pytest.mark.parametrize("which", ["cache_root", "locks_root"])
def test_the_cache_root_and_locks_directory_themselves_are_never_exempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    which: str,
    replacement: str,
) -> None:
    """The `<hh>` exemption must not reach the two directories above it. No
    display worker ever removes the cache root or `.locks`, so either one
    vanishing after preflight / after `_locks_root_skip` is not the
    concurrent-miss race; it stays `enumeration_unavailable` and rc 1."""
    root = tmp_path / "cache"
    _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    sibling_pbf = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    vanishing = root if which == "cache_root" else root / ".locks"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", _racing_scandir(vanishing, replacement))
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["status"] == "completed"
    assert payload["failed"] == [
        {
            "path": str(vanishing),
            "kind": None,
            "reason": "enumeration_unavailable",
            "error": payload["failed"][0]["error"],
            "error_type": _REPLACEMENT_ERRORS[replacement].__name__,
        }
    ]
    if which == "locks_root":
        # The tile lane had already run; only the lock lane lost its listing.
        assert _paths(payload["deleted"]) == [str(sibling_pbf)]
        assert payload["skipped"] == []
    else:
        assert payload["deleted"] == []


@pytest.mark.parametrize("lane", ["tile", "lock"])
def test_a_stale_hex_directory_is_still_an_enumeration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lane: str,
) -> None:
    """Only ENOENT / ENOTDIR are exempt on a `<hh>`; an NFS `ESTALE` there
    still hides an unknown number of aged files."""
    root = tmp_path / "cache"
    _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    stale = root / "ab" if lane == "tile" else root / ".locks" / "ab"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    real_scandir = os.scandir

    def stale_scandir(path: Any = ".") -> Any:
        if str(path) == str(stale):
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE), str(path))
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", stale_scandir)
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["failed"] == [
        {
            "path": str(stale),
            "kind": None,
            "reason": "enumeration_unavailable",
            "error": payload["failed"][0]["error"],
            "error_type": "OSError",
        }
    ]
    assert f"[Errno {errno.ESTALE}]" in payload["failed"][0]["error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory anyway")
def test_an_unreadable_locks_directory_fails_the_run_and_the_tile_lane_still_prunes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.locks` itself `lstat`s fine (its parent is readable), so
    `_locks_root_skip` lets the lane run and the `scandir` gets `EACCES`. That
    is a failure, not the `locks_root_unsafe` lane skip, and not silence."""
    root = tmp_path / "cache"
    locks_root = root / ".locks"
    hidden_lock = _touch(locks_root / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")
    sibling_pbf = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    locks_root.chmod(0o000)
    try:
        assert stat.S_ISDIR(os.lstat(locks_root).st_mode)
        exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
        payload = json.loads(capsys.readouterr().out)
    finally:
        locks_root.chmod(0o755)

    assert exit_code == 1
    assert payload["failed"] == [
        {
            "path": str(locks_root),
            "kind": None,
            "reason": "enumeration_unavailable",
            "error": payload["failed"][0]["error"],
            "error_type": "PermissionError",
        }
    ]
    assert set(payload["failed"][0]) == _ENUMERATION_FAILURE_KEYS
    assert payload["skipped"] == []
    assert _paths(payload["deleted"]) == [str(sibling_pbf)]
    assert not sibling_pbf.exists()
    assert hidden_lock.exists()


# ---------------------------------------------------------------------------
# Scenario: missing or unsafe cache root blocks before any deletion
# ---------------------------------------------------------------------------
def _blocked_fields(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {(str(entry["field"]), str(entry["reason"])) for entry in payload["blockers"]}


def test_unset_cache_root_blocks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "preflight_blocked"
    assert payload["execution_mode"] == "preflight_blocked"
    assert ("cache_root", "missing") in _blocked_fields(payload)
    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("relative", "path_not_absolute"),
        ("root", "path_is_root"),
        ("missing", "path_missing"),
        ("symlink", "path_is_symlink"),
        ("dangling_symlink", "path_is_symlink"),
        ("file", "path_not_directory"),
    ],
)
def test_unsafe_cache_roots_block_before_any_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    reason: str,
) -> None:
    real_root = tmp_path / "cache"
    aged_pbf = _touch(real_root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    if kind == "relative":
        value = "relative/cache"
    elif kind == "root":
        value = "/"
    elif kind == "missing":
        value = str(tmp_path / "absent")
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(real_root, target_is_directory=True)
        value = str(link)
    elif kind == "dangling_symlink":
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "never-existed")
        value = str(link)
    else:
        value = str(_touch(tmp_path / "cache.txt", mtime=_AGED))
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", value)

    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert ("cache_root", reason) in _blocked_fields(payload)
    assert aged_pbf.exists(), "a blocked preflight must delete nothing"


@pytest.mark.parametrize(
    "argv, env, field, reason",
    [
        (["--retention-days", "0"], {}, "retention_days", "must_be_at_least_one"),
        (["--retention-days", "-3"], {}, "retention_days", "must_be_at_least_one"),
        (["--retention-days", "seven"], {}, "retention_days", "not_an_integer"),
        (
            [],
            {"NODE27_MVT_CACHE_RETENTION_DAYS": "0"},
            "NODE27_MVT_CACHE_RETENTION_DAYS",
            "must_be_at_least_one",
        ),
        (
            [],
            {"NODE27_MVT_CACHE_RETENTION_DAYS": "fourteen"},
            "NODE27_MVT_CACHE_RETENTION_DAYS",
            "not_an_integer",
        ),
        (["--reference-time", "2026/09/08"], {}, "reference_time", "not_rfc3339"),
        (["--reference-time", "2026-09-08T12:00:00"], {}, "reference_time", "not_rfc3339"),
    ],
)
def test_malformed_age_or_reference_time_blocks_without_falling_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    env: dict[str, str],
    field: str,
    reason: str,
) -> None:
    """The explicit departure from `node27_raw_retention._env_int`: no silent
    fallback to the 14-day default, and `--retention-days 0` is not `or`-ed away."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    exit_code = runner.main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert (field, reason) in _blocked_fields(payload)
    assert aged_pbf.exists()


def test_a_blocked_run_writes_its_receipt_to_the_summary_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same sink a completed run would use, or the operator reads a stale file."""
    summary_path = tmp_path / "logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path / "absent"))

    exit_code = runner.main(["--summary-path", str(summary_path)])
    capsys.readouterr()

    assert exit_code == 2
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["status"] == "preflight_blocked"
    assert ("cache_root", "path_missing") in _blocked_fields(payload)


# ---------------------------------------------------------------------------
# Requirement 2: summary schema, gates and exit codes
# ---------------------------------------------------------------------------
def test_disabled_gate_yields_a_disabled_summary_with_zero_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_ENABLED", "false")

    exit_code = runner.main([])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["execution_mode"] == "disabled"
    assert payload["status"] == "disabled"
    assert payload["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}
    assert payload["freed_bytes"] == 0
    assert aged_pbf.exists()


def test_env_gates_are_parsed_into_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_ENABLED", "false")
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_PLAN_ONLY", "true")
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_DAYS", "3")

    config, blockers = runner.config_from_env(
        runner.build_parser().parse_args(["--summary-path", str(tmp_path / "s.json")])
    )

    assert blockers == []
    assert config is not None
    assert (config.enabled, config.plan_only, config.retention_days) == (False, True, 3)
    assert config.cache_root == root


def test_a_leftover_dry_run_variable_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1407: the gate is deliberately not named `*_DRY_RUN`."""
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_DRY_RUN", "true")
    try:
        config, _ = runner.config_from_env(runner.build_parser().parse_args([]))
    finally:
        monkeypatch.delenv("NODE27_MVT_CACHE_RETENTION_DRY_RUN", raising=False)

    assert config is not None
    assert config.plan_only is False


def test_summary_carries_every_required_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED, content=b"1234567")
    summary_path = tmp_path / "logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))

    exit_code = runner.main(
        ["--summary-path", str(summary_path), "--reference-time", "2026-09-08T12:00:00Z"]
    )
    printed = json.loads(capsys.readouterr().out)
    payload = json.loads(summary_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert printed == payload, "the file and stdout must carry the same receipt"
    assert set(payload) >= {
        "schema_version",
        "started_at",
        "finished_at",
        "reference_time",
        "cache_root",
        "retention_days",
        "cutoff",
        "enabled",
        "plan_only",
        "execution_mode",
        "status",
        "counts",
        "planned",
        "deleted",
        "skipped",
        "failed",
        "freed_bytes",
        "precip_root_untouched",
    }
    assert payload["schema_version"] == "nhms.node27_mvt_cache_retention.production.v1"
    assert payload["reference_time"] == "2026-09-08T12:00:00Z"
    assert payload["cutoff"] == "2026-08-25T12:00:00Z"
    assert payload["retention_days"] == 14
    assert payload["cache_root"] == str(root)
    assert payload["precip_root_untouched"] == str(root / "precip")
    assert payload["freed_bytes"] == 7
    entry = payload["deleted"][0]
    assert entry == {
        "path": str(aged_pbf),
        "kind": "pbf",
        "size_bytes": 7,
        "mtime": "2026-08-01T00:00:00Z",
    }


def test_a_relative_summary_path_is_a_valid_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt is a file this process CREATES, not a tree it deletes from,
    so the absoluteness `cache_root` needs buys nothing here."""
    root = tmp_path / "cache"
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.chdir(tmp_path)

    exit_code = runner.main(
        ["--summary-path", "logs/summary.json", "--reference-time", "2026-09-08T12:00:00Z"]
    )
    printed = json.loads(capsys.readouterr().out)
    payload = json.loads((tmp_path / "logs" / "summary.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert printed == payload
    assert _paths(payload["deleted"]) == [str(aged_pbf)]


def test_the_summary_path_env_variable_is_not_a_runner_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH` belongs to the WRAPPER, which
    resolves it and always passes the result as `--summary-path`. A second
    reader here would only be a way for the two to disagree."""
    root = tmp_path / "cache"
    _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    ghost = tmp_path / "ghost-logs" / "summary.json"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setenv("NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH", str(ghost))

    exit_code = runner.main(["--reference-time", "2026-09-08T12:00:00Z"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "completed"
    assert payload["counts"]["deleted"] == 1
    assert not ghost.parent.exists(), "stdout was the sink; the env var is inert"


def test_an_undeletable_target_is_a_failure_and_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes, so the failure cannot be simulated")
    root = tmp_path / "cache"
    blocked_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    deletable = _touch(root / "cd" / f"{_SHA_B}.pbf", mtime=_AGED)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    (root / "ab").chmod(0o555)
    try:
        exit_code = runner.main([])
        payload = json.loads(capsys.readouterr().out)
    finally:
        (root / "ab").chmod(0o755)

    assert exit_code == 1
    assert payload["failed"] == [
        {
            "path": str(blocked_pbf),
            "kind": "pbf",
            "error": payload["failed"][0]["error"],
            "error_type": "PermissionError",
        }
    ]
    assert payload["failed"][0]["error"]
    assert _paths(payload["deleted"]) == [str(deletable)]
    assert blocked_pbf.exists()


def test_repeated_ticks_are_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _mixed_tree(root)

    first = runner.run_retention(_config(root), now=_NOW)
    second = runner.run_retention(_config(root), now=_NOW)

    assert first["counts"]["deleted"] == 3
    assert second["counts"] == {"planned": 0, "deleted": 0, "skipped": 0, "failed": 0}


def test_the_cutoff_is_wall_clock_relative_to_the_reference_time(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    just_inside = _touch(
        root / "ab" / f"{_SHA_A}.pbf",
        mtime=datetime(2026, 8, 25, 11, 59, 59, tzinfo=UTC).timestamp(),
    )
    just_outside = _touch(
        root / "ab" / f"{_SHA_B}.pbf",
        mtime=datetime(2026, 8, 25, 12, 0, 1, tzinfo=UTC).timestamp(),
    )
    # The cutoff instant itself. The comparison is `st_mtime < cutoff`, so this
    # one SURVIVES; a `<=` would delete it and nothing else in this file notices.
    on_the_cutoff = _touch(
        root / "ab" / f"{_SHA_C}.pbf",
        mtime=datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC).timestamp(),
    )

    payload = runner.run_retention(_config(root, reference_time=_NOW), now=_NOW)

    assert _paths(payload["deleted"]) == [str(just_inside)]
    assert _paths(payload["planned"]) == [str(just_inside)]
    assert just_outside.exists()
    assert on_the_cutoff.exists()


# ---------------------------------------------------------------------------
# Shared-shape contract with services/tiles/mvt.py (both directions)
# ---------------------------------------------------------------------------
def test_the_three_patterns_match_the_paths_mvt_actually_produces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-source pin: the producer builds the paths, the runner's regexes must
    match them. Change the layout in `services/tiles/mvt.py` and this reds."""
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = mvt.TileInput(
        layer_id="hydro-national",
        source_id="gfs",
        source_version="v1",
        valid_time="2026-09-01T00:00:00Z",
        z=4,
        x=13,
        y=6,
    )
    key = mvt.cache_key(tile)
    body_path = mvt._file_cache_path(key)
    lock_path = mvt._file_cache_lock_path(key)
    assert body_path is not None and lock_path is not None
    # The `.tmp` name `_write_file_cache` really leaves behind, observed rather
    # than reconstructed: the writer is driven and the intermediate captured.
    observed_tmp: list[str] = []
    real_replace = os.replace

    def capture(src: Any, dst: Any) -> None:
        observed_tmp.append(Path(src).name)
        real_replace(src, dst)

    # Scoped: `os.replace` is a process-global, and pyproject.toml's author
    # contract calls an unscoped global `os.*` patch a teardown hazard.
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", capture)
        assert mvt._write_file_cache(key, b"tile-bytes") is True

    assert runner.HEX_DIR_PATTERN.match(body_path.parent.name)
    assert runner.HEX_DIR_PATTERN.match(lock_path.parent.name)
    assert body_path.parent.parent == Path(tmp_path)
    assert lock_path.parent.parent == Path(tmp_path) / runner.LOCKS_DIR_NAME
    assert runner.PBF_PATTERN.match(body_path.name)
    assert runner.LOCK_PATTERN.match(lock_path.name)
    assert observed_tmp and runner.TMP_PATTERN.match(observed_tmp[0])
    # And no pattern matches the other lane's name.
    assert not runner.LOCK_PATTERN.match(body_path.name)
    assert not runner.PBF_PATTERN.match(lock_path.name)
    assert not runner.PBF_PATTERN.match(observed_tmp[0])


def test_a_real_mvt_cache_write_is_collected_by_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on one root: the display API writes, the runner collects."""
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    tile = mvt.TileInput(
        layer_id="river-network-national",
        source_id="gfs",
        source_version="v1",
        valid_time="2026-09-01T00:00:00Z",
        z=6,
        x=50,
        y=25,
    )
    key = mvt.cache_key(tile)
    assert mvt._write_file_cache(key, b"tile-bytes") is True
    with mvt.tile_generation_lock(tile):
        lock_path = mvt._file_cache_lock_path(key)
        assert lock_path is not None
        os.utime(lock_path, (_AGED, _AGED))
        body = mvt._file_cache_path(key)
        assert body is not None
        os.utime(body, (_AGED, _AGED))
        targets, _, _ = runner.collect_targets(tmp_path, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert sorted(target.kind for target in targets) == ["lock", "pbf"]
    assert {str(target.path) for target in targets} == {str(body), str(lock_path)}


def test_the_two_runners_never_list_each_others_paths(tmp_path: Path) -> None:
    """Mirror-image exclusion on ONE shared root (spec Requirement 5)."""
    root = tmp_path / "cache"
    precip_cycle = root / "precip" / "IFS" / "2026060100"
    precip_cycle.mkdir(parents=True)
    png = precip_cycle / "2026-06-01T03:00:00Z.cma24h6-abcdef01.0123456789ab.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.utime(precip_cycle, (_AGED, _AGED))
    aged_pbf = _touch(root / "ab" / f"{_SHA_A}.pbf", mtime=_AGED)
    aged_lock = _touch(root / ".locks" / "ab" / f"{_SHA_A}.lock", mtime=_AGED, content=b"")

    object_store = tmp_path / "object-store"
    (object_store / "raw").mkdir(parents=True)
    raw_payload = node27_raw_retention.run_retention(
        node27_raw_retention.RawRetentionConfig(
            object_store_root=object_store,
            retention_days=14,
            sources=frozenset({"gfs", "ifs"}),
            summary_path=None,
            dry_run=True,
            precip_cache_root=root,
        ),
        now=_NOW,
    )
    mvt_payload = runner.run_retention(_config(root, plan_only=True), now=_NOW)

    raw_paths = _paths(raw_payload["planned"])
    mvt_paths = _paths(mvt_payload["planned"])
    assert raw_paths == [str(precip_cycle)]
    assert sorted(mvt_paths) == sorted([str(aged_pbf), str(aged_lock)])
    assert not [path for path in raw_paths if not path.startswith(str(root / "precip"))]
    assert not [path for path in mvt_paths if path.startswith(str(root / "precip"))]
    assert set(raw_paths).isdisjoint(mvt_paths)
