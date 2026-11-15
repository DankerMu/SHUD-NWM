"""Requirement-driven tests for the root-level Slurm log lane (#3142).

Slurm writes ``<WORKSPACE_ROOT>/<run_id>/logs/...`` and the gateway writes
``<WORKSPACE_ROOT>/<cycle_id>/{manifests,array_logs}``. With the additional-root
gate open and the workspace root W named as ``slurm_log_root``, retention
reclaims those direct children of W -- and nothing else on W or on any other
root -- when their cycle is outside the additional-root window, below the
frontier, and nothing in their tree was modified within that window.

Every lane directory these tests build is backdated after its last write:
``NOW`` is 2026-06-03, so a freshly written file carries a wall-clock mtime far
past the cutoff and would land in ``slurm_log_recently_modified``.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import services.orchestrator.retention_copyback_mutex as retention_mutex_mod
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator.retention import plan_retention, run_retention
from tests.retention_test_helpers import (
    DELETING_CONFIG,
    EXTRA_CONFIG,
    NOW,
    _real_dir,
    _seed_run_workspace,
    _write,
)

# EXTRA_CONFIG's additional-root window is 30 days.
EXTRA_CUTOFF = NOW - timedelta(days=30)
IDLE_MTIME = (NOW - timedelta(days=60)).timestamp()
# Every lane name below carries the 2026010100 cycle: aged past the 30-day window.
AGED_CYCLE = datetime(2026, 1, 1, 0, tzinfo=UTC)
BELOW_AGED = NOW - timedelta(days=20)  # a frontier above AGED_CYCLE
LANE_TARGET = "slurm_log_cycle_aged_out"
LANE_RECENT = "slurm_log_recently_modified"
PAYLOAD = b"0123456789"  # 10 bytes per seeded file


# --- 2.1 layout helper -------------------------------------------------------
def _backdate(tree: Path, timestamp: float = IDLE_MTIME) -> None:
    """Set every file and directory under ``tree`` (and ``tree``) to ``timestamp``.

    Bottom-up, so a directory's mtime is set after its last child changed.
    Symlinks are left alone: their own mtime is not part of the recency guard.
    """
    for dirpath, _dirnames, filenames in os.walk(tree, topdown=False):
        for filename in filenames:
            path = Path(dirpath) / filename
            if not path.is_symlink():
                os.utime(path, (timestamp, timestamp))
        os.utime(dirpath, (timestamp, timestamp))


def _lane_dir(root: Path, rel_file: str, *, backdate: bool = True) -> Path:
    """Seed ``root/<rel_file>`` and return the root-level lane directory it lives in."""
    _write(root, rel_file, PAYLOAD)
    top = root / Path(rel_file).parts[0]
    if backdate:
        _backdate(top)
    return top


def _lane_entries(entries: list[dict], root: Path) -> dict[str, dict]:
    """Entries attributed to ``root`` whose key is root-level (no ``/``)."""
    return {
        entry["key"]: entry
        for entry in entries
        if entry["root"] == str(root) and entry.get("key") and "/" not in entry["key"]
    }


def _all_keys(result, root: Path) -> set[str]:
    return {
        entry["key"]
        for entry in [*result.planned, *result.skipped, *result.deleted, *result.failed]
        if entry["root"] == str(root)
    }


def _run(store: Path, workspace: Path, *, config=DELETING_CONFIG, bound=BELOW_AGED, **kwargs):
    kwargs.setdefault("runs_only_roots", (workspace,))
    kwargs.setdefault("slurm_log_root", workspace)
    return run_retention(
        object_store_root=store,
        now=NOW,
        config=config,
        active_lower_bound=bound,
        **kwargs,
    )


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """(object store, workspace root W), both free of symlinked ancestors."""
    return _real_dir(tmp_path, "object-store"), _real_dir(tmp_path, "workspace")


# --- 2.2 ---------------------------------------------------------------------
LANE_SHAPES = {
    "cycle_gfs_2026010100_convert_cohort_ab12": "cycle_gfs_2026010100_convert_cohort_ab12/logs/1.out",
    "fcst_ifs_2026010100_m1": "fcst_ifs_2026010100_m1/logs/2.err",
    "gfs_2026010100": "gfs_2026010100/manifests/forcing_index_x.json",
    "era5_2026010100": "era5_2026010100/array_logs/x/3_1.out",
}


def test_aged_slurm_log_directories_are_planned_and_removed(roots) -> None:
    store, workspace = roots
    for rel_file in LANE_SHAPES.values():
        _lane_dir(workspace, rel_file)

    planned_only = _run(store, workspace, config=EXTRA_CONFIG)

    expected = {
        name: {
            "key": name,
            "path": str(workspace / name),
            "cycle_time": "2026-01-01T00:00:00Z",
            "reason": LANE_TARGET,
            "size_bytes": len(PAYLOAD),
            "root": str(workspace),
        }
        for name in LANE_SHAPES
    }
    assert _lane_entries(planned_only.planned, workspace) == expected
    assert all((workspace / name).is_dir() for name in LANE_SHAPES)  # dry-run removes nothing

    deleted = _run(store, workspace)

    assert _lane_entries(deleted.deleted, workspace) == expected
    assert deleted.failed == []
    assert deleted.freed_bytes == len(PAYLOAD) * len(LANE_SHAPES)
    assert not any((workspace / name).exists() for name in LANE_SHAPES)
    assert deleted.to_dict()["schema_version"] == "nhms.production_scheduler.retention.v2"


# --- 2.3 ---------------------------------------------------------------------
def test_in_window_gateway_cycle_is_skipped_as_within_retention_window(roots) -> None:
    store, workspace = roots
    fresh = f"gfs_{(NOW - timedelta(days=3)).strftime('%Y%m%d%H')}"
    _lane_dir(workspace, f"{fresh}/manifests/forcing_index_x.json")

    result = _run(store, workspace, bound=None)

    assert _lane_entries(result.skipped, workspace) == {
        fresh: {"key": fresh, "root": str(workspace), "reason": "within_retention_window"}
    }
    assert _lane_entries(result.planned, workspace) == {}
    assert (workspace / fresh).is_dir()


# --- 2.4 ---------------------------------------------------------------------
@pytest.mark.parametrize("bound", [AGED_CYCLE, AGED_CYCLE - timedelta(hours=6)], ids=["at", "above"])
def test_aged_cycle_at_or_above_the_frontier_is_exempt_without_a_size_walk(roots, bound) -> None:
    store, workspace = roots
    _lane_dir(workspace, "gfs_2026010100/array_logs/x/3_1.out")

    result = _run(store, workspace, bound=bound)

    entry = _lane_entries(result.skipped, workspace)["gfs_2026010100"]
    assert entry["reason"] == "pipeline_frontier_exempt"
    assert "size_bytes" not in entry
    assert _lane_entries(result.planned, workspace) == {}
    assert (workspace / "gfs_2026010100/array_logs/x/3_1.out").is_file()


# --- 2.4b --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "recent_rel", "recent_mtime"),
    [
        (
            "analysis_era5_2026010100_2026010200_m",
            "logs/9.out",
            EXTRA_CUTOFF.timestamp(),  # exactly at the cutoff counts as recent
        ),
        ("ifs_2026010100", "manifests/forcing_index_x.json", None),  # written just now
    ],
    ids=["analysis-at-cutoff", "gateway-cycle-now"],
)
def test_recently_touched_tree_below_the_frontier_is_kept(
    roots, name: str, recent_rel: str, recent_mtime: float | None
) -> None:
    store, workspace = roots
    _lane_dir(workspace, f"{name}/logs/1.out")
    _write(workspace / name, recent_rel, PAYLOAD)
    os.utime(workspace / name, (IDLE_MTIME, IDLE_MTIME))
    os.utime((workspace / name / recent_rel).parent, (IDLE_MTIME, IDLE_MTIME))
    if recent_mtime is not None:
        os.utime(workspace / name / recent_rel, (recent_mtime, recent_mtime))
    _lane_dir(workspace, "era5_2026010100/array_logs/x/3_1.out")  # positive control

    result = _run(store, workspace)

    entry = _lane_entries(result.skipped, workspace)[name]
    assert entry["reason"] == LANE_RECENT
    assert entry["cycle_time"] == "2026-01-01T00:00:00Z"
    assert "size_bytes" not in entry
    assert (workspace / name / recent_rel).is_file()
    assert set(_lane_entries(result.deleted, workspace)) == {"era5_2026010100"}


def test_a_recent_lane_directory_mtime_alone_keeps_the_tree(roots) -> None:
    """The directory's own mtime counts: a just-created ``logs/`` with no file yet."""
    store, workspace = roots
    _lane_dir(workspace, "fcst_gfs_2026010100_m/logs/1.out")
    (workspace / "fcst_gfs_2026010100_m/array_logs").mkdir()

    result = _run(store, workspace)

    assert _lane_entries(result.skipped, workspace)["fcst_gfs_2026010100_m"]["reason"] == LANE_RECENT
    assert (workspace / "fcst_gfs_2026010100_m").is_dir()


