"""Requirement-driven tests for canonical run identity and additional roots (#1405, #1318)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.orchestrator.retention import (
    PIPELINE_FRONTIER_EXEMPT_REASON,
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
    _reasons,
    _run_id,
    _seed_cycle,
    _seed_pass_env,
    _seed_run_workspace,
    _write,
)

# ---------------------------------------------------------------------------
# Issue #1405 — run-workspace deletion recognizes only canonical run identities.
#
# The previous criterion split the run id on `_` and deleted on the first token
# that happened to parse as `%Y%m%d%H`, so any stray directory carrying a
# ten-digit token was adjudicated as an expired run workspace, and a forecast
# run could bind to the wrong embedded timestamp.
# ---------------------------------------------------------------------------

_AGED = _cycle_name(NOW - timedelta(days=20))  # 2026051412, past the 14d cutoff


def _plan_runs(root: Path, *names: str, active_lower_bound: datetime | None = None):
    """Seed `runs/<name>` workspaces and return the wall-clock plan for them."""
    for name in names:
        _write(root, f"runs/{name}/output/out.nc")
    return plan_retention(
        object_store_root=root,
        cutoff=NOW - timedelta(days=14),
        retention_days=14,
        enabled=True,
        dry_run=True,
        active_lower_bound=active_lower_bound,
    )


def test_non_run_directory_with_a_timestamp_token_is_preserved(tmp_path: Path) -> None:
    """[#1405 B] A stray salvage capture is not a run workspace, so it survives."""
    root = tmp_path / "object-store"
    result = _plan_runs(root, f"manual_salvage_{_AGED}_keepme")

    key = f"runs/manual_salvage_{_AGED}_keepme"
    assert key not in _keys(result.planned)
    assert _reasons(result.skipped)[key] == "unparseable_run_cycle"


@pytest.mark.parametrize(
    "name",
    [
        f"debug_snapshot_{_AGED}",
        f"runs_{_AGED}_scratch",
        f"FCST_GFS_{_AGED}_MODEL_A",
        f"fcst_gfs_2026139999_model_{_AGED}",
    ],
    ids=["no-canonical-prefix", "foreign-writer", "uppercase", "illegal-canonical-date"],
)
def test_stray_run_directory_shapes_are_preserved(tmp_path: Path, name: str) -> None:
    """[#1405 B] Names outside the canonical shapes never enter adjudication."""
    root = tmp_path / "object-store"
    result = _plan_runs(root, name)

    assert f"runs/{name}" not in _keys(result.planned)
    assert _reasons(result.skipped)[f"runs/{name}"] == "unparseable_run_cycle"


def test_forecast_run_cycle_is_taken_from_the_canonical_position(tmp_path: Path) -> None:
    """[#1405 A] A leading timestamp-like token must not outrank the cycle slot.

    The canonical cycle here is inside the retention window, so binding to the
    stray leading `2020010100` would age the workspace out and delete it.
    """
    root = tmp_path / "object-store"
    fresh = _cycle_name(NOW - timedelta(days=3))
    name = f"fcst_2020010100_{fresh}_model_a"
    result = _plan_runs(root, name)

    assert f"runs/{name}" not in _keys(result.planned)
    assert _reasons(result.skipped)[f"runs/{name}"] == "within_retention_window"


def test_trailing_timestamp_token_does_not_shift_the_forecast_cycle(tmp_path: Path) -> None:
    """[#1405] A model id that looks like a timestamp leaves the cycle alone."""
    root = tmp_path / "object-store"
    name = f"fcst_gfs_{_AGED}_model_2026010100"
    result = _plan_runs(root, name)

    planned = {entry["key"]: entry for entry in result.planned}
    assert planned[f"runs/{name}"]["cycle_time"] == (NOW - timedelta(days=20)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.mark.parametrize(
    "name",
    [
        f"fcst_gfs_{_AGED}_model_a",
        f"cycle_gfs_{_AGED}",
        f"cycle_gfs_{_AGED}_model_a",
        f"analysis_era5_{_AGED}_2026060312_model_a",
    ],
    ids=["forecast", "cohort", "cohort-with-suffix", "analysis"],
)
def test_expired_canonical_run_workspaces_are_still_collected(tmp_path: Path, name: str) -> None:
    """[#1405 invariant] Recycling of the three canonical shapes is unchanged."""
    root = tmp_path / "object-store"
    result = _plan_runs(root, name)

    planned = {entry["key"]: entry for entry in result.planned}
    assert planned[f"runs/{name}"]["reason"] == "run_cycle_aged_out"
    # The analysis shape binds to its START timestamp, matching chain_analysis.
    assert planned[f"runs/{name}"]["cycle_time"] == (NOW - timedelta(days=20)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_canonical_run_workspaces_keep_the_two_skip_tiers(tmp_path: Path) -> None:
    """[#1405 invariant] Window and frontier adjudication order is untouched."""
    root = tmp_path / "object-store"
    aged_cycle = NOW - timedelta(days=20)
    in_window = _cycle_name(NOW - timedelta(days=3))
    result = _plan_runs(
        root,
        f"fcst_gfs_{_AGED}_model_a",
        f"fcst_gfs_{in_window}_model_a",
        active_lower_bound=aged_cycle,
    )

    reasons = _reasons(result.skipped)
    assert reasons[f"runs/fcst_gfs_{_AGED}_model_a"] == PIPELINE_FRONTIER_EXEMPT_REASON
    assert reasons[f"runs/fcst_gfs_{in_window}_model_a"] == "within_retention_window"
    assert result.planned == []


# ===========================================================================
# Issue #1318 -- additional ``runs/``-only roots
#
# The governing invariant: retention only ever deletes a directory that lives
# under ``<configured root>/runs``, is named like a canonical run id, has a
# cycle older than *that root's* cutoff, and sits below the frontier. Nothing
# else -- above all the cycle-scoped prefixes on an additional root -- may
# enter the plan under any configuration.
# ===========================================================================


# --- 2.1 -------------------------------------------------------------------
def test_extra_root_run_workspace_is_reclaimed_and_attributed(tmp_path: Path) -> None:
    """[2.1] An aged run workspace on the workspace root is deleted, and the
    receipt entry names that root."""
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    aged = NOW - timedelta(days=40)
    key = _seed_run_workspace(workspace, aged)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(workspace,),
    )

    assert _entries_for(result.planned, workspace) == {key}
    assert _entries_for(result.deleted, workspace) == {key}
    assert result.freed_bytes > 0
    assert not (workspace / key).exists()
    assert (workspace / "runs").is_dir()


# --- 2.2 -------------------------------------------------------------------
def test_extra_root_cycle_scoped_prefixes_are_never_selected(tmp_path: Path) -> None:
    """[2.2] runs-only, pinned. The copyback root's forcing/ tree is node-27's
    live display serving surface; sweeping it is a silent feature regression."""
    store = tmp_path / "object-store"
    store.mkdir()
    copyback = tmp_path / "copyback"
    ancient = NOW - timedelta(days=400)
    seeded = _seed_cycle(copyback, ancient)  # raw/ + canonical/ + forcing/
    run_key = _seed_run_workspace(copyback, ancient)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(copyback,),
    )

    # Only the run workspace is ever touched, in plan and on disk.
    assert _entries_for(result.planned, copyback) == {run_key}
    for prefix in ("raw", "canonical", "forcing"):
        assert (copyback / seeded[prefix]).exists()
        assert not any(
            entry["key"].startswith(f"{prefix}/")
            for entry in [*result.planned, *result.deleted, *result.skipped, *result.failed]
            if entry["root"] == str(copyback.resolve())
        )


# --- 2.3 -------------------------------------------------------------------
def test_the_two_retention_windows_are_independent(tmp_path: Path) -> None:
    """[2.3] Same cycle, both roots: past the 14d object-store cutoff, inside
    the 30d additional window."""
    store = tmp_path / "object-store"
    workspace = tmp_path / "workspace"
    cycle = NOW - timedelta(days=20)
    store_key = _seed_run_workspace(store, cycle)
    workspace_key = _seed_run_workspace(workspace, cycle)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=EXTRA_CONFIG,
        runs_only_roots=(workspace,),
    )

    assert _entries_for(result.planned, store) == {store_key}
    assert _entries_for(result.planned, workspace) == set()
    workspace_skips = {
        entry["key"]: entry["reason"]
        for entry in result.skipped
        if entry["root"] == str(workspace.resolve())
    }
    assert workspace_skips == {workspace_key: "within_retention_window"}


