"""Requirement-driven tests for primary admission, overlap and contained unlink (#1616, #1617, #1615)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from services.orchestrator.retention import (
    PRIMARY_ROOT_BLANK_REASON,
    PRIMARY_ROOT_NOT_ABSOLUTE_REASON,
    ROOT_OVERLAP_REASON,
    RetentionConfig,
    plan_retention,
    run_retention,
)
from tests.retention_test_helpers import (
    EXTRA_CONFIG,
    NOW,
    _cycle_name,
    _entries_for,
    _keys,
    _pass_scheduler,
    _run_id,
    _seed_pass_env,
    _seed_run_workspace,
    _write,
)

# ===========================================================================
# Issue #1616 -- primary root admission hygiene (shared with additional roots)
#
# ``Path("").expanduser().resolve()`` is the process working directory, so an
# explicitly blank or relative ``OBJECT_STORE_ROOT`` used to become a deletion
# surface derived from the CWD (direct API) or from the scheduler workspace
# (pass, whose normalization anchors relative values beneath it). None stays an
# ordinary unset no-op; every explicit invalid value must be rejected BEFORE
# any scan with a readable reason token, and no CWD/workspace-derived tree may
# be touched.
# ===========================================================================

# Canonical run id whose cycle is far past every cutoff used below.
_OLD_RUN = _run_id(NOW - timedelta(days=90))


def _assert_primary_rejected(result, *, reason: str, raw: str) -> None:
    """The shared shape of an invalid-primary receipt.

    ``raw`` is the stripped configured value -- the same convention the
    additional-root lane uses for its recorded values.
    """
    assert result.planned == []
    assert result.deleted == []
    assert result.failed == []
    assert result.freed_bytes == 0
    assert [(entry["key"], entry["root"], entry["reason"]) for entry in result.skipped] == [
        ("", raw, reason)
    ]


@pytest.mark.parametrize("value", ["", "   ", "\t  "])
def test_blank_primary_root_is_rejected_before_any_scan(tmp_path: Path, monkeypatch, value: str) -> None:
    """[3.1] An explicitly blank primary never resolves to the CWD and never
    becomes a scan/delete surface; the receipt records ``primary_root_blank``."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    _write(cwd, f"raw/gfs/{old_cycle}/gfs.f000.nc")
    _write(cwd, f"runs/{_OLD_RUN}/output/out.nc")

    result = run_retention(
        object_store_root=value,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )

    _assert_primary_rejected(result, reason=PRIMARY_ROOT_BLANK_REASON, raw=value.strip())
    # physical bytes survive under the CWD
    assert (cwd / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (cwd / f"runs/{_OLD_RUN}/output/out.nc").exists()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_primary_root_via_plan_retention_records_blank_reason(
    tmp_path: Path, monkeypatch, value: str
) -> None:
    """[3.1] The plan seam carries the same rejection as the run seam."""
    monkeypatch.chdir(tmp_path)

    result = plan_retention(
        object_store_root=value,
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=False,
    )

    _assert_primary_rejected(result, reason=PRIMARY_ROOT_BLANK_REASON, raw=value.strip())


@pytest.mark.parametrize("value", ["relative/store", "  relative/store  "])
def test_relative_primary_root_is_rejected_before_any_scan(tmp_path: Path, monkeypatch, value: str) -> None:
    """[3.1] A relative primary never resolves against the CWD (direct seam) and
    records ``primary_root_not_absolute``."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    _write(cwd, f"raw/gfs/{old_cycle}/gfs.f000.nc")
    _write(cwd, f"runs/{_OLD_RUN}/output/out.nc")

    result = run_retention(
        object_store_root=value,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )

    _assert_primary_rejected(result, reason=PRIMARY_ROOT_NOT_ABSOLUTE_REASON, raw=value.strip())
    assert (cwd / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (cwd / f"runs/{_OLD_RUN}/output/out.nc").exists()


def test_none_primary_root_stays_a_quiet_noop(tmp_path: Path) -> None:
    """[3.1] None keeps the historical unset no-op: no skip entry, no plan."""
    result = run_retention(
        object_store_root=None,
        now=NOW,
        config=RetentionConfig(enabled=True, dry_run=False, retention_days=14),
    )
    assert result.planned == []
    assert result.deleted == []
    assert result.skipped == []


# ===========================================================================
# Issue #1616 -- scheduler pass + cleanup CLI raw primary input
#
# The pass hands retention the constructor-time RAW ``OBJECT_STORE_ROOT``
# (``_object_store_root_raw``) so env-default and programmatic construction
# both surface an explicitly blank/relative value; the cleanup CLI already
# passes the raw env value. The old trees under the CWD and under the
# scheduler-normalized workspace location must survive and the receipt must
# carry the exact primary reason token.
# ===========================================================================

def _seed_pass_raw_primary_old_trees(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Old retention-shaped trees under the CWD and under the scheduler
    workspace-derived location; returns (cwd, normalized_store)."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    workspace = tmp_path / "ws"
    # where the relative value normalizes to: <workspace>/<relative/store>
    normalized_store = workspace / "relative" / "store"
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    _write(cwd, f"raw/gfs/{old_cycle}/gfs.f000.nc")
    _write(cwd, f"runs/{_OLD_RUN}/output/out.nc")
    _write(normalized_store, f"raw/gfs/{old_cycle}/gfs.f000.nc")
    _write(normalized_store, f"runs/{_OLD_RUN}/output/out.nc")
    return cwd, normalized_store


@pytest.mark.parametrize("value", ["", "   ", "relative/store"])
def test_pass_raw_primary_blank_or_relative_is_never_scanned(
    tmp_path: Path, monkeypatch, value: str
) -> None:
    """[3.2] Scheduler pass: the raw constructor-time value reaches retention,
    both the CWD and the normalized-workspace old trees survive, and ``skipped``
    carries the exact primary reason token."""
    _seed_pass_env(monkeypatch)
    monkeypatch.setenv("OBJECT_STORE_ROOT", value)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    cwd, normalized_store = _seed_pass_raw_primary_old_trees(tmp_path, monkeypatch)

    scheduler = _pass_scheduler(workspace_root=str(tmp_path / "ws"))
    payload = scheduler._run_retention(NOW)

    expected_reason = (
        PRIMARY_ROOT_BLANK_REASON if value.strip() == "" else PRIMARY_ROOT_NOT_ABSOLUTE_REASON
    )
    assert payload["status"] == "completed"
    assert [(entry["key"], entry["reason"]) for entry in payload["skipped"]] == [
        ("", expected_reason)
    ]
    assert payload["planned"] == []
    assert payload["deleted"] == []
    # physical bytes under both old-tree locations survive
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    assert (cwd / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (cwd / f"runs/{_OLD_RUN}/output/out.nc").exists()
    assert (normalized_store / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (normalized_store / f"runs/{_OLD_RUN}/output/out.nc").exists()


def test_pass_explicit_blank_primary_is_rejected_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    """[3.2] Programmatic construction: an explicitly blank constructor value is
    captured raw (it would otherwise normalize to None and read as an unset
    quiet no-op with no reason record)."""
    _seed_pass_env(monkeypatch)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    cwd, normalized_store = _seed_pass_raw_primary_old_trees(tmp_path, monkeypatch)

    scheduler = _pass_scheduler(
        workspace_root=str(tmp_path / "ws"),
        object_store_root="",
    )
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert [(entry["key"], entry["reason"]) for entry in payload["skipped"]] == [
        ("", PRIMARY_ROOT_BLANK_REASON)
    ]
    assert payload["planned"] == []
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    assert (cwd / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (normalized_store / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()


def test_pass_explicit_relative_primary_is_rejected_not_workspace_anchored(
    tmp_path: Path, monkeypatch
) -> None:
    """[3.2] Programmatic construction with a relative value: the normalized
    config would anchor it beneath the workspace, but the raw value must win for
    retention."""
    _seed_pass_env(monkeypatch)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    cwd, normalized_store = _seed_pass_raw_primary_old_trees(tmp_path, monkeypatch)

    scheduler = _pass_scheduler(
        workspace_root=str(tmp_path / "ws"),
        object_store_root="relative/store",
    )
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert [(entry["key"], entry["reason"]) for entry in payload["skipped"]] == [
        ("", PRIMARY_ROOT_NOT_ABSOLUTE_REASON)
    ]
    assert payload["planned"] == []
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    assert (cwd / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()
    assert (normalized_store / f"raw/gfs/{old_cycle}/gfs.f000.nc").exists()


def test_absolute_configured_primary_still_works_in_a_pass(tmp_path: Path, monkeypatch) -> None:
    """[3.2] An ordinary absolute configured primary stays functional: old cycles
    are planned/deleted exactly as before."""
    _seed_pass_env(monkeypatch)
    store = tmp_path / "object-store"
    store.mkdir()
    old_cycle = _cycle_name(NOW - timedelta(days=20))
    expired = _write(store, f"raw/gfs/{old_cycle}/gfs.f000.nc").parent

    scheduler = _pass_scheduler(workspace_root=str(tmp_path / "ws"), object_store_root=str(store))
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert not (store / f"raw/gfs/{old_cycle}").exists()
    assert not payload["skipped"]
    assert any(entry["key"] == f"raw/gfs/{old_cycle}" for entry in payload["deleted"])
    assert expired.parent.exists()


# ===========================================================================
# Issue #1617 -- unequal ancestor/descendant root overlap is rejected
#
# The #1318 equality-only dedup could not see ``B = A/runs/<outer_run>/nested``
# beneath ``A``, so both roots were admitted and selected the same subtree
# twice: A plans ``runs/<outer_run>`` (whose byte size already includes B's
# nested tree) and B plans its own ``runs/<inner_run>`` -- a duplicate target
# and a double-counted subtree. Now: primary wins over every additional root;
# among additional roots the first accepted configured root wins. The loser is
# omitted from ``extra_roots.roots``, records ``root_overlap`` +
# ``conflicting_root``, and contributes no target. Equal aliases stay a
# silent dedup.
# ===========================================================================

_OUTER_RUN = _run_id(NOW - timedelta(days=90))
_INNER_RUN = _run_id(NOW - timedelta(days=80))
_OUTER_BYTES = len(b"outer-bytes")
_INNER_BYTES = len(b"inner-bytes")


def _seed_overlap_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    """Seed the actual #1617 duplicate-target shape; returns (ancestor, nested).

    - A = ``tmp_path/ancestor`` holds ``runs/<outer_run>/output/out.nc``.
    - B = ``A/runs/<outer_run>/nested`` holds its OWN
      ``B/runs/<inner_run>/output/out.nc`` (a second, distinct canonical run
      workspace nested inside A's outer run tree).

    Pre-change (equality-only dedup): B is admitted, so A plans
    ``runs/<outer_run>`` (whose ``_dir_size`` already includes B's inner tree)
    AND B plans ``runs/<inner_run>`` -- the inner target is planned twice and,
    depending on order, its bytes are freed twice.
    """
    ancestor = tmp_path / "ancestor"
    _write(ancestor, f"runs/{_OUTER_RUN}/output/out.nc", b"outer-bytes")
    nested = ancestor / "runs" / _OUTER_RUN / "nested"
    _write(nested, f"runs/{_INNER_RUN}/output/out.nc", b"inner-bytes")
    return ancestor, nested


def _assert_overlap_winner(
    result,
    *,
    winner: Path,
    winner_keys: set[str],
    loser: Path,
) -> None:
    """The shared shape of a deterministic overlap adjudication: the loser is
    omitted from the receipt's admitted roots, records ``root_overlap`` with
    ``conflicting_root=<winner>``, and contributes no target."""
    assert str(loser.resolve()) not in result.to_dict()["extra_roots"]["roots"]
    assert [
        (entry["key"], entry["reason"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [("runs", ROOT_OVERLAP_REASON, str(winner.resolve()))]
    assert _entries_for(result.planned, loser) == set()
    assert _entries_for(result.planned, winner) == set(winner_keys)


def test_primary_wins_over_a_nested_additional_root(tmp_path: Path) -> None:
    """[3.3] Primary A + additional B=A/runs/<outer_run>/nested: A wins, the
    loser records root_overlap with conflicting_root=A, and B's inner run is
    never planned -- the exact target A's tree already covers."""
    ancestor, nested = _seed_overlap_workspaces(tmp_path)

    result = run_retention(
        object_store_root=ancestor,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested,),
    )

    _assert_overlap_winner(
        result,
        winner=ancestor,
        winner_keys={f"runs/{_OUTER_RUN}"},
        loser=nested,
    )
    assert result.to_dict()["extra_roots"]["roots"] == []


def test_nested_additional_root_wins_when_primary_is_unrelated(tmp_path: Path) -> None:
    """[3.3] Primary X unrelated + additional A + B=A/runs/<outer_run>/nested
    in (A, B) order: A (first accepted) wins, B records root_overlap, and only
    A's outer target is planned."""
    ancestor, nested = _seed_overlap_workspaces(tmp_path)
    store = tmp_path / "object-store"
    store.mkdir()

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(ancestor, nested),
    )

    _assert_overlap_winner(
        result,
        winner=ancestor,
        winner_keys={f"runs/{_OUTER_RUN}"},
        loser=nested,
    )


def test_additional_overlap_in_descendant_then_ancestor_order(tmp_path: Path) -> None:
    """[3.3] Additional A + B with B beneath A, in BOTH configuration orders:
    the first accepted configured root wins, and only that winner's target set
    appears."""
    ancestor, nested = _seed_overlap_workspaces(tmp_path)
    store = tmp_path / "object-store"
    store.mkdir()

    ancestor_first = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(ancestor, nested),
    )
    descendant_first = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested, ancestor),
    )

    # ancestor-first: A admitted first, B rejected; only A's outer target.
    _assert_overlap_winner(
        ancestor_first,
        winner=ancestor,
        winner_keys={f"runs/{_OUTER_RUN}"},
        loser=nested,
    )
    # descendant-first: B admitted first, A rejected; only B's inner target.
    _assert_overlap_winner(
        descendant_first,
        winner=nested,
        winner_keys={f"runs/{_INNER_RUN}"},
        loser=ancestor,
    )


