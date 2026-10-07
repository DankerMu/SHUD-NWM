"""The checks before any step, and ``plan.json``: what a succession id was first applied with.

Part of ``scripts/node22_model_succession.py`` (the entry point).  Nothing here
writes except ``write_plan``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from scripts.merged_registry_publish.model import APPLY_RECEIPT_NAME as PUBLISH_APPLY_NAME
from scripts.merged_registry_publish.model import (
    PROVISION_APPLY_RECEIPT_NAME,
    MergedRegistryPublishError,
    refuse_database_environment,
)
from scripts.model_succession.model import (
    _NOTHING_WRITTEN,
    ABORT_BEFORE_NEW_ID,
    FINAL_STEP,
    KIND_ADD_BASIN,
    KIND_REMOVE_BASIN,
    PLAN_NAME,
    PLAN_SCHEMA_VERSION,
    PROVIDER_STORE_ROOT_ENV,
    STEPS_BY_KIND,
    TIMER_RECORD_CUT_OFF,
    TIMER_RECORD_NAME,
    Inputs,
    ModelSuccessionRefusal,
    Plan,
    Settings,
    manifests_differ_text,
    read_json,
    receipt_header,
)

_CUTOVER_TIME = re.compile(r"\d{10}")


def refuse_database() -> None:
    """The publish tool's own refusal of a database environment, as this tool's refusal."""

    try:
        refuse_database_environment()
    except MergedRegistryPublishError as error:
        raise ModelSuccessionRefusal(str(error)) from error


def build_plan(
    *,
    succession_id: str,
    provision_succession_id: str | None,
    kind: str,
    pairs: Sequence[tuple[str, str]],
    cutover_time: str | None,
    adds: Sequence[str] = (),
    removes: Sequence[str] = (),
    new_rows_registry: str | None = None,
) -> Plan:
    """The plan of the command line; ``new_rows_registry`` is the option as given, only held to the kind here."""

    try:
        succession.validate_succession_id(succession_id)
        if provision_succession_id is not None:
            succession.validate_succession_id(provision_succession_id)
    except succession.SuccessionReceiptError as error:
        raise ModelSuccessionRefusal(str(error)) from error
    if kind not in STEPS_BY_KIND:
        raise ModelSuccessionRefusal(
            f"Refused: --kind {kind!r} is not supported; the kinds are {', '.join(map(repr, STEPS_BY_KIND))}."
        )
    named = [model_id for pair in pairs for model_id in pair] + list(adds) + list(removes)
    repeated = sorted({model_id for model_id in named if named.count(model_id) > 1})
    if kind == KIND_REMOVE_BASIN:
        given = {
            "--pair": bool(pairs),
            "--add": bool(adds),
            "--cutover-time": cutover_time is not None,
            "--provision-succession-id": provision_succession_id is not None,
            "--new-rows-registry": new_rows_registry is not None,
        }
        _check_remove_basin_arguments([option for option, is_given in given.items() if is_given], removes, repeated)
    elif removes:
        raise ModelSuccessionRefusal(
            f"Refused: --remove is not valid with --kind {kind}: only --kind {KIND_REMOVE_BASIN} removes the "
            "models of a basin from the manifests. Run the removal under its own --succession-id."
        )
    elif kind == KIND_ADD_BASIN:
        _check_add_basin_arguments(pairs, cutover_time, adds, repeated)
    else:
        if adds:
            raise ModelSuccessionRefusal(
                f"Refused: --add is not valid with --kind {kind}, which replaces models: name each replaced model "
                f"and its successor with --pair, or add the models of a new basin with --kind {KIND_ADD_BASIN}."
            )
        if not pairs:
            raise ModelSuccessionRefusal("Refused: at least one --pair <old_model_id>:<new_model_id> is required.")
        if repeated:
            raise ModelSuccessionRefusal(
                f"Refused: a model_id may be named in one --pair only, and once: {repeated}."
            )
        if cutover_time is None:
            raise ModelSuccessionRefusal(f"Refused: --cutover-time <YYYYMMDDHH> is required with --kind {kind}.")
        if not _CUTOVER_TIME.fullmatch(cutover_time):
            raise ModelSuccessionRefusal(f"Refused: --cutover-time {cutover_time!r} is not YYYYMMDDHH.")
    return Plan(
        succession_id=succession_id,
        provision_succession_id=provision_succession_id or succession_id,
        kind=kind,
        pairs=tuple((old, new) for old, new in pairs),
        cutover_time=cutover_time,
        adds=tuple(adds),
        removes=tuple(removes),
    )


def _check_add_basin_arguments(
    pairs: Sequence[tuple[str, str]], cutover_time: str | None, adds: Sequence[str], repeated: Sequence[str]
) -> None:
    """The arguments of an added basin: its new models, and neither a predecessor nor a cutover."""

    if pairs:
        raise ModelSuccessionRefusal(
            f"Refused: --pair is not valid with --kind {KIND_ADD_BASIN}: a new basin has no old model to replace. "
            "Name each of its models with --add <new_model_id>, or run the replacement under its own kind and "
            "--succession-id."
        )
    if cutover_time is not None:
        raise ModelSuccessionRefusal(
            f"Refused: --cutover-time is not valid with --kind {KIND_ADD_BASIN}: a new basin has no history and "
            "so no cutover. Leave the option out."
        )
    if not adds:
        raise ModelSuccessionRefusal(
            f"Refused: at least one --add <new_model_id> is required with --kind {KIND_ADD_BASIN}: one per source "
            "of each new basin."
        )
    if repeated:
        raise ModelSuccessionRefusal(f"Refused: a model_id may be named with --add once only: {list(repeated)}.")


def _check_remove_basin_arguments(misfits: Sequence[str], removes: Sequence[str], repeated: Sequence[str]) -> None:
    """The arguments of a removed basin: its models, and nothing of a replacement, an addition or a provision."""

    if misfits:
        raise ModelSuccessionRefusal(
            f"Refused: {', '.join(misfits)} is not valid with --kind {KIND_REMOVE_BASIN}: a removal has no "
            "successor, no added model, no cutover and no provision. Name each model of the removed basin with "
            "--remove <model_id> and leave the option out."
        )
    if not removes:
        raise ModelSuccessionRefusal(
            f"Refused: at least one --remove <model_id> is required with --kind {KIND_REMOVE_BASIN}: one per "
            "source of each removed basin."
        )
    if repeated:
        raise ModelSuccessionRefusal(f"Refused: a model_id may be named with --remove once only: {list(repeated)}.")


def _abort_receipts(directory: Path) -> list[Path]:
    return sorted(directory.glob("abort-*.json"))


def refuse_other_timer_holder(settings: Settings, *, wrote: str = _NOTHING_WRITTEN) -> None:
    """Refuse while another succession holds the scheduler timer: it stopped it and neither finished nor was aborted.

    A succession begun then would find the timer inactive, record that, and leave the scheduler stopped at its
    own finish.  A sibling's timer record that cannot be read is a refusal too, not a succession to skip.
    Checked before any step and again by ``begin``, which says in ``wrote`` what it left alone.
    """

    root = settings.receipt_root
    try:
        siblings = sorted(entry for entry in root.iterdir() if entry.name != settings.plan.succession_id)
    except OSError as error:
        raise ModelSuccessionRefusal(
            f"Refused: cannot list the receipt root {root} ({error}), so whether another succession holds the "
            f"scheduler timer is unknown. {wrote}"
        ) from error
    for directory in siblings:
        record = directory / TIMER_RECORD_NAME
        if not os.path.lexists(record):
            continue
        try:
            was_active = read_json(record).get("timer_was_active")
            closed = os.path.lexists(directory / f"step-{FINAL_STEP}.json") or bool(_abort_receipts(directory))
        except (OSError, ValueError) as error:
            raise ModelSuccessionRefusal(
                f"Refused: {record} of succession {directory.name!r} cannot be read ({error}), so whether that "
                f"succession holds the scheduler timer is unknown. {TIMER_RECORD_CUT_OFF} {wrote}"
            ) from error
        if not isinstance(was_active, bool):
            raise ModelSuccessionRefusal(
                f"Refused: {record} of succession {directory.name!r} does not record timer_was_active, so whether "
                f"that succession holds the scheduler timer is unknown. {TIMER_RECORD_CUT_OFF} {wrote}"
            )
        if was_active and not closed:
            raise ModelSuccessionRefusal(
                f"Refused: succession {directory.name!r} stopped the scheduler timer ({record} records "
                "timer_was_active true) and has neither finished nor been aborted. A succession begun now would "
                "record the timer as inactive and leave the scheduler stopped at its finish. Resume "
                f"{directory.name!r} with its own command, or give it up first: its command line (--succession-id "
                f"{directory.name}) with --abort --confirm-timer-start instead of --apply. {wrote}"
            )


def manifests_differ(settings: Settings) -> bool:
    """The canonical manifest and the worker mirror are not the same bytes, or one of them cannot be read."""

    try:
        return settings.canonical_manifest.read_bytes() != settings.mirror_manifest.read_bytes()
    except OSError:
        return True


def published_without_receipt(settings: Settings) -> bool:
    """This succession reached its publish step and left no ``publish-apply.json``, yet both manifests are equal
    and hold every new id and no old id.

    Without the receipt of the step before ``publish`` in the list of its kind (``step-clone.json``, or
    ``step-begin.json`` of a cold start, an added basin or a removed basin) the publish step of this succession
    never ran: the manifests then hold what another succession published, with its own receipt.  A removed
    basin has no new id: its removed ids are the old ones, and none of them is left.
    """

    if not os.path.lexists(settings.step_receipt(settings.plan.step_before_publish)):
        return False
    if os.path.lexists(settings.directory / PUBLISH_APPLY_NAME):
        return False
    try:
        canonical = settings.canonical_manifest.read_bytes()
        if canonical != settings.mirror_manifest.read_bytes():
            return False
        ids = {str(row.get("model_id")) for row in read_json(settings.canonical_manifest).get("models") or []}
    except (OSError, ValueError, AttributeError):
        return False
    return set(settings.plan.new_ids) <= ids and not set(settings.plan.old_ids) & ids


def refuse_closed(settings: Settings) -> None:
    """A succession that has an abort receipt is closed for every later run."""

    try:
        aborts = _abort_receipts(settings.directory)
    except OSError:
        aborts = []
    if aborts:
        raise ModelSuccessionRefusal(
            f"Refused: succession {settings.plan.succession_id!r} was aborted ({aborts[0]}) and is closed. "
            f"A new attempt needs a new --succession-id. {_NOTHING_WRITTEN}"
        )


def manifest_model_ids(path: Path, *, what: str) -> list[str]:
    try:
        models = read_json(path).get("models")
    except (OSError, ValueError) as error:
        raise ModelSuccessionRefusal(f"Refused: cannot read {what} {path} ({error}). {_NOTHING_WRITTEN}") from error
    if not isinstance(models, list) or any(not isinstance(row, Mapping) or not row.get("model_id") for row in models):
        raise ModelSuccessionRefusal(
            f"Refused: {what} {path} must hold a models list of rows with a model_id. {_NOTHING_WRITTEN}"
        )
    return [str(row["model_id"]) for row in models]


def _refuse_manifests_that_differ_at_publish(settings: Settings) -> None:
    # Past the step before its publish (its clone; the begin of a cold start, an added basin or a removed basin)
    # this succession is at, or after, its publish: manifests that differ there are not retried.
    if os.path.lexists(settings.step_receipt(settings.plan.step_before_publish)) and manifests_differ(settings):
        raise ModelSuccessionRefusal(
            f"Refused: {manifests_differ_text(settings)} Once both are the same bytes again, run the same command "
            f"or give the succession up with --abort --confirm-timer-start. {_NOTHING_WRITTEN}"
        )


def _refuse_published_without_receipt(settings: Settings) -> None:
    if not published_without_receipt(settings):
        return
    if settings.plan.kind == KIND_REMOVE_BASIN:
        found = "neither manifest holds any model_id this plan removes any more"
    else:
        found = "both manifests already hold every new model_id of this plan and no old one"
    raise ModelSuccessionRefusal(
        f"Refused: {found}, and "
        f"{settings.directory / PUBLISH_APPLY_NAME} does not exist: a publish of this plan is in effect, "
        "without the receipt of this succession. Do not undo it and do not run this command again; continue "
        f"by hand with the provider refresh and then start the timer, from the runbook ({settings.plan.runbook}). "
        f"{_NOTHING_WRITTEN}"
    )


def _refuse_absent_old_ids(settings: Settings, canonical_ids: Sequence[str]) -> None:
    absent = [model_id for model_id in settings.plan.old_ids if model_id not in canonical_ids]
    if absent:
        raise ModelSuccessionRefusal(
            f"Refused: old model_id is not in the canonical manifest {settings.canonical_manifest}: {absent}. "
            f"{_NOTHING_WRITTEN}"
        )


def _check_remove_basin_inputs(settings: Settings) -> Inputs:
    """The checks of ``check_inputs`` that read the two manifests, in its order; nothing else is read."""

    _refuse_manifests_that_differ_at_publish(settings)
    canonical_ids = manifest_model_ids(settings.canonical_manifest, what="the canonical manifest")
    # Before ``begin`` this is never true: a removed id that is not in the manifest is refused as absent below.
    _refuse_published_without_receipt(settings)
    if not os.path.lexists(settings.directory / PUBLISH_APPLY_NAME):
        _refuse_absent_old_ids(settings, canonical_ids)
    return Inputs(provision_receipt=None, new_rows_registry=None, new_rows={})


def check_inputs(settings: Settings) -> Inputs:
    """Refuse a plan that the provision receipt, its registry or the canonical manifest does not support.

    A removed basin has neither a provision receipt nor a new-rows registry: only the checks of the two
    manifests apply to it, in the same order.
    """

    plan = settings.plan
    if plan.kind == KIND_REMOVE_BASIN:
        return _check_remove_basin_inputs(settings)
    receipt_path = settings.receipt_root / plan.provision_succession_id / PROVISION_APPLY_RECEIPT_NAME
    try:
        receipt = read_json(receipt_path)
    except (OSError, ValueError) as error:
        raise ModelSuccessionRefusal(
            f"Refused: the provision apply receipt of succession {plan.provision_succession_id!r} is missing or "
            f"unreadable at {receipt_path} ({error}). Run the provision step with --apply on node-27 first, or name "
            f"its succession with --provision-succession-id. {_NOTHING_WRITTEN}"
        ) from error
    if receipt.get("outcome") != "applied" or receipt.get("dry_run") is not False:
        raise ModelSuccessionRefusal(
            f"Refused: {receipt_path} is not a provision apply receipt with outcome 'applied' (found outcome "
            f"{receipt.get('outcome')!r}, dry_run {receipt.get('dry_run')!r}). {_NOTHING_WRITTEN}"
        )
    models = receipt.get("models")
    provisioned = [str(model.get("model_id")) for model in models if isinstance(model, Mapping)] if models else []
    unprovisioned = [model_id for model_id in plan.new_ids if model_id not in provisioned]
    if unprovisioned:
        raise ModelSuccessionRefusal(
            f"Refused: new model_id is not in models[] of {receipt_path}: {unprovisioned}. {_NOTHING_WRITTEN}"
        )

    _refuse_manifests_that_differ_at_publish(settings)
    canonical_ids = manifest_model_ids(settings.canonical_manifest, what="the canonical manifest")
    unaccounted = [
        model_id for model_id in provisioned if model_id not in plan.new_ids and model_id not in canonical_ids
    ]
    if unaccounted:
        named = "named with --add" if plan.kind == KIND_ADD_BASIN else "a new id of a --pair"
        raise ModelSuccessionRefusal(
            f"Refused: {receipt_path} lists model_id that is neither {named} nor in the canonical "
            f"manifest: {unaccounted}. A provisioned row must not be left out by accident. {_NOTHING_WRITTEN}"
        )
    # Once this succession's publish has happened the canonical manifest holds the new ids instead of the
    # old ones; the finish step checks exactly that, so a resumed run is not refused for it here.
    _refuse_published_without_receipt(settings)
    if not os.path.lexists(settings.directory / PUBLISH_APPLY_NAME):
        # The new ids first: pairs that another succession already published are named as that.
        present = [model_id for model_id in plan.new_ids if model_id in canonical_ids]
        if present:
            raise ModelSuccessionRefusal(
                f"Refused: new model_id is already in the canonical manifest {settings.canonical_manifest}: "
                f"{present}. {_NOTHING_WRITTEN}"
            )
        _refuse_absent_old_ids(settings, canonical_ids)

    registry_path, registry_sha256, rows = _new_rows(settings, receipt, receipt_path)
    missing_rows = [model_id for model_id in plan.new_ids if model_id not in rows]
    if missing_rows:
        raise ModelSuccessionRefusal(
            f"Refused: new model_id is not a row of the new-rows registry {registry_path}: {missing_rows}. "
            f"{_NOTHING_WRITTEN}"
        )
    return Inputs(
        provision_receipt={"path": str(receipt_path), "sha256": succession.file_sha256(receipt_path)},
        new_rows_registry={"path": str(registry_path), "sha256": registry_sha256},
        new_rows={model_id: rows[model_id] for model_id in plan.new_ids},
    )


def _new_rows(
    settings: Settings, receipt: Mapping[str, Any], receipt_path: Path
) -> tuple[Path, str, dict[str, dict[str, Any]]]:
    output_registry = receipt.get("output_registry")
    output_registry = output_registry if isinstance(output_registry, Mapping) else {}
    registry_path = settings.new_rows_registry
    if registry_path is None:
        key = output_registry.get("object_store_key")
        if not key:
            raise ModelSuccessionRefusal(
                f"Refused: {receipt_path} records no object_store_key for its output_registry, so the registry "
                f"written by the provision apply must be named with --new-rows-registry. {_NOTHING_WRITTEN}"
            )
        registry_path = settings.provider_store_root / str(key)
    try:
        registry_sha256 = succession.file_sha256(registry_path)
        models = read_json(registry_path).get("models")
    except (OSError, ValueError) as error:
        raise ModelSuccessionRefusal(
            f"Refused: cannot read the new-rows registry {registry_path} ({error}); without --new-rows-registry it "
            f"is resolved under {PROVIDER_STORE_ROOT_ENV}. {_NOTHING_WRITTEN}"
        ) from error
    recorded = str(output_registry.get("sha256") or "").removeprefix("sha256:")
    if registry_sha256 != recorded:
        raise ModelSuccessionRefusal(
            f"Refused: the new-rows registry {registry_path} has sha256 {registry_sha256}, but {receipt_path} "
            f"recorded {recorded or None} for the registry its apply wrote. {_NOTHING_WRITTEN}"
        )
    rows = {
        str(row["model_id"]): dict(row)
        for row in (models if isinstance(models, list) else [])
        if isinstance(row, Mapping) and row.get("model_id")
    }
    return registry_path, registry_sha256, rows


def _plan_record(settings: Settings, inputs: Inputs | None) -> dict[str, Any]:
    # A removed basin has no provision receipt and no new-rows registry: its record is the command line alone.
    if inputs is None or settings.plan.kind == KIND_REMOVE_BASIN:
        return settings.plan.record()
    return {
        **settings.plan.record(),
        "provision_apply_receipt_sha256": inputs.provision_receipt["sha256"],
        "new_rows_registry_sha256": inputs.new_rows_registry["sha256"],
    }


def compare_with_plan(settings: Settings, inputs: Inputs | None) -> bool:
    """Refuse a command line that differs from ``plan.json``; False when no plan was written yet.

    With ``inputs`` the provision apply receipt and the new-rows registry must also be the bytes the plan
    recorded; the abort, which reads neither, compares the command line alone.
    """

    path = settings.directory / PLAN_NAME
    if not os.path.lexists(path):
        return False
    try:
        recorded = read_json(path)
    except (OSError, ValueError) as error:
        raise ModelSuccessionRefusal(f"Refused: cannot read {path} ({error}). {_NOTHING_WRITTEN}") from error
    current = _plan_record(settings, inputs)
    different = {
        key: {"plan.json": recorded.get(key), "this run": value}
        for key, value in current.items()
        if recorded.get(key) != value
    }
    if different:
        raise ModelSuccessionRefusal(
            f"Refused: this command line differs from {path}, which succession {settings.plan.succession_id!r} "
            f"was first applied with: {different}. Run the same command, or use a new --succession-id for a "
            f"changed plan. {ABORT_BEFORE_NEW_ID} {_NOTHING_WRITTEN}"
        )
    return True


def write_plan(settings: Settings, inputs: Inputs) -> None:
    """Exclusive-create ``plan.json``; the first apply of a succession id.

    The plan of a removed basin has no ``provision_apply_receipt`` and no ``new_rows_registry``: the keys are
    left out, not written as null.
    """

    path = settings.directory / PLAN_NAME
    provisioned = {"provision_apply_receipt": inputs.provision_receipt, "new_rows_registry": inputs.new_rows_registry}
    try:
        succession.prepare_receipt_target(path, receipt_root=settings.receipt_root)
        succession.write_receipt(
            path,
            {
                **receipt_header(settings, PLAN_SCHEMA_VERSION),
                **_plan_record(settings, inputs),
                **settings.plan.continuity(),
                **({} if settings.plan.kind == KIND_REMOVE_BASIN else provisioned),
            },
        )
    except (OSError, succession.SuccessionReceiptError) as error:
        raise ModelSuccessionRefusal(f"Refused: cannot write {path}: {error}") from error