# --- 2.4 -------------------------------------------------------------------
def test_closed_gate_reproduces_the_previous_plan_key_for_key(tmp_path: Path) -> None:
    """[2.4] With the gate closed the additional roots are not scanned at all,
    and the plan matches the no-extra-roots call byte for byte."""
    store = tmp_path / "object-store"
    _seed_cycle(store, NOW - timedelta(days=20), run=True)
    workspace = tmp_path / "workspace"
    for age in (40, 60, 90):
        _seed_run_workspace(workspace, NOW - timedelta(days=age))

    gated = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, extra_roots_enabled=False),
        runs_only_roots=(workspace, tmp_path / "copyback"),
    )
    baseline = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, extra_roots_enabled=False),
    )

    assert gated.planned == baseline.planned
    assert gated.deleted == baseline.deleted
    assert gated.skipped == baseline.skipped
    assert gated.freed_bytes == baseline.freed_bytes
    # nothing from the additional root was scanned, in any bucket
    assert str(workspace.resolve()) not in {
        entry["root"] for entry in [*gated.planned, *gated.skipped, *gated.failed]
    }
    # ... and the receipt still discloses what opening the gate would use
    assert gated.to_dict()["extra_roots"] == {
        "enabled": False,
        "retention_days": 30,
        "cutoff": (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roots": [],
    }


# --- 2.5 -------------------------------------------------------------------
def test_roots_resolving_to_the_same_path_are_swept_once(tmp_path: Path) -> None:
    """[2.5] Historical single-root deployments must see no plan drift and no
    double-counted freed bytes."""
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
    assert result.to_dict()["extra_roots"]["roots"] == []

    solo = run_retention(
        object_store_root=tmp_path / "solo",
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
    )
    assert solo.freed_bytes == 0
    assert result.freed_bytes == _dir_bytes_of(result.planned)


def _dir_bytes_of(entries: list[dict]) -> int:
    return sum(int(entry["size_bytes"]) for entry in entries)


# --- 2.6 -------------------------------------------------------------------
def test_missing_extra_root_and_root_without_runs_are_silent_no_ops(tmp_path: Path) -> None:
    """[2.6] Absent root / root without runs/: no entries, no exception."""
    store = tmp_path / "object-store"
    store.mkdir()
    missing = tmp_path / "does-not-exist"
    no_runs = tmp_path / "no-runs"
    _write(no_runs, "raw/gfs/2026010100/payload.nc")

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(missing, no_runs),
    )

    assert result.planned == []
    assert result.skipped == []
    assert result.failed == []
    # Both survive hygiene + dedup, so the receipt still discloses them: a
    # mistyped root must be visible, not silently absent.
    assert result.to_dict()["extra_roots"]["roots"] == [str(missing), str(no_runs.resolve())]


