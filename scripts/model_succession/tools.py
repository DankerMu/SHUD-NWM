"""The steps that call the clone tool and the publish tool: ``preflight``, ``clone`` and ``publish``.

Part of ``scripts/node22_model_succession.py`` (the entry point).  Both tools
are called in-process through their own entry functions, with their own
receipts; this module adds no option to either and decides only what their
outcome means for the succession.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import scripts.node22_clone_direct_grid_cutover_states as clone_tool
import scripts.node22_publish_merged_scheduler_registry as publish_tool
from packages.common import succession_receipt as succession
from scripts.model_succession import plan as planning
from scripts.model_succession.model import (
    ABORT_BEFORE_NEW_ID,
    CLONE_APPLY_NAME,
    CLONE_DRY_RUN_NAME,
    RUNBOOK,
    HardStop,
    Inputs,
    Settings,
    StepFailure,
    read_json,
    utc_text,
)
from workers.data_adapters.base import parse_cycle_time

_MANUAL = f"This tool does not retry it; continue by hand from the runbook ({RUNBOOK})."


def _record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": succession.file_sha256(path)}


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except (OSError, ValueError) as error:
        raise HardStop(f"The receipt {path} exists but cannot be read ({error}). {_MANUAL}") from error


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
        wrong = _clone_matches_plan(settings, _read_receipt(target), dry_run=True)
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
    """Both tools' dry-runs, so that what they refuse surfaces before the scheduler is stopped."""

    clone_record = _clone_dry_run(settings, inputs)
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
        wrong = _mismatches(_read_receipt(target), expected)
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
    return {"clone_dry_run": clone_record, "publish_dry_run": {**_record(target), "reused": reused}}


def clone(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    target = settings.directory / CLONE_APPLY_NAME
    adopted = os.path.lexists(target)
    if not adopted:
        try:
            _call_clone(settings, inputs, target, apply=True)
        except StepFailure as error:
            if os.path.lexists(target):
                raise HardStop(
                    f"{error} It left {target}: clone rows of this run are in the state indexes. {_MANUAL}"
                ) from error
            # No receipt: the clone tool stopped before it wrote any row, and the same command retries it.
            raise
    receipt = _read_receipt(target)
    wrong = _clone_matches_plan(settings, receipt, dry_run=False)
    if wrong:
        raise HardStop(
            f"{target} is not a successful clone apply of this plan: {'; '.join(wrong)}; failed_pair "
            f"{receipt.get('failed_pair')!r}. Clone rows may be in one or both state indexes. {_MANUAL}"
        )
    return {"clone_apply": _record(target), "adopted_existing_receipt": adopted}


def publish(settings: Settings, inputs: Inputs) -> dict[str, Any]:
    target = settings.directory / publish_tool.APPLY_RECEIPT_NAME
    adopted = os.path.lexists(target)
    if not adopted:
        for failed in sorted(settings.directory.glob("publish-apply-failed-*.json")):
            if _read_receipt(failed).get("outcome") == "inconsistent":
                raise HardStop(
                    f"{failed} records an inconsistent publish: the two manifests may differ. {_MANUAL}"
                )
        try:
            _call_publish(settings, inputs, apply=True, succession_id=settings.plan.succession_id)
        except _PUBLISH_ERRORS as error:
            failed_receipt = getattr(error, "receipt", None) or {}
            if failed_receipt.get("outcome") == "inconsistent":
                raise HardStop(f"The publish apply ended inconsistent: {error} {_MANUAL}") from error
            if planning.published_without_receipt(settings):
                raise HardStop(
                    f"The publish is complete but has no receipt: {error} The publish is in effect: do not undo it "
                    "and do not run this command again. Continue by hand with the provider refresh and then start "
                    f"the timer, from the runbook ({RUNBOOK})."
                ) from error
            raise StepFailure(f"The publish apply did not publish: {error}") from error
    receipt = _read_receipt(target)
    expected = {
        "outcome": "published",
        "dry_run": False,
        "succession_id": settings.plan.succession_id,
        "operations": _operations(settings).record(),
    }
    wrong = _mismatches(receipt, expected)
    if wrong:
        raise HardStop(f"{target} is not the publish apply of this plan: {'; '.join(wrong)}. {_MANUAL}")
    return {"publish_apply": _record(target), "adopted_existing_receipt": adopted}


# --- what a dry-run of the succession can predict ---------------------------------


_CLONE_PREVIEW_FIELDS = ("source_model_id", "target_model_id", "source_id", "cloned_from_state_id")
_PUBLISH_PREVIEW_FIELDS = (
    "row_count_before",
    "row_count_after",
    "replaced",
    "manifest_bytes_remaining",
    "manifest_json_nodes_remaining",
)


def preview(settings: Settings, inputs: Inputs) -> tuple[dict[str, Any], list[str]]:
    """Both tools' dry-runs leaving nothing behind: ``(report, what would be refused)``."""

    report: dict[str, Any] = {}
    refusals: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix="nhms-model-succession-"))
    try:
        receipt = _call_clone(settings, inputs, scratch / CLONE_DRY_RUN_NAME, apply=False)
        wrong = _clone_matches_plan(settings, receipt, dry_run=True)
        if wrong:
            raise StepFailure(f"The clone dry-run did not complete for this plan: {'; '.join(wrong)}.")
        report["clone_dry_run"] = {
            "outcome": "would_clone",
            "pairs": [
                {key: pair.get(key) for key in _CLONE_PREVIEW_FIELDS}
                for pair in receipt["pairs"]
            ],
        }
    except StepFailure as error:
        report["clone_dry_run"] = {"outcome": "refused", "reason": str(error)}
        refusals.append(str(error))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    try:
        planned = _call_publish(settings, inputs, apply=False, succession_id=None)
        report["publish_dry_run"] = {
            "outcome": "would_publish",
            **{key: planned.get(key) for key in _PUBLISH_PREVIEW_FIELDS},
        }
    except _PUBLISH_ERRORS as error:
        report["publish_dry_run"] = {"outcome": "refused", "reason": str(error)}
        refusals.append(str(error))
    return report, refusals