def test_additional_overlap_with_primary_rejected_in_both_configuration_orders(
    tmp_path: Path,
) -> None:
    """[3.3] Primary A + additional B=A/runs/<outer_run>/nested, and the reverse
    (B as primary, A as additional): the primary always wins."""
    ancestor, nested = _seed_overlap_workspaces(tmp_path)

    nested_as_extra = run_retention(
        object_store_root=ancestor,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested,),
    )
    _assert_overlap_winner(
        nested_as_extra,
        winner=ancestor,
        winner_keys={f"runs/{_OUTER_RUN}"},
        loser=nested,
    )

    nested_as_primary = run_retention(
        object_store_root=nested,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(ancestor,),
    )
    _assert_overlap_winner(
        nested_as_primary,
        winner=nested,
        winner_keys={f"runs/{_INNER_RUN}"},
        loser=ancestor,
    )


def test_overlap_loser_contributes_no_freed_bytes(tmp_path: Path) -> None:
    """[3.3] Execute in both configuration orders: the winner's target is
    deleted exactly once, no second target is ever attempted, and freed_bytes
    equals the physical unique bytes of the winner's admitted tree (independent
    of any receipt size bookkeeping)."""
    ancestor, nested = _seed_overlap_workspaces(tmp_path)
    store = tmp_path / "object-store"
    store.mkdir()

    ancestor_first = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(ancestor, nested),
    )
    # A wins: its outer tree (which physically contains B's inner tree) is
    # removed once; B is never planned, so nothing fails on a missing target.
    assert _entries_for(ancestor_first.deleted, ancestor) == {f"runs/{_OUTER_RUN}"}
    assert _entries_for(ancestor_first.deleted, nested) == set()
    assert ancestor_first.failed == []
    assert ancestor_first.freed_bytes == _OUTER_BYTES + _INNER_BYTES
    assert not (ancestor / f"runs/{_OUTER_RUN}").exists()

    # Re-seed for the second order.
    ancestor2, nested2 = _seed_overlap_workspaces(tmp_path)
    descendant_first = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(nested2, ancestor2),
    )
    # B wins: only B's inner run is removed; A's outer tree (loser) survives.
    assert _entries_for(descendant_first.deleted, nested2) == {f"runs/{_INNER_RUN}"}
    assert _entries_for(descendant_first.deleted, ancestor2) == set()
    assert descendant_first.failed == []
    assert descendant_first.freed_bytes == _INNER_BYTES
    assert (ancestor2 / f"runs/{_OUTER_RUN}/output/out.nc").read_bytes() == b"outer-bytes"
    assert not (nested2 / f"runs/{_INNER_RUN}").exists()