# --- 2.6b ------------------------------------------------------------------
def test_blank_extra_root_never_resolves_to_the_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """[2.6b] ``Path("").expanduser().resolve()`` is the CWD, so a blank root
    would drag ``<cwd>/runs`` into the deletion surface."""
    cwd = tmp_path / "cwd"
    cwd_key = _seed_run_workspace(cwd, NOW - timedelta(days=90))
    monkeypatch.chdir(cwd)
    store = tmp_path / "object-store"
    store.mkdir()

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(None, "", "   "),
    )

    assert result.planned == []
    assert (cwd / cwd_key).exists()
    assert result.to_dict()["extra_roots"]["roots"] == []
    assert str(cwd.resolve()) not in result.to_dict()["extra_roots"]["roots"]


def test_extra_roots_are_swept_even_when_the_object_store_root_is_unset(tmp_path: Path) -> None:
    """[2.6b, task 1.2d] The object-store root's early return must not make the
    additional roots silently dead -- the CLI reads it from a bare getenv."""
    unset_workspace = tmp_path / "workspace-unset"
    absent_workspace = tmp_path / "workspace-absent"
    unset_key = _seed_run_workspace(unset_workspace, NOW - timedelta(days=40))
    absent_key = _seed_run_workspace(absent_workspace, NOW - timedelta(days=40))

    unset = run_retention(
        object_store_root=None,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(unset_workspace,),
    )
    absent = run_retention(
        object_store_root=tmp_path / "nope",
        now=NOW,
        config=EXTRA_CONFIG,
        runs_only_roots=(absent_workspace,),
    )

    assert _entries_for(unset.deleted, unset_workspace) == {unset_key}
    assert not (unset_workspace / unset_key).exists()
    assert _entries_for(absent.planned, absent_workspace) == {absent_key}


