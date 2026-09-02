"""#1944: the read-only job-id scope census over the file journal.

The census exists because a legacy row whose ``job_id`` contradicts its own
``(source, cycle)`` has no observation surface: the #1760 gate rejects it on
every write lane, and the one shape that aborts a whole reconcile scan -- a
segment-resident row with a reconcile-inventory anchor and no flat direct file
-- is invisible to any flat-only look.

Divergent rows are minted the way the journal's own regression suite mints them
(``tests/test_file_orchestration_journal.py`` around ``_rewrite_persisted_job_id``):
a legal row is written through the PUBLIC ``reserve_pipeline_job`` writer and its
persisted ``job_id`` bytes are then rewritten on disk.  The divergent id never
passes through a write boundary -- the gate would reject it -- so the tree is
exactly what a pre-gate deployment or a torn rename would have left.  The
helpers are copied here rather than imported: the journal suite is a 19k-line
module and importing it to reach four helpers would couple this file's
collection time to all of it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli as cli_module
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.journal_root_authority import JOURNAL_ROOT_INVALID_MESSAGE
from services.orchestrator.journal_scope_census import (
    CENSUS_JOB_ID_SCOPE_COMMAND,
    CENSUS_SCHEMA_VERSION,
    OUTPUT_UNWRITABLE_MESSAGE,
    census_job_id_scope,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

_ENTRYPOINTS = ("click", "argparse")

_CYCLE = datetime.fromisoformat("2026-07-20T00:00:00+00:00")
_HEALTHY_CYCLE = datetime.fromisoformat("2026-07-20T06:00:00+00:00")
_MINTED_JOB_ID = "job_fcst_gfs_2026072000_model_a_forecast"
_DIVERGENT_JOB_ID = "job_fcst_gfs_2026060100_model_a_forecast"
_HEALTHY_JOB_ID = "job_fcst_gfs_2026072006_model_a_forecast"
#: What the gate's evidence spells for the minted-then-rewritten row: the row's
#: own July pair against the June pair its identifier claims.
_OWN_SCOPE = "gfs/2026072000"
_JOB_ID_SCOPE = "gfs/2026060100"
_RESIDUE_NAME = f".{_HEALTHY_JOB_ID}.json.{'0' * 32}.tmp"


# ---------------------------------------------------------------------------
# Fixture helpers (copied from the journal suite's minting idiom)
# ---------------------------------------------------------------------------
def _reservation(cycle_time: datetime, job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": f"fcst_gfs_{format_cycle_time(cycle_time)}_model_a",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "reserved",
        "stage": "forecast",
        "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:basin_a:forecast",
        "candidate_id": "candidate_a",
    }


def _mint_legal_journal(root: Path) -> FileOrchestrationJournalRepository:
    """Two legal reserved rows in two cycles, all through the public writer."""

    repository = FileOrchestrationJournalRepository(root)
    assert repository.reserve_pipeline_job(_reservation(_CYCLE, _MINTED_JOB_ID)) is not None
    assert repository.reserve_pipeline_job(_reservation(_HEALTHY_CYCLE, _HEALTHY_JOB_ID)) is not None
    return repository


def _rewrite_persisted_job_id(root: Path, *, minted: str, divergent: str) -> Path:
    """Edit the PERSISTED ``job_id`` bytes only; every other identity field stays.

    Copied from ``tests/test_file_orchestration_journal.py``.  Touches the flat
    direct file, every journal segment and every latest view -- deliberately not
    the reconcile-inventory anchor, which the callers below plant explicitly in
    the shape ``_sync_reconcile_inventory_for_row_unlocked`` writes.
    """

    minted_direct = root / "pipeline-jobs" / f"{minted}.json"
    assert minted_direct.exists()
    for path in [
        minted_direct,
        *sorted(root.rglob("*.jsonl")),
        *sorted((root / "latest").rglob("*.json")),
    ]:
        text = path.read_text(encoding="utf-8")
        if minted in text:
            path.write_text(text.replace(minted, divergent), encoding="utf-8")
    divergent_direct = root / "pipeline-jobs" / f"{divergent}.json"
    minted_direct.rename(divergent_direct)
    return divergent_direct


def _write_anchor(
    root: Path,
    *,
    job_id: str,
    source_id: str = "gfs",
    cycle_time: datetime = _CYCLE,
    row_kind: str = "legacy",
) -> Path:
    """One anchor in the exact shape ``_sync_reconcile_inventory_for_row_unlocked`` writes."""

    directory = root / journal_module._RECONCILE_INVENTORY_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{job_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": journal_module._RECONCILE_INVENTORY_SCHEMA_VERSION,
                "job_id": job_id,
                "source_id": source_id,
                "cycle_time": journal_module._format_utc(cycle_time),
                "row_kind": row_kind,
            }
        ),
        encoding="utf-8",
    )
    return path


def _segment_only_divergent_tree(root: Path) -> FileOrchestrationJournalRepository:
    """The reconcile-abort shape: segments + latest + anchor, NO flat direct.

    The minted row's own anchor is removed and replaced by one naming the
    rewritten id, because that is what the pre-gate writer would have left
    behind: the anchor carries the row's own ``(source, cycle)`` pair, and the
    row's persisted id is what changed.
    """

    repository = _mint_legal_journal(root)
    inventory = root / journal_module._RECONCILE_INVENTORY_DIRECTORY
    (inventory / f"{_MINTED_JOB_ID}.json").unlink()
    divergent_direct = _rewrite_persisted_job_id(root, minted=_MINTED_JOB_ID, divergent=_DIVERGENT_JOB_ID)
    # The missing flat direct is exactly what sends the anchor into the
    # reconcile scan's repair lane, where the gate aborts the whole scan.
    divergent_direct.unlink()
    _write_anchor(root, job_id=_DIVERGENT_JOB_ID)
    assert not (root / "pipeline-jobs" / f"{_DIVERGENT_JOB_ID}.json").exists()
    assert not list((root / "pipeline-jobs").rglob(f"{_DIVERGENT_JOB_ID}.json"))
    return repository


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every directory and file under ``root``; ``None`` marks a directory."""

    snapshot: dict[str, bytes | None] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = None
        for name in files:
            path = Path(current) / name
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _invoke(entrypoint: str, args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    if entrypoint == "click":
        try:
            code = cli_module._click_main(args)
        except SystemExit as exit_error:
            code = int(exit_error.code or 0)
    else:
        code = cli_module._argparse_main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _census_args(root: Path | str, *extra: str) -> list[str]:
    return [CENSUS_JOB_ID_SCOPE_COMMAND, "--journal-root", str(root), *extra]


# ---------------------------------------------------------------------------
# 1. A legal tree censuses to zero, and every surface is reported
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_legal_tree_censuses_to_zero_and_reports_every_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """Two legal rows across two cycles, plus one hand-written anchor.

    An absent directory is REPORTED absent (``present: false``), never omitted:
    node-22 has no ``active-reconcile/`` today and the receipt must say so.
    """

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    # A by-cycle direct file, placed the way the journal suite places one
    # (``test_direct_cycle_read_lanes_separate_flat_from_by_cycle``): the
    # partition only ever receives current accepted-submit candidate rows, so a
    # reservation lands flat and is relocated here.
    by_cycle = root / "pipeline-jobs" / "by-cycle" / "gfs" / format_cycle_time(_HEALTHY_CYCLE)
    by_cycle.mkdir(parents=True)
    (root / "pipeline-jobs" / f"{_HEALTHY_JOB_ID}.json").rename(by_cycle / f"{_HEALTHY_JOB_ID}.json")

    code, out, err = _invoke(entrypoint, _census_args(root), capsys)

    assert err == ""
    assert code == 0
    receipt = json.loads(out)
    assert receipt["schema_version"] == CENSUS_SCHEMA_VERSION
    assert receipt["divergent_total"] == 0
    assert receipt["divergent_rows"] == []
    assert receipt["reconcile_abort_triggers"] == 0
    assert receipt["exit_code"] == 0
    assert receipt["journal_root"] == str(root)
    assert receipt["journal_root_verified"] == str(root)
    surfaces = receipt["surfaces"]
    assert surfaces["flat_direct"] == {"present": True, "files": 1, "rows": 1, "divergent": 0}
    assert surfaces["by_cycle_direct"] == {"present": True, "files": 1, "rows": 1, "divergent": 0}
    assert surfaces["reconcile_inventory"] == {
        "present": True,
        "files": 2,
        "rows": 2,
        "divergent": 0,
        "residue": 0,
    }
    # Absence is reported, not omitted.
    assert surfaces["active_reconcile"] == {"present": False, "files": 0, "rows": 0, "divergent": 0}
    replay = surfaces["journal_replay"]
    assert replay["present"] is True
    assert replay["rows"] == 2
    assert replay["divergent"] == 0
    assert replay["latest_files"] == 2
    assert replay["segment_files"] == 2
    assert replay["files"] == 4


def test_absent_inventory_and_active_reconcile_are_reported_absent(tmp_path: Path) -> None:
    """A tree with neither optional directory still reports both surfaces."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    inventory = root / journal_module._RECONCILE_INVENTORY_DIRECTORY
    for path in sorted(inventory.iterdir()):
        path.unlink()
    inventory.rmdir()

    receipt = census_job_id_scope(root)

    assert receipt["surfaces"]["reconcile_inventory"]["present"] is False
    assert receipt["surfaces"]["active_reconcile"]["present"] is False
    assert receipt["divergent_total"] == 0


# ---------------------------------------------------------------------------
# 2. The reconcile-abort shape: segment-only divergent row with an anchor
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_segment_only_divergent_row_with_anchor_is_the_abort_trigger(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """The one shape a flat-only scan counts as zero, and the reason for D2."""

    root = tmp_path / "journal"
    _segment_only_divergent_tree(root)

    code, out, err = _invoke(entrypoint, _census_args(root), capsys)

    assert err == ""
    assert code == 2
    receipt = json.loads(out)
    assert receipt["divergent_total"] == 1
    assert receipt["reconcile_abort_triggers"] == 1
    assert receipt["exit_code"] == 2
    entry = receipt["divergent_rows"][0]
    assert entry["job_id"] == _DIVERGENT_JOB_ID
    # Anchors are not rows: the inventory never appears in ``surfaces``.
    assert entry["surfaces"] == ["journal_replay"]
    assert entry["own_scope"] == _OWN_SCOPE
    assert entry["job_id_scope"] == _JOB_ID_SCOPE
    assert entry["anchor_present"] is True
    assert entry["flat_direct_present"] is False
    assert entry["by_cycle_present"] is False
    assert entry["journal_present"] is True
    assert entry["reconcile_abort_trigger"] is True
    assert receipt["surfaces"]["reconcile_inventory"]["divergent"] == 1
    assert receipt["surfaces"]["flat_direct"]["divergent"] == 0
    assert receipt["surfaces"]["journal_replay"]["divergent"] == 1


def test_divergent_row_on_three_surfaces_counts_once_and_is_not_a_trigger(tmp_path: Path) -> None:
    """Dedup by ``job_id``; a present flat direct means the repair lane never runs."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    divergent_direct = _rewrite_persisted_job_id(root, minted=_MINTED_JOB_ID, divergent=_DIVERGENT_JOB_ID)
    by_cycle = root / "pipeline-jobs" / "by-cycle" / "gfs" / format_cycle_time(_CYCLE)
    by_cycle.mkdir(parents=True)
    (by_cycle / f"{_DIVERGENT_JOB_ID}.json").write_bytes(divergent_direct.read_bytes())

    receipt = census_job_id_scope(root)

    assert receipt["divergent_total"] == 1
    assert receipt["reconcile_abort_triggers"] == 0
    entry = receipt["divergent_rows"][0]
    assert entry["job_id"] == _DIVERGENT_JOB_ID
    assert entry["surfaces"] == ["by_cycle_direct", "flat_direct", "journal_replay"]
    assert entry["flat_direct_present"] is True
    assert entry["by_cycle_present"] is True
    assert entry["journal_present"] is True
    assert entry["reconcile_abort_trigger"] is False
    assert receipt["surfaces"]["flat_direct"]["divergent"] == 1
    assert receipt["surfaces"]["by_cycle_direct"]["divergent"] == 1
    assert receipt["surfaces"]["journal_replay"]["divergent"] == 1


def test_anchor_only_divergent_id_counts_once_with_no_row_surfaces(tmp_path: Path) -> None:
    """An anchor with no row anywhere is still a divergent id and still a trigger."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    _write_anchor(root, job_id=_DIVERGENT_JOB_ID)

    receipt = census_job_id_scope(root)

    assert receipt["divergent_total"] == 1
    assert receipt["reconcile_abort_triggers"] == 1
    entry = receipt["divergent_rows"][0]
    assert entry["job_id"] == _DIVERGENT_JOB_ID
    assert entry["surfaces"] == []
    assert entry["own_scope"] == _OWN_SCOPE
    assert entry["job_id_scope"] == _JOB_ID_SCOPE
    assert entry["anchor_present"] is True
    assert entry["flat_direct_present"] is False
    assert entry["journal_present"] is False
    assert entry["reconcile_abort_trigger"] is True
    assert receipt["surfaces"]["reconcile_inventory"]["divergent"] == 1


def test_divergent_row_in_the_legacy_active_reconcile_directory_is_counted(tmp_path: Path) -> None:
    """node-22 has no ``active-reconcile/`` today; the canonical lookup still reads it."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    divergent_direct = _rewrite_persisted_job_id(root, minted=_MINTED_JOB_ID, divergent=_DIVERGENT_JOB_ID)
    legacy = root / journal_module._LEGACY_ACTIVE_RECONCILE_DIRECTORY
    legacy.mkdir()
    (legacy / f"{_DIVERGENT_JOB_ID}.json").write_bytes(divergent_direct.read_bytes())

    receipt = census_job_id_scope(root)

    assert receipt["surfaces"]["active_reconcile"]["present"] is True
    assert receipt["surfaces"]["active_reconcile"]["rows"] == 1
    assert receipt["surfaces"]["active_reconcile"]["divergent"] == 1
    assert receipt["divergent_total"] == 1
    assert receipt["divergent_rows"][0]["surfaces"] == [
        "active_reconcile",
        "flat_direct",
        "journal_replay",
    ]


# ---------------------------------------------------------------------------
# 3. The inventory listing rules are the journal's own
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_inventory_entry_that_is_neither_anchor_nor_residue_fails_loud(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """Mirrors ``_reconcile_inventory_entry_names_unlocked``: no skipping, ever."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    (root / journal_module._RECONCILE_INVENTORY_DIRECTORY / "not-an-anchor.txt").write_text("x", encoding="utf-8")
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root), capsys)

    assert code == 1
    assert out.strip() == ""
    assert err.strip() == "file_journal_reconcile_inventory_invalid: reconcile_inventory"
    assert "Traceback" not in err
    assert _snapshot(root) == before


def test_atomic_temp_residue_is_reported_and_left_in_place(tmp_path: Path) -> None:
    """The lock-holding lane DELETES residue; a census must not.

    Removing residue is a write, and it belongs to the writer that holds the
    inventory lock -- not to a read-only observation run against a live tree.
    """

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    residue = root / journal_module._RECONCILE_INVENTORY_DIRECTORY / _RESIDUE_NAME
    residue.write_text("{}", encoding="utf-8")

    receipt = census_job_id_scope(root)

    assert receipt["surfaces"]["reconcile_inventory"]["residue"] == 1
    assert receipt["surfaces"]["reconcile_inventory"]["files"] == 3
    assert receipt["surfaces"]["reconcile_inventory"]["rows"] == 2
    assert residue.exists()
    assert residue.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# 4. Zero writes, and no repair/lock path entered
# ---------------------------------------------------------------------------
def test_census_writes_nothing_and_enters_no_reconcile_repair_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files AND directories are byte- and entry-identical afterwards.

    A files-only snapshot would miss exactly the paths that could slip: residue
    removal and directory creation.  The four monkeypatched entry points are the
    ones that take the write lock, migrate the inventory, restore derived
    directs or prune anchors; reaching any of them is a failure by itself.
    """

    root = tmp_path / "journal"
    _segment_only_divergent_tree(root)
    residue = root / journal_module._RECONCILE_INVENTORY_DIRECTORY / _RESIDUE_NAME
    residue.write_text("{}", encoding="utf-8")
    before = _snapshot(root)

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the census must not enter any reconcile write/lock path")

    for name in (
        "_iter_reconcile_inventory_records",
        "_ensure_reconcile_inventory_migrated",
        "_reconcile_inventory_entry_names_unlocked",
        "_locked_cycle_write",
    ):
        monkeypatch.setattr(FileOrchestrationJournalRepository, name, _forbidden)

    receipt = census_job_id_scope(root)

    assert receipt["divergent_total"] == 1
    assert receipt["surfaces"]["reconcile_inventory"]["residue"] == 1
    after = _snapshot(root)
    assert after == before
    assert residue.exists()


def test_census_classifies_only_through_the_gates_own_predicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Predicate-reuse pin: neuter the gate's derivation, the count drops to zero.

    A second comparison living in the census module would keep counting here,
    and that is exactly the "census 0 while the gate fires" false green -- in
    reverse.  Nothing else in the receipt is asserted: the point is the count.
    """

    root = tmp_path / "journal"
    _segment_only_divergent_tree(root)
    assert census_job_id_scope(root)["divergent_total"] == 1

    monkeypatch.setattr(journal_module, "_cycle_scope_from_job_id", lambda job_id: None)

    receipt = census_job_id_scope(root)

    assert receipt["divergent_total"] == 0
    assert receipt["exit_code"] == 0
    assert receipt["surfaces"]["reconcile_inventory"]["divergent"] == 0
    assert receipt["surfaces"]["journal_replay"]["divergent"] == 0


# ---------------------------------------------------------------------------
# 5. Root and receipt-path refusals
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_alias_ancestor_root_refuses_the_census_typed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """The census constructs through the #1943 seam, so an alias root never reads."""

    real_base = tmp_path / "real"
    real_root = real_base / "scheduler" / "journal"
    real_root.mkdir(parents=True)
    _mint_legal_journal(real_root)
    alias_base = tmp_path / "alias"
    alias_base.symlink_to(real_base, target_is_directory=True)
    alias_root = alias_base / "scheduler" / "journal"
    before = _snapshot(real_base)

    code, out, err = _invoke(entrypoint, _census_args(alias_root), capsys)

    assert code == 1
    assert out.strip() == ""
    assert err.strip() == f"FILE_JOURNAL_INVALID_ROOT: {JOURNAL_ROOT_INVALID_MESSAGE}"
    assert "Traceback" not in err
    assert _snapshot(real_base) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("configured", ["", "."])
def test_blank_or_relative_root_is_refused_instead_of_censusing_the_cwd(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    configured: str,
) -> None:
    """``--journal-root ""`` must not census the working directory and exit 0.

    ``required=True`` only demands the option be present; an empty or relative
    value used to reach ``safe_fs``, which anchors a non-absolute path on the
    cwd.  An operator whose env var was unset would then read a clean receipt
    with ``divergent_total: 0`` over a directory that is not the journal at all
    -- the one failure mode a census exists to rule out.
    """

    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)

    code, out, err = _invoke(entrypoint, _census_args(configured), capsys)

    assert code == 1
    assert out.strip() == ""
    assert err.strip() == f"FILE_JOURNAL_INVALID_ROOT: {JOURNAL_ROOT_INVALID_MESSAGE}"
    assert "Traceback" not in err
    # No receipt, no scan and no byte: the cwd is untouched.
    assert os.listdir(".") == []


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_output_inside_the_journal_root_is_refused_with_nothing_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """The receipt must not become the first byte the census writes into the tree."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    output = root / "reconcile-inventory" / "census-receipt.json"
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--output", str(output)), capsys)

    assert code == 1
    assert out.strip() == ""
    assert err.startswith("CENSUS_OUTPUT_INSIDE_JOURNAL_ROOT: ")
    assert "Traceback" not in err
    assert not output.exists()
    assert _snapshot(root) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_output_outside_the_root_writes_the_same_json_as_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    root = tmp_path / "journal"
    _mint_legal_journal(root)
    output = tmp_path / "receipts" / "census.json"
    output.parent.mkdir()
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--output", str(output)), capsys)

    assert err == ""
    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(out)
    assert _snapshot(root) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("kind", ("missing_parent", "existing_directory"))
def test_unwritable_output_still_publishes_the_receipt_on_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    kind: str,
) -> None:
    """An unwritable ``--output`` costs the file, never the census.

    On node-22 the census takes minutes over the live tree; an unguarded
    ``write_text`` would leak an ``OSError`` traceback AND discard the run,
    leaving the operator with nothing.  The receipt is echoed first, so stdout
    still carries the complete JSON and the failure is one typed line.
    """

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    if kind == "missing_parent":
        output = tmp_path / "absent" / "census.json"
        assert not output.parent.exists()
    else:
        output = tmp_path / "receipts"
        output.mkdir()
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--output", str(output)), capsys)

    assert code == 1
    assert err.strip() == f"CENSUS_OUTPUT_UNWRITABLE: {OUTPUT_UNWRITABLE_MESSAGE}"
    assert "Traceback" not in err
    # The whole receipt is on stdout and parses: the census is not lost.
    receipt = json.loads(out)
    assert receipt["schema_version"] == CENSUS_SCHEMA_VERSION
    assert receipt["divergent_total"] == 0
    assert receipt["surfaces"]["flat_direct"]["rows"] == 2
    assert _snapshot(root) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_unwritable_output_after_a_divergent_census_exits_1_not_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """Process exit is 1, and the divergence verdict lives inside the receipt.

    The receipt is emitted before ``--output`` is written (deliberately: on
    node-22 the run costs minutes), so a typed failure raised AFTER the emit
    replaces the ``2`` the census computed with the typed-failure ``1``.  Both
    facts are true at once and an operator scripting on ``$?`` alone would read
    "no divergent rows" from a run that found some; the receipt's own
    ``exit_code`` is the field to read.
    """

    root = tmp_path / "journal"
    _segment_only_divergent_tree(root)
    output = tmp_path / "receipts"
    output.mkdir()
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--output", str(output)), capsys)

    assert code == 1
    assert err.strip() == f"CENSUS_OUTPUT_UNWRITABLE: {OUTPUT_UNWRITABLE_MESSAGE}"
    assert "Traceback" not in err
    receipt = json.loads(out)
    assert receipt["exit_code"] == 2
    assert receipt["divergent_total"] >= 1
    assert receipt["reconcile_abort_triggers"] >= 1
    assert _snapshot(root) == before


# ---------------------------------------------------------------------------
# 6. Record budget: the live node-22 tree needs the override
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_record_budget_trip_fails_loud_with_the_documented_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """The exact failure node-22 hit on 2026-09-02 with the default budget.

    The whole-tree replay charges ONE budget: one unit per pipeline-job row
    materialised from each latest view (a view can carry many rows) plus one per
    JSONL line in every segment, whatever the record type.  Direct records are
    charged only on the ``include_direct=True`` replay, which the census never
    asks for.  A production tree therefore trips ``MAX_FILE_JOURNAL_RECORDS``
    long before any file budget.  ``--max-files`` is not the knob: this is a
    record count, not a file count.
    """

    root = tmp_path / "journal"
    _mint_legal_journal(root)
    before = _snapshot(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--max-records", "1"), capsys)

    assert code == 1
    assert out.strip() == ""
    assert err.strip() == "file_journal_record_limit_exceeded: pipeline_job_records"
    assert "Traceback" not in err
    assert _snapshot(root) == before


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_raised_record_budget_completes_and_is_recorded_in_the_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """``--max-records N`` is the documented remedy, and the receipt says N."""

    root = tmp_path / "journal"
    _mint_legal_journal(root)

    code, out, err = _invoke(entrypoint, _census_args(root, "--max-records", "1000"), capsys)

    assert err == ""
    assert code == 0
    receipt = json.loads(out)
    assert receipt["limits"]["max_records"] == 1000
    assert receipt["limits"]["max_files"] == journal_module.MAX_FILE_JOURNAL_DISCOVERED_FILES
    assert receipt["divergent_total"] == 0