# --- 2.5 ---------------------------------------------------------------------
NON_LANE_NAMES = (
    "scheduler",
    "slurm-logs",
    "repairs",
    "runs",
    "onboarding-19-20260921",
    "GFS_2026010100",
    "gfs_2026010100x",
    "gfs_2026013200",
    "reset_2026070115",
    "backup_2026010100",
    "manual_reconvert_gfs_2026062700",
    "manual_gfs_2026062700_recanonical_20260629T172037Z",
    "cmfd_2026010100",
)


def test_operational_and_look_alike_names_are_never_selected(roots) -> None:
    store, workspace = roots
    for name in NON_LANE_NAMES:
        _lane_dir(workspace, f"{name}/logs/1.out")
    _lane_dir(workspace, "era5_2026010100/array_logs/x/3_1.out")  # positive control

    result = _run(store, workspace)

    assert set(_lane_entries(result.deleted, workspace)) == {"era5_2026010100"}
    # Root-level keys only: ``runs/logs`` is the unchanged runs/ lane's own
    # unparseable_run_cycle skip for the seeded ``runs/logs/1.out``.
    assert {key for key in _all_keys(result, workspace) if "/" not in key} == {"era5_2026010100"}
    for name in NON_LANE_NAMES:
        assert (workspace / name / "logs/1.out").read_bytes() == PAYLOAD, name


