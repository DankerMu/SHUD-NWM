"""The steps that call the clone tool and the publish tool: ``preflight``, ``clone`` and ``publish``.

Part of ``scripts/node22_model_succession.py`` (the entry point).  Both tools
are called in-process through their own entry functions, with their own
receipts; this module adds no option to either and decides only what their
outcome means for the succession.  ``preflight`` also holds the kind of the
succession to what changed between the packages of each pair and, for a cold
start and for an added basin, audits the packaged initial condition of every
new model.  An added basin has no pair: its kind is not checked.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import scripts.audit_first_cycle_initial_state as ic_audit_tool
import scripts.node22_clone_direct_grid_cutover_states as clone_tool
import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt as succession
from packages.common.source_identity import normalize_source_id
from scripts.model_succession import copyback
from scripts.model_succession import plan as planning
from scripts.model_succession.model import (
    ABORT_BEFORE_NEW_ID,
    CLONE_APPLY_NAME,
    CLONE_DRY_RUN_NAME,
    IC_AUDIT_NAME,
    KIND_ADD_BASIN,
    KIND_COLD_START,
    KIND_RECALIBRATION,
    KINDS_STARTING_FROM_PACKAGED_IC,
    HardStop,
    Inputs,
    Settings,
    StepFailure,
    read_json,
    utc_now,
    utc_text,
)
from workers.data_adapters.base import parse_cycle_time
from workers.mapping_builder.rewrite import (
    STATE_COMPATIBILITY_SURFACES,
    HydrologicCoreFingerprintMismatchError,
    MissingPackageFileError,
    verify_hydrologic_core_fingerprint_equal,
)


def _manual(settings: Settings) -> str:
    return f"This tool does not retry it; continue by hand from the runbook ({settings.plan.runbook})."


def _record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": succession.file_sha256(path)}


def _read_receipt(settings: Settings, path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except (OSError, ValueError) as error:
        raise HardStop(f"The receipt {path} exists but cannot be read ({error}). {_manual(settings)}") from error


# --- the kind check ----------------------------------------------------------------


def _old_rows(settings: Settings) -> dict[str, dict[str, Any]]:
    """The rows of the canonical manifest by model_id: where the old model of a pair is registered."""

    try:
        models = read_json(settings.canonical_manifest).get("models")
    except (OSError, ValueError) as error:
        raise StepFailure(
            f"The packages cannot be compared: cannot read the canonical manifest {settings.canonical_manifest} "
            f"({error})."
        ) from error
    return {
        str(row["model_id"]): dict(row)
        for row in (models if isinstance(models, list) else [])
        if isinstance(row, Mapping) and row.get("model_id")
    }


def _compute_package_root(settings: Settings, row: Mapping[str, Any]) -> Path:
    _source, root = copyback.package_paths(settings, dict(row))
    if not root.is_dir():
        raise StepFailure(f"the package of {row.get('model_id')} is missing in the compute store: {root}")
    return root


def _state_compatible(
    settings: Settings, old: str, old_row: Mapping[str, Any] | None, new_row: Mapping[str, Any]
) -> bool:
    """Whether the two packages of a pair are equal on the eight state-compatibility surfaces.

    The comparison is the gate of the recalibration clone: the clone tool builds the inputs and the gate's own
    function compares them.  A surface file present on one side only is "unequal", as in the gate.  Whatever
    prevents the comparison is a ``StepFailure`` and never a result.
    """

    pair = f"{old}:{new_row['model_id']}"
    try:
        if old_row is None:
            raise StepFailure(
                f"old model_id {old} is not a row of the canonical manifest {settings.canonical_manifest}"
            )
        old_root = _compute_package_root(settings, old_row)
        new_root = _compute_package_root(settings, new_row)
        gate = clone_tool.state_compatibility_gate_inputs(old_root, new_root)
        verify_hydrologic_core_fingerprint_equal(
            old_root,
            new_root,
            baseline_sp_att_path=gate.source_sp_att,
            variant_sp_att_path=gate.target_sp_att,
            category_files=gate.category_files,
            baseline_state_schema_bytes=gate.source_state_schema_bytes,
            variant_state_schema_bytes=gate.target_state_schema_bytes,
            baseline_solver_config_bytes=gate.source_solver_config_bytes,
            variant_solver_config_bytes=gate.target_solver_config_bytes,
            surfaces=STATE_COMPATIBILITY_SURFACES,
        )
    except (HydrologicCoreFingerprintMismatchError, MissingPackageFileError):
        return False
    except Exception as error:  # noqa: BLE001 - whatever prevents the comparison stops the step
        reason = str(error) if isinstance(error, StepFailure) else f"{type(error).__name__}: {error}"
        raise StepFailure(
            f"The packages of pair {pair} cannot be compared on the state-compatibility surfaces, so whether "
            f"this succession is a recalibration or a cold start is unknown: {reason}. Nothing is concluded "
            "from a comparison that was not made; repair the package or the row and run the same command."
        ) from error
    return True


def _kind_check(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """Hold the kind of the succession to what changed between the two packages of every pair."""

    plan = settings.plan
    old_rows = _old_rows(settings)
    pairs = [
        {
            "old_model_id": old,
            "new_model_id": new,
            "state_compatible": _state_compatible(settings, old, old_rows.get(old), inputs.new_rows[new]),
        }
        for old, new in plan.pairs
    ]
    # A recalibration needs every pair state-compatible; a cold start needs none of them to be.
    wrong = [pair for pair in pairs if pair["state_compatible"] is (plan.kind == KIND_COLD_START)]
    if not wrong:
        return {"outcome": "matches_kind", "pairs": pairs}
    names = ", ".join(f"{pair['old_model_id']}:{pair['new_model_id']}" for pair in wrong)
    new_id = (
        f"a new --succession-id and --provision-succession-id {plan.provision_succession_id} (the plan of "
        f"{plan.succession_id!r} records its kind)"
    )
    split = "" if len(wrong) == len(pairs) else (
        " The other pairs of this command line do match its kind: split it into two successions, one per kind."
    )
    if plan.kind == KIND_COLD_START:
        raise StepFailure(
            f"Refused: --kind {KIND_COLD_START} for pair(s) whose packages are equal on the state-compatibility "
            f"surfaces: {names}. Such a pair is a recalibration: the state of the old model can be carried, and a "
            f"cold start would throw it away. Run these pairs with --kind {KIND_RECALIBRATION}, {new_id}.{split}"
        )
    raise StepFailure(
        f"Refused: --kind {KIND_RECALIBRATION} for pair(s) whose packages differ on the state-compatibility "
        f"surfaces: {names}. Such a change is structural: the state of the old model cannot be carried and the "
        f"clone gate would refuse it. Run these pairs with --kind {KIND_COLD_START}, {new_id}.{split}"
    )


# --- the initial-condition audit of a cold start and of an added basin ---------------

# What the audit refusals call the succession, by kind.
_AUDITED_KIND_NAMES = {KIND_COLD_START: "cold start", KIND_ADD_BASIN: "new basin"}


def _audit_sources(inputs: Inputs) -> tuple[str, ...]:
    sources: list[str] = []
    for model_id, row in inputs.new_rows.items():
        profile = row.get("resource_profile")
        source = profile.get("direct_grid_source_id") if isinstance(profile, Mapping) else None
        if not source:
            raise StepFailure(
                f"The initial-condition audit cannot run: new model {model_id} has no "
                "resource_profile.direct_grid_source_id in the new-rows registry."
            )
        normalized = normalize_source_id(str(source))
        if normalized not in sources:
            sources.append(normalized)
    return tuple(sources)


def _run_ic_audit(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """The audit's own receipt for the new-rows registry, read against the compute store; writes nothing."""

    try:
        return ic_audit_tool.build_receipt(
            registry_manifest=Path(inputs.new_rows_registry["path"]),
            object_store_root=settings.object_store_root,
            object_store_prefix=settings.object_store_prefix,
            workspace_root=None,
            sources=_audit_sources(inputs),
            generated_at=utc_now(),
        )
    except StepFailure:
        raise
    except ic_audit_tool.AuditBlocked as error:
        raise StepFailure(
            f"The initial-condition audit of {inputs.new_rows_registry['path']} was blocked ({error.reason}): "
            f"{error}. No {IC_AUDIT_NAME} was written."
        ) from error
    except Exception as error:  # noqa: BLE001 - whatever stops the audit stops the step, and a dry-run reports it
        raise StepFailure(
            f"The initial-condition audit of {inputs.new_rows_registry['path']} could not run: "
            f"{type(error).__name__}: {error}. No {IC_AUDIT_NAME} was written."
        ) from error