# --- 2.7 -------------------------------------------------------------------
def test_non_canonical_names_on_an_extra_root_are_never_deleted(tmp_path: Path) -> None:
    """[2.7] Only names the pipeline actually mints are admitted."""
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    _write(workspace, "runs/not-a-run-id/output/out.nc")
    _write(workspace, "runs/2026010100/output/out.nc")

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(workspace,),
    )

    assert result.planned == []
    assert {
        entry["key"]: entry["reason"]
        for entry in result.skipped
        if entry["root"] == str(workspace.resolve())
    } == {
        "runs/not-a-run-id": "unparseable_run_cycle",
        "runs/2026010100": "unparseable_run_cycle",
    }
    assert (workspace / "runs/not-a-run-id").exists()


# --- 2.8 -------------------------------------------------------------------
def test_frontier_exemption_applies_to_extra_roots(tmp_path: Path) -> None:
    """[2.8] Catch-up/replay pulls the active lower bound far below wall clock;
    the exemption must gate the additional roots too."""
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    in_flight = NOW - timedelta(days=45)
    collectable = NOW - timedelta(days=60)
    exempt_key = _seed_run_workspace(workspace, in_flight)
    collectable_key = _seed_run_workspace(workspace, collectable)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=EXTRA_CONFIG,
        runs_only_roots=(workspace,),
        active_lower_bound=in_flight,
        active_lower_bound_source="candidates",
    )

    exempt = [
        entry for entry in result.skipped if entry["reason"] == PIPELINE_FRONTIER_EXEMPT_REASON
    ]
    assert [entry["key"] for entry in exempt] == [exempt_key]
    assert exempt[0]["root"] == str(workspace.resolve())
    assert "size_bytes" not in exempt[0]
    assert _entries_for(result.planned, workspace) == {collectable_key}


# --- 2.9 -------------------------------------------------------------------
def test_published_artifact_protection_applies_to_extra_roots(tmp_path: Path) -> None:
    """[2.9] The published-artifact root protects every root, not just the
    object-store one."""
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    key = _seed_run_workspace(workspace, NOW - timedelta(days=40))

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        published_artifact_root=workspace / "runs",
        runs_only_roots=(workspace,),
    )

    assert result.planned == []
    assert {
        entry["key"]: entry["reason"]
        for entry in result.skipped
        if entry["root"] == str(workspace.resolve())
    } == {key: "protected_path"}
    assert (workspace / key).exists()


# --- 2.9b ------------------------------------------------------------------
def test_symlinked_runs_directory_does_not_extend_the_deletion_surface(tmp_path: Path) -> None:
    """[2.9b] ``Path.is_dir()`` follows symlinks: a swapped ``runs/`` would aim
    the enumeration, and the deletion, outside the root."""
    store = tmp_path / "object-store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside_run = f"runs/{_run_id(NOW - timedelta(days=90))}"
    _write(outside, f"{outside_run}/output/out.nc")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "runs").symlink_to(outside / "runs", target_is_directory=True)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(workspace,),
    )

    assert result.planned == []
    assert result.deleted == []
    assert (outside / outside_run).exists()
    assert not any(
        str(outside.resolve()) in entry["path"]
        for entry in [*result.planned, *result.skipped, *result.failed]
        if "path" in entry
    )
    assert [
        (entry["key"], entry["reason"])
        for entry in result.skipped
        if entry["root"] == str(workspace.resolve())
    ] == [("runs", "runs_root_symlink_skipped")]


# --- 2.9c ------------------------------------------------------------------
def test_removal_on_an_extra_root_unlinks_internal_links_without_following(tmp_path: Path) -> None:
    """[2.9c] Containment (issue #1615): a link inside a selected run workspace
    is unlinked, never followed -- the whole run is reclaimed and the link's
    target survives byte-identical."""
    store = tmp_path / "object-store"
    store.mkdir()
    outside = tmp_path / "outside"
    _write(outside, "precious/data.nc", b"precious")
    workspace = tmp_path / "workspace"
    linked_key = _seed_run_workspace(workspace, NOW - timedelta(days=40))
    plain_key = _seed_run_workspace(workspace, NOW - timedelta(days=50))
    (workspace / linked_key / "escape").symlink_to(outside / "precious", target_is_directory=True)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(workspace,),
    )

    # The link target is untouched and the link is unlinked, not refused, so
    # the whole run -- links included -- is reclaimed in one pass.
    assert (outside / "precious/data.nc").read_bytes() == b"precious"
    assert _entries_for(result.deleted, workspace) == {linked_key, plain_key}
    assert _entries_for(result.failed, workspace) == set()
    assert not (workspace / linked_key).exists()
    assert not (workspace / plain_key).exists()