# --- 2.6 ---------------------------------------------------------------------
def test_symlinks_and_files_are_not_followed_and_runs_symlink_does_not_stop_the_lane(
    tmp_path: Path, roots
) -> None:
    store, workspace = roots
    outside = _real_dir(tmp_path, "outside")
    _lane_dir(outside, "ifs_target/manifests/forcing_index_x.json")
    outside_run = _seed_run_workspace(outside, NOW - timedelta(days=90))
    _backdate(outside)
    (workspace / "ifs_2026010100").symlink_to(outside / "ifs_target", target_is_directory=True)
    _write(workspace, "gfs_2026010100", PAYLOAD)
    os.utime(workspace / "gfs_2026010100", (IDLE_MTIME, IDLE_MTIME))
    (workspace / "runs").symlink_to(outside / "runs", target_is_directory=True)
    _lane_dir(workspace, "era5_2026010100/array_logs/x/3_1.out")  # positive control

    result = _run(store, workspace)

    # the runs/ lane is refused exactly as before ...
    assert [
        (entry["key"], entry["reason"])
        for entry in result.skipped
        if entry["root"] == str(workspace)
    ] == [("runs", "runs_root_symlink_skipped")]
    # ... while the Slurm log lane on the same root still ran
    assert set(_lane_entries(result.deleted, workspace)) == {"era5_2026010100"}
    assert not (workspace / "era5_2026010100").exists()
    assert _all_keys(result, workspace) == {"runs", "era5_2026010100"}
    assert (workspace / "ifs_2026010100").is_symlink()
    assert (outside / "ifs_target/manifests/forcing_index_x.json").read_bytes() == PAYLOAD
    assert (workspace / "gfs_2026010100").read_bytes() == PAYLOAD
    assert (outside / outside_run / "output/out.nc").is_file()


def test_a_descendant_symlink_is_neither_followed_nor_counted_as_recent(tmp_path: Path, roots) -> None:
    store, workspace = roots
    outside = _real_dir(tmp_path, "outside")
    _write(outside, "live/9.out", PAYLOAD)  # fresh mtime, outside W
    lane = _lane_dir(workspace, "gfs_2026010100/logs/1.out", backdate=False)
    (lane / "logs/live").symlink_to(outside / "live", target_is_directory=True)
    _backdate(lane)

    result = _run(store, workspace)

    assert _lane_entries(result.deleted, workspace)["gfs_2026010100"]["size_bytes"] == len(PAYLOAD)
    assert not lane.exists()
    assert (outside / "live/9.out").read_bytes() == PAYLOAD


# --- 2.7 ---------------------------------------------------------------------
def _seed_parity_tree(workspace: Path) -> None:
    for rel_file in LANE_SHAPES.values():
        _lane_dir(workspace, rel_file)
    _seed_run_workspace(workspace, NOW - timedelta(days=40))


def _comparable(result) -> tuple:
    return (result.planned, result.deleted, result.skipped, result.failed, result.freed_bytes)