def _ic_statuses(settings: Settings, receipt: Mapping[str, Any]) -> dict[str, list[str]]:
    """``ic_status`` of every audit row of each new model of the plan; rows of other models are not gated."""

    rows = receipt.get("rows")
    statuses: dict[str, list[str]] = {model_id: [] for model_id in settings.plan.new_ids}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("model_id") in statuses:
            statuses[str(row["model_id"])].append(str(row.get("ic_status")))
    return statuses


def _ic_findings(settings: Settings, receipt: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """``(source, ic_status, ic_sha256)`` of every audit row of each new model of the plan, as sorted text."""

    rows = receipt.get("rows")
    findings: dict[str, list[tuple[str, str]]] = {model_id: [] for model_id in settings.plan.new_ids}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("model_id") in findings:
            found = f"ic_status {row.get('ic_status')}, ic_sha256 {row.get('ic_sha256')}"
            findings[str(row["model_id"])].append((str(row.get("source")), found))
    return {model_id: sorted(found) for model_id, found in findings.items()}


def _refuse_changed_since_audit(settings: Settings, inputs: Inputs, target: Path, receipt: Mapping[str, Any]) -> None:
    """On a resume: the packages in the compute store must still be what ``ic-audit.json`` recorded.

    The audit runs again, read-only.  The receipt is never rewritten, so a package that changed after it was
    written cannot be audited again under this succession id.
    """

    recorded = _ic_findings(settings, receipt)
    found = _ic_findings(settings, _run_ic_audit(settings, inputs))
    changed = [
        f"{model_id}: recorded {[text for _source, text in recorded[model_id]]}, "
        f"now {[text for _source, text in found[model_id]]}"
        for model_id in settings.plan.new_ids
        if recorded[model_id] != found[model_id]
    ]
    if changed:
        plan = settings.plan
        raise StepFailure(
            f"Refused: the package in the compute store changed after {target} was written: {'; '.join(changed)}. "
            "The audit receipt no longer describes the initial condition the runs would start from, and a receipt "
            "is never rewritten. Check what changed the package, then run the plan under a new --succession-id "
            f"with --provision-succession-id {plan.provision_succession_id}, which audits it again."
        )


def _ic_gate_failures(statuses: Mapping[str, list[str]]) -> list[str]:
    """Why the audit does not qualify every new model; empty when it does."""

    failures: list[str] = []
    for model_id, found in statuses.items():
        if not found:
            failures.append(f"{model_id}: no audit row")
        elif any(status != ic_audit_tool.PACKAGED_IC_QUALIFIED for status in found):
            failures.append(f"{model_id}: ic_status {', '.join(sorted(set(found)))}")
    return failures


def _ic_refusal(settings: Settings, failures: list[str]) -> str:
    return (
        "the packaged initial condition is not qualified for every new model of the plan (required: at least one "
        f"audit row per model, each with ic_status {ic_audit_tool.PACKAGED_IC_QUALIFIED!r}): {'; '.join(failures)}. "
        f"A {_AUDITED_KIND_NAMES[settings.plan.kind]} begins from the calibrated initial condition in the package, "
        "so the scheduler would block or cold-start these models."
    )


def _ic_audit(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """Audit once and write ``ic-audit.json`` only when the gate passes.

    A resume reads the receipt, holds it to the same gate and audits again, read-only, to see that the
    packages are still the ones it recorded.
    """

    target = settings.directory / IC_AUDIT_NAME
    reused = os.path.lexists(target)
    if reused:
        try:
            receipt = read_json(target)
        except (OSError, ValueError) as error:
            raise StepFailure(
                f"Refused: {target} exists but cannot be read ({error}). A receipt is never overwritten; move it "
                "away after checking where it came from and run the same command."
            ) from error
    else:
        receipt = _run_ic_audit(settings, inputs)
    statuses = _ic_statuses(settings, receipt)
    failures = _ic_gate_failures(statuses)
    if failures:
        left = f"{target} is left as it is and never overwritten" if reused else f"No {IC_AUDIT_NAME} was written"
        raise StepFailure(
            f"Refused: {_ic_refusal(settings, failures)} {left}; repair the package (its manifest reference, its "
            "<shud_input_name>.cfg.ic and the header line of that file) and run the same command."
        )
    if reused:
        _refuse_changed_since_audit(settings, inputs, target, receipt)
    else:
        succession.write_receipt(target, receipt)
    return {**_record(target), "reused": reused, "models": statuses}


def _require_ic_audit(settings: Settings) -> None:
    """Before the publish apply of a cold start or an added basin: the audit receipt of ``preflight``, unchanged
    and still passing."""

    target = settings.directory / IC_AUDIT_NAME
    preflight = settings.step_receipt("preflight")
    nothing = f"Nothing was published. A receipt is never rewritten: restore {target}, or give this succession up."
    try:
        recorded = read_json(preflight).get("ic_audit")
        recorded_sha256 = recorded.get("sha256") if isinstance(recorded, Mapping) else None
        sha256 = succession.file_sha256(target)
        receipt = read_json(target)
    except (OSError, ValueError) as error:
        raise StepFailure(
            f"Refused: the publish of a {_AUDITED_KIND_NAMES[settings.plan.kind]} requires the initial-condition "
            f"audit receipt {target} as {preflight} recorded it, and one of them is missing or cannot be read "
            f"({error}). {nothing} "
            f"{ABORT_BEFORE_NEW_ID}"
        ) from error
    if not recorded_sha256 or sha256 != recorded_sha256:
        raise StepFailure(
            f"Refused: {target} has sha256 {sha256}, but {preflight} recorded {recorded_sha256} for the audit "
            f"that passed: the audit receipt changed after preflight. {nothing} {ABORT_BEFORE_NEW_ID}"
        )
    failures = _ic_gate_failures(_ic_statuses(settings, receipt))
    if failures:
        raise StepFailure(
            f"Refused: per {target}, {_ic_refusal(settings, failures)} {nothing} {ABORT_BEFORE_NEW_ID}"
        )


# --- the clone tool --------------------------------------------------------------


def _clone_arguments(settings: Settings, inputs: Inputs, receipt: Path, *, apply: bool) -> list[str]:
    plan = settings.plan
    arguments = [
        "--transfer-mode", clone_tool.TRANSFER_MODE_RECALIBRATION,
        "--object-store-root", str(settings.object_store_root),
        "--object-store-prefix", settings.object_store_prefix,
        "--state-index", settings.state_index,
        "--mirror-state-index", settings.mirror_state_index,
        "--variant-registry", str(settings.canonical_manifest),
        "--baseline-registry", str(inputs.new_rows_registry["path"]),
        "--pairs", ",".join(f"{old}:{new}" for old, new in plan.pairs),
        "--cutover-time", plan.cutover_time,
        "--receipt", str(receipt),
    ]  # fmt: skip
    return [*arguments, "--apply"] if apply else arguments


def _call_clone(settings: Settings, inputs: Inputs, receipt: Path, *, apply: bool) -> dict[str, Any]:
    """Call the clone tool as its own ``main`` does; every way it stops is a ``StepFailure``."""

    parser = clone_tool.build_parser()
    try:
        args = parser.parse_args(_clone_arguments(settings, inputs, receipt, apply=apply))
        clone_tool.enforce_mode_flags(parser, args)
        args.dry_run = not args.apply
        return clone_tool.dispatch(args)
    except SystemExit as error:
        raise StepFailure(f"The clone tool refused its arguments (exit {error.code}).") from error
    except Exception as error:  # noqa: BLE001 - the tool raises several types; all of them stop the step
        notes = "".join(f" [{note}]" for note in getattr(error, "__notes__", ()))
        mode = "apply" if apply else "dry-run"
        raise StepFailure(f"The clone {mode} failed: {type(error).__name__}: {error}{notes}") from error


def _mismatches(receipt: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    return [
        f"{key}={receipt.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if receipt.get(key) != value
    ]


def _clone_pairs(receipt: dict[str, Any]) -> list[tuple[Any, Any]]:
    pairs = receipt.get("pairs")
    return [
        (pair.get("source_model_id"), pair.get("target_model_id")) for pair in pairs or [] if isinstance(pair, dict)
    ]


def _clone_matches_plan(settings: Settings, receipt: dict[str, Any], *, dry_run: bool) -> list[str]:
    """Why a complete clone receipt is not the one of this plan; empty when it is."""

    plan = settings.plan
    expected: dict[str, Any] = {
        "dry_run": dry_run,
        "invocation_outcome": "complete",
        "cutover_time": utc_text(parse_cycle_time(plan.cutover_time)),
        "declared_pair_count": len(plan.pairs),
    }
    if not dry_run:
        expected.update(
            cloned_pair_count=len(plan.pairs),
            state_index=settings.state_index,
            mirror_state_index=settings.mirror_state_index,
        )
    wrong = _mismatches(receipt, expected)
    if _clone_pairs(receipt) != list(plan.pairs):
        wrong.append(f"pairs={_clone_pairs(receipt)} (expected {list(plan.pairs)})")
    return wrong


def _clone_dry_run(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    target = settings.directory / CLONE_DRY_RUN_NAME
    if os.path.lexists(target):
        wrong = _clone_matches_plan(settings, _read_receipt(settings, target), dry_run=True)
        if wrong:
            raise StepFailure(
                f"Refused: {target} exists and is not the complete clone dry-run of this plan: {'; '.join(wrong)}. "
                f"A receipt is never overwritten; a changed plan needs a new --succession-id. {ABORT_BEFORE_NEW_ID}"
            )
        return {**_record(target), "reused": True}
    receipt = _call_clone(settings, inputs, target, apply=False)
    wrong = _clone_matches_plan(settings, receipt, dry_run=True)
    if wrong:
        raise StepFailure(f"The clone dry-run did not complete for this plan: {'; '.join(wrong)}.")
    return {**_record(target), "reused": False}


# --- the publish tool ------------------------------------------------------------


def _operations(settings: Settings) -> publish_tool.Operations:
    if settings.plan.kind == KIND_ADD_BASIN:
        return publish_tool.Operations(add=settings.plan.adds)
    return publish_tool.Operations(replace=settings.plan.pairs)


def _call_publish(settings: Settings, inputs: Inputs, *, apply: bool, succession_id: str | None) -> dict[str, Any]:
    return publish_tool.publish_merged_scheduler_registry(
        canonical_manifest=settings.canonical_manifest,
        mirror_manifest=settings.mirror_manifest,
        object_store_root=settings.object_store_root,
        provider_store_root=settings.provider_store_root,
        object_store_prefix=settings.object_store_prefix,
        operations=_operations(settings),
        operator_id=settings.operator_id,
        apply=apply,
        succession_id=succession_id,
        provision_succession_id=settings.plan.provision_succession_id,
        receipt_root=settings.receipt_root,
        new_rows_registry=inputs.new_rows_registry["path"],
        refresh_lock=settings.refresh_lock,
    )


_PUBLISH_ERRORS = (publish_tool.MergedRegistryPublishError, succession.SuccessionReceiptError)


def preflight(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """The kind check and the tools' dry-runs, so that what they refuse surfaces before the scheduler is stopped.

    A recalibration runs the clone dry-run; a cold start, which clones nothing, audits the packaged initial
    condition of its new models instead.  The kind check comes first in both: the clone gate looks at the
    source state before the surfaces, and would refuse a structural pair for a reason that names no way on.
    An added basin has no pair to compare and nothing to clone: its new models are audited like those of a
    cold start, and that is all before the publish dry-run.
    """

    facts: dict[str, Any] = {}
    if settings.plan.kind != KIND_ADD_BASIN:
        facts["kind_check"] = _kind_check(settings, inputs)
    if settings.plan.kind in KINDS_STARTING_FROM_PACKAGED_IC:
        facts["ic_audit"] = _ic_audit(settings, inputs)
    else:
        facts["clone_dry_run"] = _clone_dry_run(settings, inputs)
    target = settings.directory / publish_tool.DRY_RUN_RECEIPT_NAME
    reused = os.path.lexists(target)
    if reused:
        # The publish apply reads this receipt after the timer is stopped: it is held to the plan here, before.
        expected = {
            "dry_run": True,
            "outcome": "planned",
            "succession_id": settings.plan.succession_id,
            "provision_succession_id": settings.plan.provision_succession_id,
            "operations": _operations(settings).record(),
        }
        wrong = _mismatches(_read_receipt(settings, target), expected)
        if wrong:
            raise StepFailure(
                f"Refused: {target} exists and is not the publish dry-run of this plan: {'; '.join(wrong)}. "
                f"A receipt is never overwritten; a changed plan needs a new --succession-id. {ABORT_BEFORE_NEW_ID}"
            )
    else:
        try:
            _call_publish(settings, inputs, apply=False, succession_id=settings.plan.succession_id)
        except _PUBLISH_ERRORS as error:
            raise StepFailure(f"The publish dry-run refused: {error}") from error
    return {**facts, "publish_dry_run": {**_record(target), "reused": reused}}


def clone(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    target = settings.directory / CLONE_APPLY_NAME
    adopted = os.path.lexists(target)
    if not adopted:
        try:
            _call_clone(settings, inputs, target, apply=True)
        except StepFailure as error:
            if os.path.lexists(target):
                raise HardStop(
                    f"{error} It left {target}: clone rows of this run are in the state indexes. {_manual(settings)}"
                ) from error
            # No receipt: the clone tool stopped before it wrote any row, and the same command retries it.
            raise
    receipt = _read_receipt(settings, target)
    wrong = _clone_matches_plan(settings, receipt, dry_run=False)
    if wrong:
        raise HardStop(
            f"{target} is not a successful clone apply of this plan: {'; '.join(wrong)}; failed_pair "
            f"{receipt.get('failed_pair')!r}. Clone rows may be in one or both state indexes. {_manual(settings)}"
        )
    return {"clone_apply": _record(target), "adopted_existing_receipt": adopted}


def publish(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    target = settings.directory / publish_tool.APPLY_RECEIPT_NAME
    adopted = os.path.lexists(target)
    if not adopted:
        for failed in sorted(settings.directory.glob("publish-apply-failed-*.json")):
            if _read_receipt(settings, failed).get("outcome") == "inconsistent":
                raise HardStop(
                    f"{failed} records an inconsistent publish: the two manifests may differ. {_manual(settings)}"
                )
        if settings.plan.kind in KINDS_STARTING_FROM_PACKAGED_IC:
            _require_ic_audit(settings)
        try:
            _call_publish(settings, inputs, apply=True, succession_id=settings.plan.succession_id)
        except _PUBLISH_ERRORS as error:
            failed_receipt = getattr(error, "receipt", None) or {}
            if failed_receipt.get("outcome") == "inconsistent":
                raise HardStop(f"The publish apply ended inconsistent: {error} {_manual(settings)}") from error
            if planning.published_without_receipt(settings):
                raise HardStop(
                    f"The publish is complete but has no receipt: {error} The publish is in effect: do not undo it "
                    "and do not run this command again. Continue by hand with the provider refresh and then start "
                    f"the timer, from the runbook ({settings.plan.runbook})."
                ) from error
            raise StepFailure(f"The publish apply did not publish: {error}") from error
    receipt = _read_receipt(settings, target)
    expected = {
        "outcome": "published",
        "dry_run": False,
        "succession_id": settings.plan.succession_id,
        "operations": _operations(settings).record(),
    }
    wrong = _mismatches(receipt, expected)
    if wrong:
        raise HardStop(f"{target} is not the publish apply of this plan: {'; '.join(wrong)}. {_manual(settings)}")
    return {"publish_apply": _record(target), "adopted_existing_receipt": adopted, **settings.plan.continuity()}


# --- what a dry-run of the succession can predict ---------------------------------


_CLONE_PREVIEW_FIELDS = ("source_model_id", "target_model_id", "source_id", "cloned_from_state_id")
_PUBLISH_PREVIEW_FIELDS = (
    "row_count_before",
    "row_count_after",
    "replaced",
    "manifest_bytes_remaining",
    "manifest_json_nodes_remaining",
)
# An added basin replaces no row: its preview names the ids the publish would introduce instead.
_ADD_BASIN_PUBLISH_PREVIEW_FIELDS = tuple(
    "introduced_model_ids" if field == "replaced" else field for field in _PUBLISH_PREVIEW_FIELDS
)


def _preview_ic_audit(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    """The audit result of a dry-run: the gate outcome and ``ic_status`` per new model; no ``ic-audit.json``."""

    statuses = _ic_statuses(settings, _run_ic_audit(settings, inputs))
    failures = _ic_gate_failures(statuses)
    if failures:
        raise StepFailure(f"Refused: {_ic_refusal(settings, failures)}")
    return {"outcome": "qualified", "models": statuses}


def _preview_clone(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="nhms-model-succession-"))
    try:
        receipt = _call_clone(settings, inputs, scratch / CLONE_DRY_RUN_NAME, apply=False)
        wrong = _clone_matches_plan(settings, receipt, dry_run=True)
        if wrong:
            raise StepFailure(f"The clone dry-run did not complete for this plan: {'; '.join(wrong)}.")
        return {
            "outcome": "would_clone",
            "pairs": [{key: pair.get(key) for key in _CLONE_PREVIEW_FIELDS} for pair in receipt["pairs"]],
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def preview(settings: Settings, inputs: Inputs) -> tuple[dict[str, Any], list[str]]:
    """What ``preflight`` would find, leaving nothing behind: ``(report, what would be refused)``.

    Every check runs and is reported, also after an earlier one refused: only the ``preflight`` of an apply
    stops at the first refusal.
    """

    report: dict[str, Any] = {}
    refusals: list[str] = []
    add_basin = settings.plan.kind == KIND_ADD_BASIN
    checks = {} if add_basin else {"kind_check": _kind_check}
    if settings.plan.kind in KINDS_STARTING_FROM_PACKAGED_IC:
        checks["ic_audit"] = _preview_ic_audit
    else:
        checks["clone_dry_run"] = _preview_clone
    for name, check in checks.items():
        try:
            report[name] = check(settings, inputs)
        except StepFailure as error:
            report[name] = {"outcome": "refused", "reason": str(error)}
            refusals.append(str(error))
    try:
        planned = _call_publish(settings, inputs, apply=False, succession_id=None)
        report["publish_dry_run"] = {
            "outcome": "would_publish",
            **{
                key: planned.get(key)
                for key in (_ADD_BASIN_PUBLISH_PREVIEW_FIELDS if add_basin else _PUBLISH_PREVIEW_FIELDS)
            },
        }
    except _PUBLISH_ERRORS as error:
        report["publish_dry_run"] = {"outcome": "refused", "reason": str(error)}
        refusals.append(str(error))
    return report, refusals