def test_equal_root_aliases_remain_a_silent_dedup(tmp_path: Path) -> None:
    """[3.3] Equal path / symlink / trailing-slash aliases still sweep once with
    no overlap record -- #1318 compatibility."""
    store = tmp_path / "object-store"
    key = _seed_run_workspace(store, NOW - timedelta(days=40))
    alias = tmp_path / "alias"
    alias.symlink_to(store, target_is_directory=True)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(store, alias, str(store) + "/"),
    )

    assert [entry["key"] for entry in result.planned] == [key]
    assert [entry["key"] for entry in result.deleted] == [key]
    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert result.to_dict()["extra_roots"]["roots"] == []


# ===========================================================================
# Issue #1617 -- potential-target intersection (round-1 cand-01 fix)
#
# Directory ancestry alone is NOT overlap. The documented layout
# ``WORKSPACE_ROOT=/work/nhms`` + ``OBJECT_STORE_ROOT=/work/nhms/object-store``
# has disjoint ``runs/`` and cycle lanes, so both roots are admitted and each
# canonical run is reclaimed once. A root is rejected only when it lies at or
# below another admitted root's POTENTIAL deletion target: ``runs/<canonical>``
# on every root, plus ``raw|canonical|forcing/<source>/<valid_cycle>`` on the
# primary -- even when that target is currently within its window.
# ===========================================================================

