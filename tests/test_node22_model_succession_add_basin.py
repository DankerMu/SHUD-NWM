"""The add-basin kind of the model succession tool, and the argument rules of every kind (#2756).

An added basin has no old model: nothing is compared and nothing is cloned, the
packaged initial condition of every added model is audited in ``preflight`` and
required again by ``publish``, the publish adds rows and replaces none, and the
plan, the receipts and the reports say in ``continuity`` that the basin has no
earlier forecasts.  The canonical manifest of these tests has two sources, as
the publish tool requires every basin to have a row for each of them; the
packages are real files, so the first-cycle audit and the publish tool actually
run.  The fixtures, the fake ``systemctl`` and the builders are in
``tests/model_succession_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt
from packages.common.object_store import sha256_bytes
from scripts.model_succession import run as succession_run
from scripts.model_succession.model import StepFailure
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
    ADD_BASIN_PACKAGE,
    COLD_START_PACKAGE,
    CUTOVER,
    REFRESH,
    SERVICE,
    SUCCESSION_ID,
    TIMER,
    Space,
    build_space,
    changed,
    no_database,
    report,
    stores,
    tree,
)

IS_ACTIVE = {f"--user is-active {TIMER}", f"--user is-active {SERVICE}"}
STOP = f"--user stop {TIMER}"
START_TIMER = f"--user start {TIMER}"
START_REFRESH = f"--user start {REFRESH}"
ADD_BASIN_STEPS = ("copyback", "preflight", "begin", "publish", "refresh", "finish")
SOURCES = ("gfs", "IFS")
QUALIFIED = "qualified"
# A header line of two numeric tokens: the shape the audit refuses.
UNQUALIFIED_IC = b"4\t6\n0.1\t0.2\n"
IC_AUDIT = "ic-audit.json"
# The models of the new basin ``d``, one per source, and the six rows the canonical manifest holds before.
ADDED = ["dg_d_gfs_v1", "dg_d_ifs_v1"]
BEFORE = ["dg_a_gfs_v1", "dg_b_gfs_v1", "dg_c_gfs_v1", "dg_a_ifs_v1", "dg_b_ifs_v1", "dg_c_ifs_v1"]


@pytest.fixture(name="added")
def added_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Space:
    """An add-basin succession of the two models of basin ``d``, whose packages ship a qualified IC, into a
    canonical manifest of three basins with two sources each."""

    return build_space(
        tmp_path,
        monkeypatch,
        kind="add_basin",
        sources=SOURCES,
        added_basin="d",
        new_packages=(ADD_BASIN_PACKAGE, ADD_BASIN_PACKAGE),
    )


def _assert_continuity(record: dict[str, Any]) -> None:
    continuity = dict(record["continuity"])
    notice = continuity.pop("notice")
    # No cutover is declared: the basin has no history.
    assert continuity == {"mode": "new_basin", "state_carried": False}
    assert "no earlier forecasts" in notice and "calibrated initial condition in its package" in notice
    assert "first cycle the scheduler plans for it after the timer is started" in notice
    assert "earliest cycle of the scheduler's lookback window, not the current one" in notice


def _refused_in_preflight(space: Space, before: dict[str, bytes], **arguments: Any) -> str:
    """An apply that fails in ``preflight``: the timer was never stopped and nothing was published."""

    assert space.main("--apply", **arguments) == 1
    (failure,) = space.failures()
    assert failure["step"] == "preflight" and failure["hard_stop"] is False
    assert failure["completed_steps"] == ["copyback"]
    assert failure["timer_touched_by_this_tool"] is False and failure["timer_stopped_by_this_tool"] is False
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "active"
    assert "timer-before-stop.json" not in space.names() and "step-preflight.json" not in space.names()
    assert stores(space) == before
    return str(failure["reason"])


def _provision_again(space: Space, rows: list[dict[str, Any]]) -> None:
    """Replace what the provision apply left by a registry and a receipt of ``rows``."""

    space.registry.unlink()
    assert space.ws.provision(SUCCESSION_ID, rows) == space.registry


# --- the add-basin apply ------------------------------------------------------------------


def test_an_add_basin_apply_runs_its_six_steps_and_adds_two_rows_leaving_every_other_row_as_it_was(
    added: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    index_paths = (added.canonical_index, added.mirror_index)
    indexes = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in index_paths}
    rows_before = [json.dumps(row, sort_keys=True) for row in added.ws.models(added.ws.canonical)]
    assert added.model_ids(added.ws.canonical) == BEFORE and [row["model_id"] for row in added.new_rows] == ADDED

    assert added.main("--apply") == 0

    result = report(capsys)
    assert list(added.settings().plan.steps) == list(ADD_BASIN_STEPS)
    assert result["steps"] == dict.fromkeys(ADD_BASIN_STEPS, "completed")
    assert result["outcome"] == "completed" and result["timer_action"] == "started"
    _assert_continuity(result)
    # No clone step, no clone receipt of either sort.
    assert added.names() == sorted(
        [
            "plan.json",
            "provision-apply.json",
            "timer-before-stop.json",
            IC_AUDIT,
            "publish-dry-run.json",
            "publish-apply.json",
            *(f"step-{step}.json" for step in ADD_BASIN_STEPS),
        ]
    )
    receipts = [added.receipt(f"step-{step}.json") for step in ADD_BASIN_STEPS]
    assert [receipt["step"] for receipt in receipts] == list(ADD_BASIN_STEPS)
    assert [receipt["generated_at"] for receipt in receipts] == sorted(receipt["generated_at"] for receipt in receipts)

    # Neither state index was written, and neither holds a clone row.
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in indexes} == indexes
    assert added.clone_rows(added.canonical_index) == added.clone_rows(added.mirror_index) == []

    # Both manifests are equal: the six earlier rows as they were, in their order, then the two added ones.
    assert added.ws.canonical.read_bytes() == added.ws.mirror.read_bytes()
    rows_after = [json.dumps(row, sort_keys=True) for row in added.ws.models(added.ws.canonical)]
    assert len(rows_after) == len(rows_before) + 2 and rows_after[: len(rows_before)] == rows_before
    assert added.model_ids(added.ws.canonical) == [*BEFORE, *ADDED]
    publish = added.receipt("publish-apply.json")
    assert publish["operations"] == {"replace": [], "add": ADDED, "remove": []}
    assert (publish["row_count_before"], publish["row_count_after"]) == (6, 8) and publish["replaced"] == []

    # The audit's own receipt: every added model under each source of the plan, all qualified.
    audit = added.receipt(IC_AUDIT)
    assert audit["inputs"]["registry_manifest"] == str(added.registry) and audit["inputs"]["sources"] == list(SOURCES)
    assert [(row["model_id"], row["source"], row["ic_status"]) for row in audit["rows"]] == [
        (model_id, source, QUALIFIED) for model_id in ADDED for source in SOURCES
    ]
    preflight = added.receipt("step-preflight.json")
    assert preflight["ic_audit"] == {
        "path": str(added.directory / IC_AUDIT),
        "sha256": succession_receipt.file_sha256(added.directory / IC_AUDIT),
        "reused": False,
        "models": {model_id: [QUALIFIED, QUALIFIED] for model_id in ADDED},
    }
    # There is no pair: no kind check ran, and nothing was cloned.
    assert "kind_check" not in preflight and "clone_dry_run" not in preflight

    plan = added.receipt("plan.json")
    assert (plan["kind"], plan["adds"], plan["provision_succession_id"]) == ("add_basin", ADDED, SUCCESSION_ID)
    assert "pairs" not in plan and "cutover_time" not in plan
    for record in (plan, added.receipt("step-publish.json"), added.receipt("step-finish.json")):
        _assert_continuity(record)

    # One stop, one refresh start, one timer start; everything before the stop was a query.
    assert added.systemctl.mutating() == [STOP, START_REFRESH, START_TIMER]
    assert set(added.systemctl.calls()[: added.systemctl.calls().index(STOP)]) <= IS_ACTIVE
    assert added.systemctl.state(TIMER) == "active"


# --- the arguments must fit the kind --------------------------------------------------------

# kind -> the options of the command line -> what the refusal says.
MISFITS: dict[str, tuple[str, dict[str, Any], tuple[str, ...], str]] = {
    "a_pair_with_add_basin": (
        "add_basin",
        {},
        ("--pair", "dg_a_gfs_v1:dg_d_gfs_v1"),
        "--pair is not valid with --kind add_basin",
    ),
    "an_add_with_recalibration": ("recalibration", {}, ("--add", "dg_d_gfs_v1"), "--add is not valid with --kind"),
    "an_add_with_cold_start": ("cold_start", {}, ("--add", "dg_d_gfs_v1"), "--add is not valid with --kind"),
    "a_cutover_time_with_add_basin": (
        "add_basin",
        {},
        ("--cutover-time", CUTOVER),
        "--cutover-time is not valid with --kind add_basin",
    ),
    "add_basin_without_an_add": ("add_basin", {"adds": []}, (), "at least one --add <new_model_id> is required"),
    "recalibration_without_a_cutover_time": (
        "recalibration",
        {"cutover": None},
        (),
        "--cutover-time <YYYYMMDDHH> is required with --kind recalibration",
    ),
    "cold_start_without_a_cutover_time": (
        "cold_start",
        {"cutover": None},
        (),
        "--cutover-time <YYYYMMDDHH> is required with --kind cold_start",
    ),
    "an_added_id_named_twice": (
        "add_basin",
        {"adds": [*ADDED, ADDED[0]]},
        (),
        f"may be named with --add once only: ['{ADDED[0]}']",
    ),
}


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
@pytest.mark.parametrize("case", sorted(MISFITS))
def test_arguments_that_do_not_fit_the_kind_are_refused_and_nothing_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    mode: tuple[str, ...],
) -> None:
    kind, arguments, extra, expected = MISFITS[case]
    if kind == "add_basin":
        packages, options = (ADD_BASIN_PACKAGE, ADD_BASIN_PACKAGE), {"sources": SOURCES, "added_basin": "d"}
    else:
        packages, options = ((COLD_START_PACKAGE, COLD_START_PACKAGE) if kind == "cold_start" else ({}, {})), {}
    space = build_space(tmp_path, monkeypatch, kind=kind, new_packages=packages, **options)
    before = tree(space.ws.root)

    assert space.main(*mode, *extra, **arguments) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Refused: ") and expected in captured.err
    assert changed(before, tree(space.ws.root)) == set()
    assert space.systemctl.calls() == []


@pytest.mark.parametrize("kind", ["recalibration", "cold_start"])
def test_the_refusals_of_the_replacing_kinds_keep_their_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    space = build_space(tmp_path, monkeypatch, kind=kind)
    old, new = space.pairs[0]

    assert space.main(pairs=[]) == 1
    assert capsys.readouterr().err == "Refused: at least one --pair <old_model_id>:<new_model_id> is required.\n"
    assert space.main(pairs=[(old, new), (old, "another")]) == 1
    assert capsys.readouterr().err == f"Refused: a model_id may be named in one --pair only, and once: ['{old}'].\n"
    assert space.main(cutover="20261005") == 1
    assert capsys.readouterr().err == "Refused: --cutover-time '20261005' is not YYYYMMDDHH.\n"


# --- the plan must be what was provisioned and is not yet scheduled ---------------------------


def _already_scheduled(space: Space) -> tuple[list[str], str]:
    # Provisioned again together with the new basin, and named with --add although it is scheduled.
    scheduled = space.ws.rows[2]
    _provision_again(space, [*space.new_rows, scheduled])
    return [*ADDED, scheduled["model_id"]], f"new model_id is already in the canonical manifest {space.ws.canonical}"


def _not_provisioned(space: Space) -> tuple[list[str], str]:
    return [*ADDED, "dg_e_gfs_v1"], "new model_id is not in models[] of"


def _left_out(space: Space) -> tuple[list[str], str]:
    return ADDED[:1], "lists model_id that is neither named with --add nor in the canonical manifest"


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
@pytest.mark.parametrize("arrange", [_already_scheduled, _not_provisioned, _left_out])
def test_an_add_the_provision_or_the_canonical_manifest_does_not_support_is_refused_before_any_step(
    added: Space, capsys: pytest.CaptureFixture[str], arrange: Any, mode: tuple[str, ...]
) -> None:
    adds, expected = arrange(added)
    named = {_already_scheduled: "dg_c_gfs_v1", _not_provisioned: "dg_e_gfs_v1", _left_out: ADDED[1]}[arrange]
    before = tree(added.ws.root)

    assert added.main(*mode, adds=adds) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and expected in captured.err and f"['{named}']" in captured.err
    assert "Nothing was written" in captured.err
    # The refusal of this kind names its own option.
    assert "--pair" not in captured.err
    assert changed(before, tree(added.ws.root)) == set() and added.failures() == []
    assert added.systemctl.mutating() == []


# --- preflight: the audit, then the publish tool's dry-run --------------------------------------


def test_an_added_model_whose_initial_condition_is_not_qualified_is_refused_in_preflight(added: Space) -> None:
    (added.package(added.new_rows[1], added.ws.shared) / "huai.cfg.ic").write_bytes(UNQUALIFIED_IC)

    reason = _refused_in_preflight(added, stores(added))

    assert f"{ADDED[1]}: ic_status unqualified" in reason and ADDED[0] not in reason
    assert "A new basin begins from the calibrated initial condition" in reason and "A cold start" not in reason
    assert f"No {IC_AUDIT} was written" in reason
    assert IC_AUDIT not in added.names() and "publish-dry-run.json" not in added.names()


def test_one_source_of_a_new_basin_alone_is_refused_by_the_publish_dry_run_in_preflight(added: Space) -> None:
    # The provision receipt lists only that model: a second provisioned id would be refused as left out, earlier.
    _provision_again(added, added.new_rows[:1])

    reason = _refused_in_preflight(added, stores(added), adds=ADDED[:1])

    assert "The publish dry-run refused" in reason
    assert "every basin_id must have exactly one row for each of the sources ['IFS', 'gfs']" in reason
    assert "basins_d has ['gfs']" in reason and "Add or remove every source of a basin together" in reason
    assert "publish-dry-run.json" not in added.names()


# --- publish: after begin, and with the audit receipt ---------------------------------------------


def test_an_add_basin_publish_before_begin_is_refused_and_neither_manifest_changes(added: Space) -> None:
    settings, inputs = added.run_steps("copyback", "preflight")
    before = stores(added)

    with pytest.raises(StepFailure, match="requires the receipt of the begin step") as raised:
        succession_run.run_step(settings, inputs, "publish")

    assert str(added.directory / "step-begin.json") in str(raised.value)
    assert stores(added) == before and "publish-apply.json" not in added.names()


def test_an_add_basin_publish_refuses_without_the_audit_receipt(added: Space) -> None:
    added.run_steps("copyback", "preflight", "begin")
    (added.directory / IC_AUDIT).unlink()
    before = stores(added)
    added.systemctl.clear()

    assert added.main("--apply") == 1

    (failure,) = added.failures()
    assert failure["step"] == "publish" and failure["hard_stop"] is False
    assert "the publish of a new basin requires the initial-condition audit receipt" in failure["reason"]
    assert "missing or cannot be read" in failure["reason"] and str(added.directory / IC_AUDIT) in failure["reason"]
    assert "Nothing was published" in failure["reason"] and "--abort --confirm-timer-start" in failure["reason"]
    assert stores(added) == before
    assert not {"publish-apply.json", "step-publish.json"} & set(added.names())
    # The timer stays stopped; the tool did not start it.
    assert added.systemctl.mutating() == [] and added.systemctl.state(TIMER) == "inactive"


def test_a_resume_after_a_failed_add_basin_publish_completes(
    added: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = publish_tool.publish_merged_scheduler_registry

    def refuse_the_apply(**arguments: Any) -> dict[str, Any]:
        if arguments["apply"]:
            raise publish_tool.MergedRegistryPublishError("refused: injected")
        return real(**arguments)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", refuse_the_apply)
    before = stores(added)
    assert added.main("--apply") == 1
    (failure,) = added.failures()
    assert failure["step"] == "publish" and failure["completed_steps"] == ["copyback", "preflight", "begin"]
    assert "injected" in failure["reason"] and failure["timer_stopped_by_this_tool"] is True
    assert stores(added) == before and added.systemctl.state(TIMER) == "inactive"
    target = added.directory / IC_AUDIT
    written = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
    capsys.readouterr()

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", real)
    assert added.main("--apply") == 0

    steps = report(capsys)["steps"]
    assert steps == {
        step: ("skipped" if step in ("copyback", "preflight", "begin") else "completed") for step in ADD_BASIN_STEPS
    }
    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns) == written
    assert added.model_ids(added.ws.canonical) == [*BEFORE, *ADDED]
    assert added.systemctl.state(TIMER) == "active" and len(added.failures()) == 1


def test_an_add_basin_publish_without_its_receipt_is_a_hard_stop_and_aborts_as_published(
    added: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    added.run_steps("copyback", "preflight", "begin")
    write = succession_receipt.write_receipt

    def unwritable(path: Path, receipt: Any) -> None:
        if path.name == "publish-apply.json":
            raise OSError("no space left on device")
        write(path, receipt)

    monkeypatch.setattr(succession_receipt, "write_receipt", unwritable)
    assert added.main("--apply") == 1
    monkeypatch.setattr(succession_receipt, "write_receipt", write)

    (failure,) = added.failures()
    assert failure["step"] == "publish" and failure["hard_stop"] is True
    assert "complete but has no receipt" in failure["reason"] and "is in effect" in failure["reason"]
    # The runbook section of this kind.
    assert "section 5.7.3" in failure["reason"] and "section 5.7.2" not in failure["reason"]
    assert added.model_ids(added.ws.canonical) == [*BEFORE, *ADDED]
    assert added.ws.canonical.read_bytes() == added.ws.mirror.read_bytes()

    # The rerun is stopped before any step: "reached publish" is the begin receipt of this kind.
    before = stores(added)
    added.systemctl.clear()
    capsys.readouterr()
    assert added.main("--apply") == 1
    error = capsys.readouterr().err
    assert "is in effect" in error and "provider refresh" in error and "section 5.7.3" in error
    assert stores(added) == before and added.systemctl.mutating() == [] and len(added.failures()) == 1

    assert added.main("--abort") == 0
    result = report(capsys)
    assert result["publish_state"] == "published_without_receipt"
    meaning = " ".join(result["what_this_state_means"])
    assert "new models are live" in meaning and "old models" not in meaning and "pair" not in meaning


def test_an_abort_of_an_add_basin_after_begin_starts_the_timer_and_names_no_pair_and_no_old_model(
    added: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    added.run_steps("copyback", "preflight", "begin")
    added.systemctl.clear()
    before = stores(added)

    assert added.main("--abort", "--confirm-timer-start") == 0

    result = report(capsys)
    (path,) = sorted(added.directory.glob("abort-*.json"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert result["abort_receipt"] == str(path) and receipt["aborted"] is True
    assert receipt["completed_steps"] == ["copyback", "preflight", "begin"]
    assert receipt["publish_state"] == "not_published" and receipt["timer_was_active_at_begin"] is True
    meaning = " ".join(receipt["what_this_state_means"])
    assert "The publish did not complete" in meaning and "without the models of the new basin" in meaning
    assert not [word for word in ("pair", "old model", "clone", "state index") if word in meaning.lower()]
    assert added.systemctl.mutating() == [START_TIMER] and added.systemctl.state(TIMER) == "active"
    assert stores(added) == before


# --- the dry-run -----------------------------------------------------------------------------


def test_an_add_basin_dry_run_changes_nothing_and_reports_the_audit_and_the_ids_it_would_introduce(
    added: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    added.run_steps("copyback")
    added.systemctl.clear()
    before = tree(added.ws.root)

    assert added.main() == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    notice = captured.err.lower()
    assert "dry-run" in notice and "audits their packaged initial conditions" in notice
    assert not [word for word in ("pair", "old model", "clone", "kind") if word in notice]
    assert result["dry_run"] is True and result["would_be_refused"] == []
    assert result["plan"] == {"kind": "add_basin", "adds": ADDED, "provision_succession_id": SUCCESSION_ID}
    assert sorted(result["steps"]) == sorted(ADD_BASIN_STEPS) and len(result["steps"]) == 6
    preflight = result["steps"]["preflight"]
    # Neither a clone nor a kind check: there is no pair.
    assert sorted(preflight) == ["ic_audit", "publish_dry_run"]
    assert preflight["ic_audit"] == {
        "outcome": QUALIFIED,
        "models": {model_id: [QUALIFIED, QUALIFIED] for model_id in ADDED},
    }
    publish = preflight["publish_dry_run"]
    assert publish["outcome"] == "would_publish" and publish["introduced_model_ids"] == ADDED
    assert (publish["row_count_before"], publish["row_count_after"]) == (6, 8) and "replaced" not in publish
    assert result["steps"]["copyback"] == {"status": "completed"}
    assert result["steps"]["publish"] == {"would": "run the publish apply"}
    assert "clone" not in json.dumps(result["steps"]) and "kind_check" not in json.dumps(result)
    _assert_continuity(result)
    # Not one file changed, the audit receipt included, and only queries were issued.
    assert changed(before, tree(added.ws.root)) == set()
    assert not added.directory.joinpath(IC_AUDIT).exists()
    assert list((added.ws.root / "tmp").iterdir()) == []
    assert set(added.systemctl.calls()) <= IS_ACTIVE


def test_an_add_basin_dry_run_before_the_copyback_names_neither_a_kind_check_nor_a_clone(
    added: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(added.ws.root)

    assert added.main() == 0

    result = report(capsys)
    assert sorted(result["steps"]) == sorted(ADD_BASIN_STEPS) and len(result["steps"]) == 6
    assert [package["outcome"] for package in result["steps"]["copyback"]["packages"]] == ["would_copy", "would_copy"]
    preflight = result["steps"]["preflight"]
    assert preflight["status"] == "needs copyback"
    assert preflight["note"].startswith("The initial-condition audit and the publisher's package checks need")
    assert "kind check" not in preflight["note"] and "clone" not in json.dumps(result["steps"])
    assert changed(before, tree(added.ws.root)) == set()


def test_an_add_basin_dry_run_reports_an_unqualified_initial_condition(
    added: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    (added.package(added.new_rows[1], added.ws.shared) / "huai.cfg.ic").write_bytes(UNQUALIFIED_IC)
    added.run_steps("copyback")
    before = tree(added.ws.root)

    assert added.main() == 1

    result = report(capsys)
    audit = result["steps"]["preflight"]["ic_audit"]
    assert audit["outcome"] == "refused" and f"{ADDED[1]}: ic_status unqualified" in audit["reason"]
    assert "A new basin begins" in audit["reason"] and result["would_be_refused"] == [audit["reason"]]
    assert changed(before, tree(added.ws.root)) == set()


# --- the plans of the two replacing kinds are what they were ----------------------------------

# ``continuity.notice`` of a cold start, as written before this kind existed.
COLD_START_NOTICE = (
    "Cold start: no state is carried from the old models. Each new model starts from the calibrated initial "
    "condition in its package at the first cycle the scheduler plans after the timer is started, and the "
    "hydrograph of these basins is discontinuous there. The cutover time is recorded as the operator declared "
    "it; this tool does not enforce it."
)


@pytest.mark.parametrize("kind", ["recalibration", "cold_start"])
def test_the_plan_json_of_a_replacing_kind_holds_exactly_what_it_held_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    packages = (COLD_START_PACKAGE, COLD_START_PACKAGE) if kind == "cold_start" else ({}, {})
    space = build_space(tmp_path, monkeypatch, kind=kind, new_packages=packages)
    receipt_path = space.directory / "provision-apply.json"
    provisioned = {"path": str(receipt_path), "sha256": sha256_bytes(receipt_path.read_bytes())}
    registry = {"path": str(space.registry), "sha256": sha256_bytes(space.registry.read_bytes())}

    space.run_steps()

    plan = space.receipt("plan.json")
    header = {name: plan.pop(name) for name in ("generated_at", "host", "git_commit")}
    assert all(isinstance(value, str) and value for value in header.values())
    expected: dict[str, Any] = {
        "schema_version": "nhms.model_succession.plan.v1",
        "succession_id": SUCCESSION_ID,
        "operator_id": "operator-1",
        "kind": kind,
        "pairs": [
            {"old_model_id": "dg_a_gfs_v1", "new_model_id": "dg_a_gfs_v2"},
            {"old_model_id": "dg_b_gfs_v1", "new_model_id": "dg_b_gfs_v2"},
        ],
        "cutover_time": "2026100512",
        "provision_succession_id": SUCCESSION_ID,
        "provision_apply_receipt_sha256": provisioned["sha256"],
        "new_rows_registry_sha256": registry["sha256"],
        "provision_apply_receipt": provisioned,
        "new_rows_registry": registry,
    }
    if kind == "cold_start":
        expected["continuity"] = {
            "mode": "cold_start",
            "state_carried": False,
            "declared_cutover_time": "2026100512",
            "notice": COLD_START_NOTICE,
        }
    assert plan == expected
