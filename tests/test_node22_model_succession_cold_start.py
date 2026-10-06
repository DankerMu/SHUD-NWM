"""The cold-start kind of the model succession tool, and the kind check of both kinds (#2740).

A cold start replaces models whose packages differ structurally: no state is
cloned, the packaged initial condition of every new model is audited in
``preflight`` and required again by ``publish``, and the plan, the receipts and
the reports say that the hydrograph is not continuous.  The packages are real
files, so the eight-surface fingerprint and the first-cycle audit actually run.
The fixtures, the fake ``systemctl`` and the builders are in
``tests/model_succession_helpers.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt
from packages.common.object_store import sha256_bytes
from scripts.audit_first_cycle_initial_state import SCHEMA_VERSION as IC_AUDIT_SCHEMA_VERSION
from scripts.model_succession import run as succession_run
from scripts.model_succession.model import StepFailure
from tests.model_succession_helpers import (  # noqa: F401 - fixtures
    COLD_START_PACKAGE,
    COLD_START_STEPS,
    CUTOVER,
    QUALIFIED_IC,
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
STRUCTURAL = (COLD_START_PACKAGE, COLD_START_PACKAGE)
# What the audit says of a new package of these suites: the canonical ``huai.cfg.ic`` was probed and qualifies.
QUALIFIED = "qualified"
# A header line of two numeric tokens: the shape the audit refuses.
UNQUALIFIED_IC = b"4\t6\n0.1\t0.2\n"
IC_AUDIT = "ic-audit.json"


@pytest.fixture(name="cold")
def cold_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Space:
    """A cold-start succession of two pairs whose new packages differ structurally and ship a qualified IC."""

    return build_space(tmp_path, monkeypatch, kind="cold_start", new_packages=STRUCTURAL)


def _new_ids(space: Space) -> list[str]:
    return [str(row["model_id"]) for row in space.new_rows]


def _pair_names(space: Space) -> list[str]:
    return [f"{old}:{new}" for old, new in space.pairs]


def _copy_to_compute_store(space: Space) -> None:
    for row in space.new_rows:
        shutil.copytree(space.package(row, space.ws.shared), space.package(row, space.ws.store))


def _assert_continuity(record: dict[str, Any]) -> None:
    continuity = record["continuity"]
    notice = continuity.pop("notice")
    assert continuity == {"mode": "cold_start", "state_carried": False, "declared_cutover_time": CUTOVER}
    assert "no state is carried from the old models" in notice
    assert "calibrated initial condition in its package" in notice
    assert "first cycle the scheduler plans after the timer is started" in notice
    assert "discontinuous" in notice and "does not enforce" in notice


def _refused_in_preflight(space: Space, before: dict[str, bytes], *mode: str, **overrides: Any) -> str:
    """An apply that fails in ``preflight``: the timer was never stopped and nothing was published or cloned."""

    assert space.main("--apply", *mode, **overrides) == 1
    (failure,) = space.failures()
    assert failure["step"] == "preflight" and failure["hard_stop"] is False
    assert failure["completed_steps"] == ["copyback"]
    assert failure["timer_touched_by_this_tool"] is False and failure["timer_stopped_by_this_tool"] is False
    assert space.systemctl.mutating() == [] and space.systemctl.state(TIMER) == "active"
    assert "timer-before-stop.json" not in space.names() and "step-preflight.json" not in space.names()
    assert stores(space) == before
    return str(failure["reason"])


# --- the cold-start apply -----------------------------------------------------------------


def test_a_cold_start_apply_runs_its_six_steps_without_a_clone_and_touches_no_state_index(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    indexes = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (cold.canonical_index, cold.mirror_index)}
    old_ids = [row["model_id"] for row in cold.old_rows]

    assert cold.main("--apply") == 0

    result = report(capsys)
    assert result["steps"] == dict.fromkeys(COLD_START_STEPS, "completed")
    assert result["outcome"] == "completed" and result["timer_action"] == "started"
    _assert_continuity(result)
    # No clone step, no clone receipt of either sort.
    assert cold.names() == sorted(
        [
            "plan.json",
            "provision-apply.json",
            "timer-before-stop.json",
            IC_AUDIT,
            "publish-dry-run.json",
            "publish-apply.json",
            *(f"step-{step}.json" for step in COLD_START_STEPS),
        ]
    )
    receipts = [cold.receipt(f"step-{step}.json") for step in COLD_START_STEPS]
    assert [receipt["step"] for receipt in receipts] == list(COLD_START_STEPS)
    assert [receipt["generated_at"] for receipt in receipts] == sorted(receipt["generated_at"] for receipt in receipts)

    # Neither state index was written, and neither holds a clone row.
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in indexes} == indexes
    assert cold.clone_rows(cold.canonical_index) == cold.clone_rows(cold.mirror_index) == []

    # Both manifests are equal and hold the new ids in place of the old ones.
    assert cold.ws.canonical.read_bytes() == cold.ws.mirror.read_bytes()
    ids = cold.model_ids(cold.ws.canonical)
    assert ids == [*_new_ids(cold), cold.ws.rows[2]["model_id"]] and not set(old_ids) & set(ids)

    # The audit's own receipt, written once, and the preflight receipt that points at it.
    audit = cold.receipt(IC_AUDIT)
    assert audit["schema_version"] == IC_AUDIT_SCHEMA_VERSION and audit["outcome"] == "completed"
    assert audit["inputs"]["registry_manifest"] == str(cold.registry) and audit["inputs"]["sources"] == ["gfs"]
    assert [(row["model_id"], row["ic_status"]) for row in audit["rows"]] == [
        (model_id, QUALIFIED) for model_id in _new_ids(cold)
    ]
    assert {row["ic_sha256"] for row in audit["rows"]} == {sha256_bytes(QUALIFIED_IC)}
    preflight = cold.receipt("step-preflight.json")
    assert preflight["ic_audit"] == {
        "path": str(cold.directory / IC_AUDIT),
        "sha256": succession_receipt.file_sha256(cold.directory / IC_AUDIT),
        "reused": False,
        "models": {model_id: [QUALIFIED] for model_id in _new_ids(cold)},
    }
    assert "clone_dry_run" not in preflight
    assert [pair["state_compatible"] for pair in preflight["kind_check"]["pairs"]] == [False, False]

    plan = cold.receipt("plan.json")
    assert plan["kind"] == "cold_start" and plan["cutover_time"] == CUTOVER
    for record in (plan, cold.receipt("step-publish.json"), cold.receipt("step-finish.json")):
        _assert_continuity(record)

    # One stop, one refresh start, one timer start; everything before the stop was a query.
    assert cold.systemctl.mutating() == [STOP, START_REFRESH, START_TIMER]
    assert set(cold.systemctl.calls()[: cold.systemctl.calls().index(STOP)]) <= IS_ACTIVE
    assert cold.systemctl.state(TIMER) == "active"


def test_a_cold_start_publish_before_begin_is_refused_and_neither_manifest_changes(cold: Space) -> None:
    settings, inputs = cold.run_steps("copyback", "preflight")
    before = stores(cold)

    with pytest.raises(StepFailure, match="requires the receipt of the begin step") as raised:
        succession_run.run_step(settings, inputs, "publish")

    assert str(cold.directory / "step-begin.json") in str(raised.value)
    assert stores(cold) == before and "publish-apply.json" not in cold.names()


def test_the_notice_text_is_not_part_of_what_a_resume_compares(cold: Space) -> None:
    cold.run_steps("copyback")
    path = cold.directory / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["continuity"]["notice"] = "the wording of an earlier version"
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert cold.main("--apply") == 0


# --- the kind must match what changed between the packages ------------------------------------


def test_a_cold_start_of_state_compatible_packages_is_refused_in_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recalibration packages: only cfg.calib, CALIB/ and cfg.para differ.
    space = build_space(tmp_path, monkeypatch, kind="cold_start")

    reason = _refused_in_preflight(space, stores(space))

    assert "--kind recalibration" in reason and "--kind cold_start for pair(s)" in reason
    assert all(pair in reason for pair in _pair_names(space))
    assert "new --succession-id" in reason and f"--provision-succession-id {SUCCESSION_ID}" in reason
    assert IC_AUDIT not in space.names() and "publish-dry-run.json" not in space.names()


def test_a_cold_start_with_one_state_compatible_pair_is_refused_and_told_to_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    space = build_space(tmp_path, monkeypatch, kind="cold_start", new_packages=(COLD_START_PACKAGE, {}))
    structural, compatible = _pair_names(space)

    reason = _refused_in_preflight(space, stores(space))

    assert compatible in reason and structural not in reason
    assert "--kind recalibration" in reason and "split it into two successions" in reason


@pytest.mark.parametrize("cutover", [CUTOVER, "2026100600"], ids=["source_state_present", "no_source_state"])
def test_a_recalibration_of_structurally_different_packages_is_refused_in_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cutover: str
) -> None:
    # Without a qualified source state at the cutover time the clone gate would refuse for that, and never
    # reach the surfaces: the kind check comes before it and names the way on in both cases.
    space = build_space(tmp_path, monkeypatch, new_packages=STRUCTURAL)

    reason = _refused_in_preflight(space, stores(space), cutover=cutover)

    assert "--kind cold_start" in reason and "structural" in reason
    assert all(pair in reason for pair in _pair_names(space))
    assert "new --succession-id" in reason
    assert "clone-dry-run.json" not in space.names() and "state clone refused" not in reason


def _package_root_missing(space: Space) -> None:
    shutil.rmtree(space.package(space.old_rows[0], space.ws.store))


def _no_cfg_para(space: Space) -> None:
    (space.package(space.old_rows[0], space.ws.store) / "huai.cfg.para").unlink()


def _two_cfg_ic(space: Space) -> None:
    (space.package(space.new_rows[0], space.ws.shared) / "CALIB" / "other.cfg.ic").write_bytes(QUALIFIED_IC)


def _unparsable_sp_att(space: Space) -> None:
    (space.package(space.old_rows[0], space.ws.store) / "huai.sp.att").write_bytes(b"not a table\n")


NOT_COMPARABLE = {
    "package_root_missing": (_package_root_missing, "is missing in the compute store"),
    "no_cfg_para": (_no_cfg_para, "Expected exactly one '*.cfg.para'"),
    "two_cfg_ic": (_two_cfg_ic, "Expected exactly one '*.cfg.ic'"),
    "unparsable_sp_att": (_unparsable_sp_att, "huai.sp.att"),
}


@pytest.mark.parametrize("case", sorted(NOT_COMPARABLE))
@pytest.mark.parametrize("kind", ["recalibration", "cold_start"])
def test_a_pair_that_cannot_be_compared_is_a_step_failure_and_not_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, case: str
) -> None:
    # Under each kind the packages that kind accepts, so that only the broken comparison can stop the run.
    space = build_space(
        tmp_path, monkeypatch, kind=kind, new_packages=STRUCTURAL if kind == "cold_start" else ({}, {})
    )
    arrange, expected = NOT_COMPARABLE[case]
    arrange(space)

    reason = _refused_in_preflight(space, stores(space))

    assert "cannot be compared" in reason and expected in reason and space.pairs[0][0] in reason
    # Neither "equal" nor "unequal" was concluded: the other kind is not advised.
    assert "--kind" not in reason
    assert not {"clone-dry-run.json", IC_AUDIT, "publish-dry-run.json"} & set(space.names())


def test_a_surface_file_on_one_side_only_is_unequal_as_in_the_clone_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The recalibration packages, except that the new one lost its lake file.
    space = build_space(tmp_path, monkeypatch)
    (space.package(space.new_rows[1], space.ws.shared) / "huai.lake.att").unlink()

    reason = _refused_in_preflight(space, stores(space))

    assert "--kind cold_start" in reason and _pair_names(space)[1] in reason
    assert _pair_names(space)[0] not in reason and "split it into two successions" in reason


# --- the initial-condition audit -------------------------------------------------------------


def _unqualified(space: Space) -> None:
    (space.package(space.new_rows[1], space.ws.shared) / "huai.cfg.ic").write_bytes(UNQUALIFIED_IC)


def _absent(space: Space) -> None:
    """The second new row publishes no package manifest reference; the publisher would not write such a
    registry, so it is written by hand, with its sha256 in the provision apply receipt."""

    registry = json.loads(space.registry.read_text(encoding="utf-8"))
    row = registry["models"][1]
    assert row["model_id"] == space.new_rows[1]["model_id"]
    row.pop("manifest_uri", None)
    row["resource_profile"].pop("manifest_uri", None)
    space.registry.write_text(json.dumps(registry), encoding="utf-8")
    receipt_path = space.directory / "provision-apply.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["output_registry"]["sha256"] = sha256_bytes(space.registry.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


@pytest.mark.parametrize(
    ("arrange", "status"), [(_unqualified, "unqualified"), (_absent, "absent")], ids=["unqualified", "absent"]
)
def test_a_new_model_without_a_qualified_initial_condition_is_refused_in_preflight(
    cold: Space, arrange: Any, status: str
) -> None:
    arrange(cold)
    good, bad = _new_ids(cold)

    reason = _refused_in_preflight(cold, stores(cold))

    assert f"{bad}: ic_status {status}" in reason and good not in reason
    assert f"No {IC_AUDIT} was written" in reason
    # A failing audit leaves no file behind: nothing stands in the way of the corrected run.
    assert IC_AUDIT not in cold.names() and "publish-dry-run.json" not in cold.names()


def test_the_same_command_passes_the_audit_after_the_package_was_repaired(cold: Space) -> None:
    _unqualified(cold)
    _refused_in_preflight(cold, stores(cold))

    # The copyback step is done: the package the runs will read is the one on the compute store.
    for root in (cold.ws.shared, cold.ws.store):
        (cold.package(cold.new_rows[1], root) / "huai.cfg.ic").write_bytes(QUALIFIED_IC)

    assert cold.main("--apply") == 0
    assert cold.receipt("step-preflight.json")["ic_audit"]["reused"] is False


def test_a_blocked_audit_is_a_step_failure(cold: Space, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.model_succession import tools

    def blocked(**_arguments: Any) -> dict[str, Any]:
        raise tools.ic_audit_tool.AuditBlocked("registry manifest is unreadable: injected")

    monkeypatch.setattr(tools.ic_audit_tool, "build_receipt", blocked)

    reason = _refused_in_preflight(cold, stores(cold))

    assert "was blocked (REGISTRY_UNREADABLE)" in reason and "injected" in reason
    assert IC_AUDIT not in cold.names()


def test_a_resumed_preflight_reads_the_audit_receipt_and_does_not_rewrite_it(
    cold: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = publish_tool.publish_merged_scheduler_registry

    def refuse(**_arguments: Any) -> dict[str, Any]:
        raise publish_tool.MergedRegistryPublishError("refused: injected")

    # The audit passes and writes its receipt; the publish dry-run after it refuses.
    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", refuse)
    assert "injected" in _refused_in_preflight(cold, stores(cold))
    target = cold.directory / IC_AUDIT
    written = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", real)
    assert cold.main("--apply") == 0

    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns) == written
    assert cold.receipt("step-preflight.json")["ic_audit"]["reused"] is True


def test_an_existing_audit_receipt_that_fails_the_gate_refuses_preflight_and_is_left_alone(cold: Space) -> None:
    cold.run_steps("copyback")
    receipt = {"rows": [{"model_id": _new_ids(cold)[0], "ic_status": QUALIFIED}]}
    target = cold.directory / IC_AUDIT
    target.write_text(json.dumps(receipt), encoding="utf-8")
    planted = target.read_bytes()

    assert cold.main("--apply") == 1

    failure = cold.failures()[-1]
    assert failure["step"] == "preflight" and f"{_new_ids(cold)[1]}: no audit row" in failure["reason"]
    assert "never overwritten" in failure["reason"] and target.read_bytes() == planted
    assert cold.systemctl.mutating() == []


# --- publish requires the audit receipt -------------------------------------------------------


def _remove(path: Path) -> str:
    path.unlink()
    return "missing or cannot be read"


def _alter(path: Path) -> str:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["rows"][0]["ic_status"] = "unqualified"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return "changed after preflight"


@pytest.mark.parametrize("damage", [_remove, _alter], ids=["removed", "altered"])
def test_publish_refuses_without_the_audit_receipt_preflight_recorded(cold: Space, damage: Any) -> None:
    cold.run_steps("copyback", "preflight", "begin")
    expected = damage(cold.directory / IC_AUDIT)
    before = stores(cold)
    cold.systemctl.clear()

    assert cold.main("--apply") == 1

    (failure,) = cold.failures()
    assert failure["step"] == "publish" and failure["hard_stop"] is False
    assert expected in failure["reason"] and str(cold.directory / IC_AUDIT) in failure["reason"]
    assert "Nothing was published" in failure["reason"] and "--abort --confirm-timer-start" in failure["reason"]
    assert stores(cold) == before
    assert not {"publish-apply.json", "step-publish.json"} & set(cold.names())
    # The timer stays stopped; the tool did not start it.
    assert cold.systemctl.mutating() == [] and cold.systemctl.state(TIMER) == "inactive"


def test_an_adopted_publish_receipt_does_not_need_the_audit_receipt_again(
    cold: Space, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The publish apply completed and the run was killed before step-publish.json: the resume adopts it.
    settings, inputs = cold.run_steps("copyback", "preflight", "begin")
    from scripts.model_succession import tools

    tools.publish(settings, inputs)
    (cold.directory / IC_AUDIT).unlink()
    monkeypatch.setattr(
        publish_tool, "publish_merged_scheduler_registry", lambda **_arguments: pytest.fail("published again")
    )

    assert cold.main("--apply") == 0
    assert cold.receipt("step-publish.json")["adopted_existing_receipt"] is True


def test_a_resume_after_a_failed_publish_completes_without_rewriting_the_audit_receipt(
    cold: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = publish_tool.publish_merged_scheduler_registry

    def refuse_the_apply(**arguments: Any) -> dict[str, Any]:
        if arguments["apply"]:
            raise publish_tool.MergedRegistryPublishError("refused: injected")
        return real(**arguments)

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", refuse_the_apply)
    before = stores(cold)
    assert cold.main("--apply") == 1
    (failure,) = cold.failures()
    assert failure["step"] == "publish" and failure["completed_steps"] == ["copyback", "preflight", "begin"]
    assert stores(cold) == before
    target = cold.directory / IC_AUDIT
    written = (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns)
    capsys.readouterr()

    monkeypatch.setattr(publish_tool, "publish_merged_scheduler_registry", real)
    assert cold.main("--apply") == 0

    steps = report(capsys)["steps"]
    assert steps == {
        step: ("skipped" if step in ("copyback", "preflight", "begin") else "completed") for step in COLD_START_STEPS
    }
    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mtime_ns) == written
    assert cold.systemctl.state(TIMER) == "active" and len(cold.failures()) == 1


# --- the dry-run -----------------------------------------------------------------------------


def test_a_cold_start_dry_run_changes_nothing_and_reports_the_audit_and_the_continuity(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_to_compute_store(cold)
    before = tree(cold.ws.root)

    assert cold.main() == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert "DRY-RUN" in captured.err and "clones" not in captured.err and "not continuous" in captured.err
    assert result["dry_run"] is True and result["would_be_refused"] == []
    assert sorted(result["steps"]) == sorted(COLD_START_STEPS) and "clone" not in result["steps"]
    preflight = result["steps"]["preflight"]
    assert sorted(preflight) == ["ic_audit", "kind_check", "publish_dry_run"]
    assert preflight["kind_check"]["outcome"] == "matches_kind"
    assert preflight["ic_audit"] == {
        "outcome": QUALIFIED,
        "models": {model_id: [QUALIFIED] for model_id in _new_ids(cold)},
    }
    assert preflight["publish_dry_run"]["outcome"] == "would_publish"
    assert result["steps"]["publish"] == {"would": "run the publish apply"}
    _assert_continuity(result)
    # Not one file changed, the audit receipt included, and only queries were issued.
    assert changed(before, tree(cold.ws.root)) == set()
    assert not cold.directory.joinpath(IC_AUDIT).exists()
    assert list((cold.ws.root / "tmp").iterdir()) == []
    assert set(cold.systemctl.calls()) <= IS_ACTIVE


def test_a_cold_start_dry_run_before_the_copyback_lists_no_clone_step(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    before = tree(cold.ws.root)

    assert cold.main() == 0

    result = report(capsys)
    assert sorted(result["steps"]) == sorted(COLD_START_STEPS)
    assert result["steps"]["preflight"]["status"] == "needs copyback"
    assert "initial-condition audit" in result["steps"]["preflight"]["note"]
    assert "clone" not in json.dumps(result["steps"])
    assert changed(before, tree(cold.ws.root)) == set()


def test_a_cold_start_dry_run_reports_an_unqualified_initial_condition(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    _unqualified(cold)
    _copy_to_compute_store(cold)
    before = tree(cold.ws.root)

    assert cold.main() == 1

    result = report(capsys)
    audit = result["steps"]["preflight"]["ic_audit"]
    assert audit["outcome"] == "refused" and f"{_new_ids(cold)[1]}: ic_status unqualified" in audit["reason"]
    assert result["would_be_refused"] == [audit["reason"]]
    assert result["steps"]["preflight"]["publish_dry_run"]["outcome"] == "would_publish"
    assert changed(before, tree(cold.ws.root)) == set()


def test_a_recalibration_dry_run_of_a_structural_pair_reports_the_kind_and_both_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    space = build_space(tmp_path, monkeypatch, new_packages=STRUCTURAL)
    _copy_to_compute_store(space)
    before = tree(space.ws.root)

    assert space.main() == 1

    result = report(capsys)
    preflight = result["steps"]["preflight"]
    kind = preflight["kind_check"]
    assert kind["outcome"] == "refused" and "--kind cold_start" in kind["reason"]
    # The two tools' dry-runs still ran and are reported as before.
    clone = preflight["clone_dry_run"]
    assert clone["outcome"] == "refused" and "state clone refused" in clone["reason"]
    assert preflight["publish_dry_run"]["outcome"] == "would_publish"
    assert result["would_be_refused"] == [kind["reason"], clone["reason"]]
    assert "continuity" not in result and "clone" in result["steps"]
    assert changed(before, tree(space.ws.root)) == set()
    assert set(space.systemctl.calls()) <= IS_ACTIVE


# --- abort, and a publish that is past its begin ------------------------------------------------


def test_an_abort_of_a_cold_start_after_begin_starts_the_timer_and_names_no_clone_rows(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    cold.run_steps("copyback", "preflight", "begin")
    cold.systemctl.clear()
    before = stores(cold)

    assert cold.main("--abort", "--confirm-timer-start") == 0

    result = report(capsys)
    (path,) = sorted(cold.directory.glob("abort-*.json"))
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert result["abort_receipt"] == str(path) and receipt["aborted"] is True
    assert receipt["completed_steps"] == ["copyback", "preflight", "begin"]
    assert receipt["publish_state"] == "not_published" and receipt["timer_was_active_at_begin"] is True
    meaning = " ".join(receipt["what_this_state_means"])
    assert "keeps running the old models" in meaning
    assert "clone" not in meaning.lower() and "state index" not in meaning
    assert cold.systemctl.mutating() == [START_TIMER] and cold.systemctl.state(TIMER) == "active"
    assert stores(cold) == before


def test_a_cold_start_publish_without_its_receipt_is_a_hard_stop_and_aborts_as_published(
    cold: Space, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cold.run_steps("copyback", "preflight", "begin")
    write = succession_receipt.write_receipt

    def unwritable(path: Path, receipt: Any) -> None:
        if path.name == "publish-apply.json":
            raise OSError("no space left on device")
        write(path, receipt)

    monkeypatch.setattr(succession_receipt, "write_receipt", unwritable)
    assert cold.main("--apply") == 1
    monkeypatch.setattr(succession_receipt, "write_receipt", write)

    (failure,) = cold.failures()
    assert failure["step"] == "publish" and failure["hard_stop"] is True
    assert "complete but has no receipt" in failure["reason"] and "is in effect" in failure["reason"]
    # The runbook section of this kind.
    assert "section 5.7.2" in failure["reason"] and "section 5.7.1" not in failure["reason"]
    assert set(_new_ids(cold)) <= set(cold.model_ids(cold.ws.canonical))
    assert cold.ws.canonical.read_bytes() == cold.ws.mirror.read_bytes()

    # The rerun is stopped before any step: "reached publish" is the begin receipt of a cold start.
    before = stores(cold)
    cold.systemctl.clear()
    capsys.readouterr()
    assert cold.main("--apply") == 1
    error = capsys.readouterr().err
    assert "is in effect" in error and "provider refresh" in error and "section 5.7.2" in error
    assert stores(cold) == before and cold.systemctl.mutating() == [] and len(cold.failures()) == 1

    assert cold.main("--abort") == 0
    result = report(capsys)
    assert result["publish_state"] == "published_without_receipt"
    meaning = " ".join(result["what_this_state_means"])
    assert "new models are live" in meaning and "old models" not in meaning


@pytest.mark.parametrize("mode", [(), ("--apply",)], ids=["dry_run", "apply"])
def test_a_cold_start_past_its_begin_is_refused_while_the_manifests_differ(
    cold: Space, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...]
) -> None:
    cold.run_steps("copyback", "preflight", "begin")
    # What a publish killed between its two writes leaves: the canonical manifest published, the mirror not.
    cold.ws.canonical.write_text(json.dumps({"models": [*cold.new_rows, cold.ws.rows[2]]}), encoding="utf-8")
    cold.systemctl.clear()
    before = tree(cold.ws.root)
    capsys.readouterr()

    assert cold.main(*mode) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and "Nothing was written" in captured.err
    assert "manifests differ" in captured.err and str(cold.ws.mirror) in captured.err
    assert ".bak-" in captured.err and "section 5.7.2" in captured.err
    assert changed(before, tree(cold.ws.root)) == set() and cold.failures() == []
    assert cold.systemctl.mutating() == []

    # And the abort says the same instead of "not published".
    assert cold.main("--abort") == 0
    assert report(capsys)["publish_state"] == "manifests_differ"


def test_a_cold_start_before_its_begin_is_not_refused_for_manifests_another_succession_left(
    cold: Space, capsys: pytest.CaptureFixture[str]
) -> None:
    # Before begin this succession is not at its publish: differing manifests are the publish dry-run's to refuse.
    cold.run_steps("copyback")
    cold.ws.mirror.write_text(json.dumps({"models": cold.ws.rows[:1]}), encoding="utf-8")

    assert cold.main("--apply") == 1

    (failure,) = cold.failures()
    assert failure["step"] == "preflight" and "The publish dry-run refused" in failure["reason"]
    assert cold.systemctl.mutating() == []