_RUN_WORKSPACE_BYTES = len(b"run-bytes") * 4  # _seed_run_workspace writes four files


def test_parent_workspace_and_child_object_store_are_both_admitted(tmp_path: Path) -> None:
    """[3.3b] Documented topology: workspace parent + child object-store primary.
    Disjoint runs/ lanes -> both admitted, each run reclaimed once, no overlap,
    freed_bytes equals the unique physical bytes of both run workspaces."""
    parent = tmp_path / "work"
    store = parent / "object-store"
    parent_key = _seed_run_workspace(parent, NOW - timedelta(days=40))
    store_key = _seed_run_workspace(store, NOW - timedelta(days=50))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(parent,),
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert result.to_dict()["extra_roots"]["roots"] == [str(parent.resolve())]
    assert _entries_for(result.deleted, parent) == {parent_key}
    assert _entries_for(result.deleted, store) == {store_key}
    assert result.freed_bytes == 2 * _RUN_WORKSPACE_BYTES
    assert not (parent / parent_key).exists()
    assert not (store / store_key).exists()


def test_pass_admits_parent_workspace_and_child_object_store(
    tmp_path: Path, monkeypatch
) -> None:
    """[3.3b] The pass seam (raw constructor primary + WORKSPACE_ROOT extra):
    the parent workspace and the child object-store are both admitted and both
    aged run trees are deleted."""
    _seed_pass_env(monkeypatch)
    parent = tmp_path / "work"
    store = parent / "object-store"
    parent_key = _seed_run_workspace(parent, NOW - timedelta(days=40))
    store_key = _seed_run_workspace(store, NOW - timedelta(days=50))
    monkeypatch.setenv("WORKSPACE_ROOT", str(parent))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(store))
    monkeypatch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", raising=False)

    scheduler = _pass_scheduler(workspace_root=str(parent), object_store_root=str(store))
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in payload["skipped"])
    assert payload["extra_roots"]["roots"] == [str(parent.resolve())]
    assert _entries_for(payload["deleted"], parent) == {parent_key}
    assert _entries_for(payload["deleted"], store) == {store_key}
    assert payload["freed_bytes"] == 2 * _RUN_WORKSPACE_BYTES
    assert not (parent / parent_key).exists()
    assert not (store / store_key).exists()