# --- 2.9d ------------------------------------------------------------------
def test_v2_receipt_still_carries_the_frontier_block_unchanged(tmp_path: Path) -> None:
    """[2.9d] ``retention_frontier`` reads this block; its shape is unchanged."""
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    in_flight = NOW - timedelta(days=45)
    _seed_run_workspace(workspace, in_flight)

    payload = run_retention(
        object_store_root=store,
        now=NOW,
        config=EXTRA_CONFIG,
        runs_only_roots=(workspace,),
        active_lower_bound=in_flight,
        active_lower_bound_source="candidates",
    ).to_dict()

    assert payload["frontier"] == {
        "active_lower_bound": in_flight.astimezone(UTC).isoformat(),
        "source": "candidates",
        "protected_count": 1,
    }


# --- 2.10 ------------------------------------------------------------------
def test_receipt_is_v2_with_a_complete_extra_roots_block(tmp_path: Path) -> None:
    """[2.10] Schema v2: the additional-root block and per-entry ``root``."""
    store = tmp_path / "object-store"
    store_key = _seed_run_workspace(store, NOW - timedelta(days=20))
    workspace = tmp_path / "workspace"
    workspace_key = _seed_run_workspace(workspace, NOW - timedelta(days=40))

    payload = run_retention(
        object_store_root=store,
        now=NOW,
        config=EXTRA_CONFIG,
        runs_only_roots=(workspace,),
    ).to_dict()

    assert payload["schema_version"] == "nhms.production_scheduler.retention.v2"
    assert payload["extra_roots"] == {
        "enabled": True,
        "retention_days": 30,
        "cutoff": (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roots": [str(workspace.resolve())],
    }
    # the object-store window is untouched by the additional one
    assert payload["retention_days"] == 14
    assert payload["cutoff"] == (NOW - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {entry["key"]: entry["root"] for entry in payload["planned"]} == {
        store_key: str(store.resolve()),
        workspace_key: str(workspace.resolve()),
    }


# --- 2.12 ------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["unsafe", "io"])
def test_failed_removal_on_an_extra_root_is_isolated(tmp_path: Path, monkeypatch, kind) -> None:
    """[2.12] ``SafeFilesystemError`` is a ``RuntimeError``, not an ``OSError``:
    an uncaught one collapses the pass receipt and aborts the CLI mid-sweep.
    The injected failure targets the actual removal primitive retention uses
    for additional-root run trees (``remove_tree_allow_symlinks``)."""
    import services.orchestrator.retention as retention_mod
    from packages.common.safe_fs import SafeFilesystemError

    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    doomed_key = _seed_run_workspace(workspace, NOW - timedelta(days=40))
    survivor_key = _seed_run_workspace(workspace, NOW - timedelta(days=50))
    real_remove = retention_mod.remove_tree_allow_symlinks

    def failing_remove(parent, name, **kwargs):
        if Path(parent) / name == Path(workspace) / doomed_key:
            raise SafeFilesystemError(f"boom on {Path(parent) / name}", kind=kind)
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(retention_mod, "remove_tree_allow_symlinks", failing_remove)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(workspace,),
    )

    assert _entries_for(result.failed, workspace) == {doomed_key}
    assert "boom on" in result.failed[0]["error"]
    # the sweep continued, and only the successful removal is counted
    assert _entries_for(result.deleted, workspace) == {survivor_key}
    assert (workspace / doomed_key).exists()
    assert not (workspace / survivor_key).exists()
    assert result.freed_bytes == int(result.deleted[0]["size_bytes"])


# ===========================================================================
# Issue #1318 section 5 -- the additional roots the *pass* actually forwards.
#
# The module-level tests above drive ``run_retention`` directly, so they say
# nothing about which roots ``ProductionScheduler._run_retention`` hands it.
# That wiring is the deletion surface an operator gets, and it is the only
# place where a root can come from a built-in default instead of from
# configuration.
# ===========================================================================


# --- 5.3 -------------------------------------------------------------------
def test_pass_forwards_both_configured_extra_roots(tmp_path: Path, monkeypatch) -> None:
    """[5.3] The pass-side wiring itself: with the gate open and both roots
    explicitly configured, both are swept and each entry names its own root.

    Deleting the ``runs_only_roots`` argument from
    ``scheduler_runtime._run_retention`` must make this test fail -- that
    four-line block had no coverage at all before this test existed.
    """
    _seed_pass_env(monkeypatch)
    store = tmp_path / "object-store"
    store.mkdir()
    workspace = tmp_path / "workspace"
    copyback = tmp_path / "copyback"
    workspace_key = _seed_run_workspace(workspace, NOW - timedelta(days=40))
    copyback_key = _seed_run_workspace(copyback, NOW - timedelta(days=50))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback))

    scheduler = _pass_scheduler(workspace_root=str(workspace), object_store_root=str(store))
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    # (1) both configured roots reached retention
    assert payload["extra_roots"]["roots"] == [str(workspace.resolve()), str(copyback.resolve())]
    # (2) the aged run under each is selected and attributed to its own root
    assert _entries_for(payload["planned"], workspace) == {workspace_key}
    assert _entries_for(payload["planned"], copyback) == {copyback_key}
    # (3) physical: both were really reclaimed
    assert not (workspace / workspace_key).exists()
    assert not (copyback / copyback_key).exists()