@pytest.mark.parametrize(
    ("gate_open", "named"),
    [(False, True), (True, False)],
    ids=["gate-closed", "slurm-log-root-none"],
)
def test_closed_gate_or_unnamed_root_keeps_the_plan_key_for_key(roots, gate_open, named) -> None:
    store, workspace = roots
    _seed_parity_tree(workspace)
    config = replace(EXTRA_CONFIG, extra_roots_enabled=gate_open)
    common = {
        "object_store_root": store,
        "now": NOW,
        "config": config,
        "active_lower_bound": BELOW_AGED,
        "runs_only_roots": (workspace,),
    }

    with_keyword = run_retention(**common, slurm_log_root=workspace if named else None)
    without_keyword = run_retention(**common)

    assert _comparable(with_keyword) == _comparable(without_keyword)
    assert LANE_TARGET not in {entry["reason"] for entry in with_keyword.planned}


def test_the_lane_applies_only_to_the_workspace_root(tmp_path: Path, roots) -> None:
    store, workspace = roots
    copyback = _real_dir(tmp_path, "copyback")
    for root in (store, workspace, copyback):
        _lane_dir(root, "gfs_2026010100/manifests/forcing_index_x.json")
        _lane_dir(root, "fcst_ifs_2026010100_m1/logs/2.err")

    result = _run(
        store,
        workspace,
        runs_only_roots=(workspace, copyback),
        copyback_root=copyback,
    )

    assert result.extra_roots == [str(workspace), str(copyback)]
    lane_roots = {entry["root"] for entry in result.planned if entry["reason"] == LANE_TARGET}
    assert lane_roots == {str(workspace)}
    assert set(_lane_entries(result.deleted, workspace)) == {"gfs_2026010100", "fcst_ifs_2026010100_m1"}
    for root in (store, copyback):
        assert _all_keys(result, root) == set(), root
        assert (root / "gfs_2026010100/manifests/forcing_index_x.json").is_file()
        assert (root / "fcst_ifs_2026010100_m1/logs/2.err").is_file()


def test_a_workspace_root_aliasing_the_copyback_root_gets_no_lane(tmp_path: Path, roots) -> None:
    store, workspace = roots
    alias = tmp_path.resolve() / "copyback-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    _lane_dir(workspace, "gfs_2026010100/manifests/forcing_index_x.json")
    run_key = _seed_run_workspace(workspace, NOW - timedelta(days=40))

    result = _run(
        store,
        workspace,
        config=EXTRA_CONFIG,
        runs_only_roots=(workspace, alias),
        copyback_root=alias,
    )

    assert result.extra_roots == [str(workspace)]
    assert {entry["key"] for entry in result.planned} == {run_key}  # the root is still swept
    assert "gfs_2026010100" not in _all_keys(result, workspace)


@pytest.mark.parametrize("case", ["relative", "blank", "empty", "not-admitted", "primary"])
def test_an_unusable_or_unadmitted_slurm_log_root_gets_no_lane(
    tmp_path: Path, roots, monkeypatch, case: str
) -> None:
    store, workspace = roots
    copyback = _real_dir(tmp_path, "copyback")
    for root in (store, workspace):
        _lane_dir(root, "gfs_2026010100/manifests/forcing_index_x.json")
    # A relative value would resolve onto W from here; it must not.
    monkeypatch.chdir(tmp_path.resolve())
    named, runs_only = {
        "relative": ("workspace", (workspace,)),
        "blank": ("   ", (workspace,)),
        "empty": ("", (workspace,)),
        "not-admitted": (workspace, (copyback,)),
        "primary": (store, (store, workspace)),
    }[case]

    result = _run(store, workspace, runs_only_roots=runs_only, slurm_log_root=named)
    baseline = _run(store, workspace, config=EXTRA_CONFIG, runs_only_roots=runs_only, slurm_log_root=None)

    assert LANE_TARGET not in {entry["reason"] for entry in result.planned}
    assert result.planned == baseline.planned
    assert result.skipped == baseline.skipped
    for root in (store, workspace):
        assert (root / "gfs_2026010100/manifests/forcing_index_x.json").is_file()