@pytest.mark.parametrize("order", ["parent-first", "child-first"])
def test_parent_child_additional_roots_outside_canonical_lane_both_admitted(
    tmp_path: Path, order: str
) -> None:
    """[3.3b] Two additional roots where one is an ordinary child of the other
    (not inside any canonical run target): both orders admit both and delete
    each aged run independently."""
    store = tmp_path / "object-store"
    store.mkdir()
    parent = tmp_path / "parent"
    child = parent / "child"
    parent_key = _seed_run_workspace(parent, NOW - timedelta(days=40))
    child_key = _seed_run_workspace(child, NOW - timedelta(days=50))
    roots = (parent, child) if order == "parent-first" else (child, parent)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=roots,
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert set(result.to_dict()["extra_roots"]["roots"]) == {
        str(parent.resolve()),
        str(child.resolve()),
    }
    assert _entries_for(result.deleted, parent) == {parent_key}
    assert _entries_for(result.deleted, child) == {child_key}
    assert result.freed_bytes == 2 * _RUN_WORKSPACE_BYTES
    assert not (parent / parent_key).exists()
    assert not (child / child_key).exists()


@pytest.mark.parametrize("prefix", ["raw", "canonical", "forcing"])
@pytest.mark.parametrize("path_cycle_age_days", [100, 3])
def test_extra_root_under_primary_cycle_target_is_rejected(
    tmp_path: Path, prefix: str, path_cycle_age_days: int
) -> None:
    """[3.3] An additional root at/below the primary's potential cycle target is
    rejected even when that cycle is currently inside the primary window (age
    3) -- admission is about the potential deletion tree, not the current plan."""
    store = tmp_path / "object-store"
    store.mkdir()
    path_cycle = _cycle_name(NOW - timedelta(days=path_cycle_age_days))
    nested = store / prefix / "gfs" / path_cycle / "nested"
    # A plan-worthy aged run under the nested root proves the rejection is not
    # about a missing target: if the root were admitted it would be planned.
    _seed_run_workspace(nested, NOW - timedelta(days=90))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested,),
    )

    assert str(nested.resolve()) not in result.to_dict()["extra_roots"]["roots"]
    assert [
        (entry["key"], entry["reason"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [("runs", ROOT_OVERLAP_REASON, str(store.resolve()))]
    assert _entries_for(result.planned, nested) == set()


def test_extra_root_under_primary_non_cycle_directory_is_admitted(tmp_path: Path) -> None:
    """[3.3b] ``A/raw/gfs/not-a-cycle/nested`` is NOT a potential cycle target,
    so the extra root is admitted and its own aged run is planned."""
    store = tmp_path / "object-store"
    store.mkdir()
    nested = store / "raw" / "gfs" / "not-a-cycle" / "nested"
    nested_key = _seed_run_workspace(nested, NOW - timedelta(days=90))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested,),
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert str(nested.resolve()) in result.to_dict()["extra_roots"]["roots"]
    assert _entries_for(result.planned, nested) == {nested_key}


def test_primary_under_candidate_extra_run_target_rejects_extra(tmp_path: Path) -> None:
    """[3.3] Reverse geometry: the primary lies inside the candidate extra's
    canonical run target. The extra is rejected (primary precedence is fixed),
    with conflicting_root naming the primary, and the primary still plans its
    own tree."""
    parent = tmp_path / "w"
    run_id = _run_id(NOW - timedelta(days=90))
    primary = parent / "runs" / run_id / "nested"
    primary_key = _seed_run_workspace(primary, NOW - timedelta(days=90))
    # The parent also holds an aged canonical run under its own runs/ tree --
    # the exact tree the parent's potential target would cover if admitted.
    _seed_run_workspace(parent, NOW - timedelta(days=80))

    result = run_retention(
        object_store_root=primary,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(parent,),
    )

    assert str(parent.resolve()) not in result.to_dict()["extra_roots"]["roots"]
    assert [
        (entry["key"], entry["reason"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [("runs", ROOT_OVERLAP_REASON, str(primary.resolve()))]
    assert _entries_for(result.planned, primary) == {primary_key}


# ---------------------------------------------------------------------------
# Round-2 boundary fenceposts (cand-02): exact-at targets and role gating.
# Every fixture seeds a real aged canonical run under the candidate root so a
# wrong admission decision is observable at the public run_retention seam.
# ---------------------------------------------------------------------------

def test_exact_at_run_target_extra_is_rejected(tmp_path: Path) -> None:
    """[3.3] Candidate extra root EXACTLY ``A/runs/<canonical_run_id>`` (no
    ``/nested``): the run dir is the winner's deletable target itself, so the
    loser is rejected with ``root_overlap`` + ``conflicting_root`` and no loser
    plan -- even though the candidate holds its own aged inner run."""
    store = tmp_path / "object-store"
    store.mkdir()
    outer = _run_id(NOW - timedelta(days=90))
    exact = store / "runs" / outer
    inner_key = _seed_run_workspace(exact, NOW - timedelta(days=80))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(exact,),
    )

    assert str(exact.resolve()) not in result.to_dict()["extra_roots"]["roots"]
    assert [
        (entry["key"], entry["reason"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [("runs", ROOT_OVERLAP_REASON, str(store.resolve()))]
    # the candidate's aged run would have been planned had it been admitted
    assert _entries_for(result.planned, exact) == set()
    assert inner_key not in _keys(result.planned)


@pytest.mark.parametrize("prefix", ["raw", "canonical", "forcing"])
def test_exact_at_primary_cycle_target_extra_is_rejected(
    tmp_path: Path, prefix: str
) -> None:
    """[3.3] Candidate extra root EXACTLY
    ``PRIMARY/<prefix>/<source>/<valid_cycle>`` (no ``/nested``): the cycle dir
    is the primary's deletable target itself, so the loser is rejected with
    ``root_overlap`` + ``conflicting_root`` and no loser plan."""
    store = tmp_path / "object-store"
    store.mkdir()
    path_cycle = _cycle_name(NOW - timedelta(days=30))
    exact = store / prefix / "gfs" / path_cycle
    inner_key = _seed_run_workspace(exact, NOW - timedelta(days=80))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(exact,),
    )

    assert str(exact.resolve()) not in result.to_dict()["extra_roots"]["roots"]
    assert [
        (entry["key"], entry["reason"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [("runs", ROOT_OVERLAP_REASON, str(store.resolve()))]
    assert _entries_for(result.planned, exact) == set()
    assert inner_key not in _keys(result.planned)


def test_root_exactly_runs_is_admitted(tmp_path: Path) -> None:
    """[3.3b] A candidate exactly at ``A/runs`` is ordinary ancestry -- ``runs``
    alone is not a deletable run workspace, so parent and child are both
    admitted and each aged run is planned once, no overlap."""
    store = tmp_path / "object-store"
    store.mkdir()
    runs = store / "runs"
    runs_key = _seed_run_workspace(runs, NOW - timedelta(days=90))
    store_key = _seed_run_workspace(store, NOW - timedelta(days=80))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(runs,),
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert str(runs.resolve()) in result.to_dict()["extra_roots"]["roots"]
    assert _entries_for(result.planned, runs) == {runs_key}
    assert _entries_for(result.planned, store) == {store_key}


def test_root_under_noncanonical_run_dir_is_admitted(tmp_path: Path) -> None:
    """[3.3b] A candidate under ``A/runs/<not-a-run-id>`` is not inside a
    deletable run workspace: both roots admit and the candidate's real aged
    canonical run is planned, no overlap."""
    store = tmp_path / "object-store"
    store.mkdir()
    nested = store / "runs" / "not-a-run-id" / "nested"
    nested_key = _seed_run_workspace(nested, NOW - timedelta(days=90))
    store_key = _seed_run_workspace(store, NOW - timedelta(days=80))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=(nested,),
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert str(nested.resolve()) in result.to_dict()["extra_roots"]["roots"]
    assert _entries_for(result.planned, nested) == {nested_key}
    assert _entries_for(result.planned, store) == {store_key}


@pytest.mark.parametrize("order", ["extra-first", "child-first"])
@pytest.mark.parametrize("prefix", ["raw", "canonical", "forcing"])
def test_cycle_shaped_ancestry_under_an_extra_is_admitted(
    tmp_path: Path, order: str, prefix: str
) -> None:
    """[3.3b] Additional roots are runs-only: a valid cycle-shaped subtree under
    extra A is NOT a potential target, so A and child B (both orders) are
    admitted and each own aged run is planned once, no overlap. This pins the
    ``primary`` role gate -- treating an extra as cycle-owning would reject B."""
    store = tmp_path / "object-store"
    store.mkdir()
    extra_a = tmp_path / "extra-a"
    path_cycle = _cycle_name(NOW - timedelta(days=30))
    child_b = extra_a / prefix / "gfs" / path_cycle / "nested"
    a_key = _seed_run_workspace(extra_a, NOW - timedelta(days=90))
    b_key = _seed_run_workspace(child_b, NOW - timedelta(days=80))
    roots = (extra_a, child_b) if order == "extra-first" else (child_b, extra_a)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=True),
        runs_only_roots=roots,
    )

    assert not any(entry["reason"] == ROOT_OVERLAP_REASON for entry in result.skipped)
    assert set(result.to_dict()["extra_roots"]["roots"]) == {
        str(extra_a.resolve()),
        str(child_b.resolve()),
    }
    assert _entries_for(result.planned, extra_a) == {a_key}
    assert _entries_for(result.planned, child_b) == {b_key}


# ===========================================================================
# Issue #1615 -- contained additional-root deletion unlinks descendant links
#
# A selected additional-root run workspace containing top-level AND nested
# symlinks is reclaimed in one pass; the links are unlinked, never followed,
# and their external targets stay byte-identical. A second pass over the same
# topology neither plans nor fails that workspace (no infinite retry).
# ===========================================================================

def test_run_with_top_level_and_nested_links_is_reclaimed_in_one_pass_and_second_pass(
    tmp_path: Path,
) -> None:
    """[3.4][3.7] One top-level link + one nested link to byte fixtures outside
    the root: the FIRST pass deletes the whole run, both targets survive
    byte-identical; the SECOND pass has no planned/failed/deleted entry for it."""
    store = tmp_path / "object-store"
    store.mkdir()
    outside = tmp_path / "outside"
    top_target = _write(outside, "top/data.nc", b"top-bytes")
    nested_target = _write(outside, "nested/data.nc", b"nested-bytes")
    workspace = tmp_path / "workspace"
    run_id = _run_id(NOW - timedelta(days=40))
    run_dir = workspace / "runs" / run_id
    (run_dir / "output").mkdir(parents=True)
    (run_dir / "output/out.nc").write_bytes(b"run-bytes")
    (run_dir / "subdir").mkdir()
    # one top-level link + one nested link
    (run_dir / "top-link").symlink_to(top_target, target_is_directory=False)
    (run_dir / "subdir" / "nested-link").symlink_to(nested_target, target_is_directory=False)

    config = replace(EXTRA_CONFIG, dry_run=False)

    first = run_retention(
        object_store_root=store,
        now=NOW,
        config=config,
        runs_only_roots=(workspace,),
    )
    assert _entries_for(first.deleted, workspace) == {f"runs/{run_id}"}
    assert _entries_for(first.failed, workspace) == set()
    # whole run workspace gone in one pass
    assert not run_dir.exists()
    assert (workspace / "runs").is_dir()
    # both external targets survive byte-identical
    assert top_target.read_bytes() == b"top-bytes"
    assert nested_target.read_bytes() == b"nested-bytes"
    assert first.freed_bytes > 0

    second = run_retention(
        object_store_root=store,
        now=NOW,
        config=config,
        runs_only_roots=(workspace,),
    )
    # second pass: nothing is planned, failed, or deleted for the removed run
    assert _entries_for(second.planned, workspace) == set()
    assert _entries_for(second.failed, workspace) == set()
    assert _entries_for(second.deleted, workspace) == set()
    # targets stay byte-identical across both passes
    assert top_target.read_bytes() == b"top-bytes"
    assert nested_target.read_bytes() == b"nested-bytes"