# --- 5.4 -------------------------------------------------------------------
def test_pass_does_not_sweep_a_defaulted_workspace_root(tmp_path: Path, monkeypatch) -> None:
    """[5.4] ``WORKSPACE_ROOT`` unset means the workspace root is the built-in
    relative default, which ``__post_init__`` anchors to the invocation working
    directory. A default must never become a deletion surface, and the anchoring
    means no absolute-path filter downstream can catch it."""
    _seed_pass_env(monkeypatch)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    store = tmp_path / "object-store"
    store.mkdir()
    defaulted = cwd / ".nhms-workspace"
    defaulted_key = _seed_run_workspace(defaulted, NOW - timedelta(days=90))

    scheduler = _pass_scheduler(object_store_root=str(store))
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert payload["extra_roots"]["roots"] == []
    assert str(defaulted.resolve()) not in {entry["root"] for entry in payload["planned"]}
    assert payload["planned"] == []
    # physical: the defaulted root's aged run workspace is still on disk
    assert (defaulted / defaulted_key).exists()
    assert (defaulted / defaulted_key / "output/out.nc").exists()


# --- 5.5 -------------------------------------------------------------------
def test_relative_extra_root_is_discarded_with_a_recorded_reason(
    tmp_path: Path, monkeypatch
) -> None:
    """[5.5] A configured-but-relative root resolves against the working
    directory. It is discarded before ``resolve()`` and recorded, so the receipt
    says why that root produced nothing. ``NHMS_OBJECT_STORE_COPYBACK_ROOT``
    arrives as the bare env string, so the value is stripped before judging."""
    from services.orchestrator.retention import EXTRA_ROOT_NOT_ABSOLUTE_REASON

    cwd = tmp_path / "cwd"
    cwd_key = _seed_run_workspace(cwd, NOW - timedelta(days=90))
    nested = cwd / "relative/copyback"
    nested_key = _seed_run_workspace(nested, NOW - timedelta(days=90))
    monkeypatch.chdir(cwd)
    store = tmp_path / "object-store"
    store.mkdir()

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(EXTRA_CONFIG, dry_run=False),
        runs_only_roots=(".", "  relative/copyback  "),
    )

    assert result.to_dict()["extra_roots"]["roots"] == []
    assert result.planned == []
    assert [
        (entry["key"], entry["root"], entry["reason"])
        for entry in result.skipped
        if entry["reason"] == EXTRA_ROOT_NOT_ABSOLUTE_REASON
    ] == [
        ("runs", ".", EXTRA_ROOT_NOT_ABSOLUTE_REASON),
        ("runs", "relative/copyback", EXTRA_ROOT_NOT_ABSOLUTE_REASON),
    ]
    # physical: nothing under the working directory was touched
    assert (cwd / cwd_key / "output/out.nc").exists()
    assert (nested / nested_key / "output/out.nc").exists()