@pytest.mark.parametrize("case", ["relative", "blank", "not-admitted", "primary"])
def test_plan_retention_ignores_an_unusable_or_unadmitted_slurm_log_root(
    tmp_path: Path, roots, monkeypatch, case: str
) -> None:
    """The same rules at the direct ``plan_retention`` seam, which a caller can
    reach without ``run_retention``'s own pre-filtering."""
    store, workspace = roots
    copyback = _real_dir(tmp_path, "copyback")
    for root in (store, workspace):
        _lane_dir(root, "gfs_2026010100/manifests/forcing_index_x.json")
    monkeypatch.chdir(tmp_path.resolve())
    named, runs_only = {
        "relative": ("workspace", (workspace,)),
        "blank": ("   ", (workspace,)),
        "not-admitted": (workspace, (copyback,)),
        "primary": (store, (store, workspace)),
    }[case]

    def plan(slurm_log_root):
        return plan_retention(
            object_store_root=store,
            cutoff=NOW - timedelta(days=14),
            retention_days=14,
            enabled=True,
            dry_run=True,
            active_lower_bound=BELOW_AGED,
            runs_only_roots=runs_only,
            extra_roots_cutoff=EXTRA_CUTOFF,
            extra_roots_enabled=True,
            slurm_log_root=slurm_log_root,
        )

    result = plan(named)

    assert LANE_TARGET not in {entry["reason"] for entry in result.planned}
    assert _comparable(result) == _comparable(plan(None))
    if workspace in runs_only:  # positive control: naming W itself does carry the lane
        assert LANE_TARGET in {entry["reason"] for entry in plan(workspace).planned}


# --- 2.10 --------------------------------------------------------------------
def test_a_failed_lane_removal_is_isolated(roots, monkeypatch) -> None:
    store, workspace = roots
    _lane_dir(workspace, "gfs_2026010100/manifests/forcing_index_x.json")
    _lane_dir(workspace, "ifs_2026010100/array_logs/x/3_1.out")
    real_remove = retention_mutex_mod.remove_tree_allow_symlinks

    def failing_remove(parent, name, **kwargs):
        if name == "gfs_2026010100":
            raise SafeFilesystemError(f"boom on {Path(parent) / name}", kind="io")
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(retention_mutex_mod, "remove_tree_allow_symlinks", failing_remove)

    result = _run(store, workspace)

    failed = _lane_entries(result.failed, workspace)
    assert set(failed) == {"gfs_2026010100"}
    assert failed["gfs_2026010100"]["reason"] == LANE_TARGET
    assert "boom on" in failed["gfs_2026010100"]["error"]
    assert set(_lane_entries(result.deleted, workspace)) == {"ifs_2026010100"}
    assert (workspace / "gfs_2026010100").is_dir()
    assert not (workspace / "ifs_2026010100").exists()
    assert result.freed_bytes == len(PAYLOAD)


# --- 2.11 --------------------------------------------------------------------
def test_a_root_nested_in_a_lane_directory_is_a_root_overlap(roots) -> None:
    store, workspace = roots
    _lane_dir(workspace, "era5_2026010100/array_logs/x/3_1.out")  # positive control
    in_lane = workspace / "gfs_2026010100" / "nested"
    outside_lane = workspace / "cmfd_2026010100" / "nested"

    result = _run(store, workspace, config=EXTRA_CONFIG, runs_only_roots=(workspace, in_lane, outside_lane))

    assert result.extra_roots == [str(workspace), str(outside_lane)]
    assert [entry for entry in result.skipped if entry["reason"] == "root_overlap"] == [
        {
            "key": "runs",
            "root": str(in_lane),
            "reason": "root_overlap",
            "conflicting_root": str(workspace),
        }
    ]
    assert set(_lane_entries(result.planned, workspace)) == {"era5_2026010100"}


def test_the_first_admitted_root_wins_against_the_lane_from_either_side(roots) -> None:
    """Containment is classified from both sides: a lane-nested root admitted
    first makes the workspace root itself the loser."""
    store, workspace = roots
    _lane_dir(workspace, "era5_2026010100/array_logs/x/3_1.out")
    in_lane = workspace / "gfs_2026010100" / "nested"

    result = _run(store, workspace, config=EXTRA_CONFIG, runs_only_roots=(in_lane, workspace))

    assert result.extra_roots == [str(in_lane)]
    assert [entry for entry in result.skipped if entry["reason"] == "root_overlap"] == [
        {
            "key": "runs",
            "root": str(workspace),
            "reason": "root_overlap",
            "conflicting_root": str(in_lane),
        }
    ]
    assert result.planned == []


def test_a_primary_named_as_the_slurm_log_root_keeps_its_admission_rules(roots) -> None:
    """The lane's overlap shape never attaches to the primary root, even when
    the primary is the value named as the Slurm log root."""
    store, _workspace = roots
    in_lane = store / "gfs_2026010100" / "nested"

    result = _run(store, store, config=EXTRA_CONFIG, runs_only_roots=(store, in_lane))

    assert result.extra_roots == [str(in_lane)]
    assert [entry for entry in result.skipped if entry["reason"] == "root_overlap"] == []
