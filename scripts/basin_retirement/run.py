"""The two runs of the basin retirement tool, the dry-run and the apply, and what is checked before either.

Part of ``scripts/node27_retire_basin.py`` (the entry point).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from packages.common import succession_receipt as succession
from scripts.basin_retirement import autopipe, database, envfile, steps
from scripts.basin_retirement.model import (
    FAILURE_RECEIPT_PREFIX,
    FAILURE_SCHEMA_VERSION,
    FORBIDDEN_AUTH_ENVIRONMENT,
    NODE22_FINISH_NAME,
    NODE22_KIND,
    NODE22_PLAN_NAME,
    NOT_DONE_BY_THIS_TOOL,
    NOTHING_WRITTEN,
    RUNBOOK,
    STEP_RECEIPT_SCHEMA_VERSION,
    STEP_REQUIRES,
    STEPS,
    Basin,
    RetirementRefusal,
    Settings,
    StepFailure,
    completed_steps,
    existing_receipts,
    read_json,
    receipt_header,
    write_stamped_receipt,
)

STEP_FUNCTIONS: dict[str, Callable[[Settings, Basin], dict[str, Any]]] = {
    "exclude": steps.exclude,
    "supersede": steps.supersede,
    "deactivate": steps.deactivate,
    "verify": steps.verify,
}


def refuse_auth_environment() -> None:
    """The lifecycle call must run with the ingest environment only."""

    present = [name for name in FORBIDDEN_AUTH_ENVIRONMENT if name in os.environ]
    if present:
        unset = " ".join(f"-u {name}" for name in FORBIDDEN_AUTH_ENVIRONMENT)
        raise RetirementRefusal(
            f"Refused: {', '.join(present)} is set. The model lifecycle operation must run with the ingest "
            f"environment only; run the same command as `env {unset} <command>`. {NOTHING_WRITTEN}"
        )


def _node22_removes(settings: Settings, refusals: list[str]) -> list[str] | None:
    """The model ids the node-22 half removed, or None when that half is not a finished ``remove_basin``."""

    finish = settings.succession_directory / NODE22_FINISH_NAME
    plan = settings.succession_directory / NODE22_PLAN_NAME
    by_hand = (
        "A removal that was finished by hand after a node-22 hard stop has no such receipt: the node-27 steps "
        f"are then manual too ({RUNBOOK})."
    )
    try:
        outcome = read_json(finish).get("outcome")
    except (OSError, ValueError) as error:
        refusals.append(
            f"the node-22 finish receipt {finish} cannot be read ({error}): succession {settings.succession_id!r} "
            f"has not finished on node-22. {by_hand}"
        )
        return None
    if outcome != "completed":
        refusals.append(f"the node-22 finish receipt {finish} has outcome {outcome!r}, not 'completed'. {by_hand}")
        return None
    try:
        recorded = read_json(plan)
    except (OSError, ValueError) as error:
        refusals.append(f"the node-22 plan {plan} cannot be read ({error}).")
        return None
    if recorded.get("kind") != NODE22_KIND:
        refusals.append(
            f"the node-22 plan {plan} has kind {recorded.get('kind')!r}, not {NODE22_KIND!r}: succession "
            f"{settings.succession_id!r} did not remove a basin."
        )
        return None
    removes = recorded.get("removes")
    if not isinstance(removes, list) or not removes or not all(isinstance(item, str) for item in removes):
        refusals.append(f"the node-22 plan {plan} names no removed model ids.")
        return None
    return list(removes)


def _manifest_rows_of_basin(settings: Settings, basin: Basin | None, refusals: list[str]) -> dict[str, Any]:
    """Refuse when the canonical manifest still holds a row of the basin, under this version or any other."""

    manifest = settings.manifest
    try:
        models = read_json(manifest).get("models")
    except (OSError, ValueError) as error:
        refusals.append(f"the canonical manifest {manifest} cannot be read ({error}).")
        return {"manifest": str(manifest), "status": "unreadable"}
    if not isinstance(models, list):
        refusals.append(f"the canonical manifest {manifest} has no models list.")
        return {"manifest": str(manifest), "status": "unreadable"}
    same_version: list[str] = []
    other_versions: list[str] = []
    for index, row in enumerate(models):
        basin_id = row.get("basin_id") if isinstance(row, Mapping) else None
        if not isinstance(basin_id, str) or not basin_id:
            # A row whose basin cannot be told is not proof that the basin is out.
            refusals.append(f"the canonical manifest {manifest} has a row without a basin_id (models[{index}]).")
            return {"manifest": str(manifest), "status": "unreadable"}
        name = str(row.get("model_id") or f"models[{index}]")
        if row.get("basin_version_id") == settings.basin_version_id:
            same_version.append(name)
        elif basin is not None and (basin_id == basin.basin_id or basin.key in envfile.basin_keys(basin_id)):
            other_versions.append(name)
    if same_version:
        refusals.append(
            f"the canonical manifest {manifest} still holds rows of basin version {settings.basin_version_id}: "
            f"{same_version}. The scheduler still plans it."
        )
    if other_versions:
        refusals.append(
            f"the canonical manifest {manifest} holds rows of another version of basin "
            f"{basin.basin_id if basin else None!r}: {other_versions}. The exclusion list works per basin: "
            "excluding it would stop that version's ingest. Changing a basin's mesh is not a retirement."
        )
    return {
        "manifest": str(manifest),
        "row_count": len(models),
        "rows_of_this_basin_version": same_version,
        "rows_of_other_versions_of_the_basin": other_versions,
    }


def preconditions(settings: Settings) -> tuple[Basin | None, dict[str, Any], list[str]]:
    """Everything that is checked before any step, in both modes: the basin, what was found, the refusals.

    Read-only.  Every check runs whatever the ones before it found, so a dry-run lists them all.
    """

    refusals: list[str] = []
    facts: dict[str, Any] = {}
    removes = _node22_removes(settings, refusals)
    facts["node22_removes"] = removes
    basin: Basin | None = None
    try:
        basin_id = database.basin_id_of(settings.database_url, settings.basin_version_id)
        if basin_id is None:
            refusals.append(f"--basin-version-id {settings.basin_version_id!r} is not a row of core.basin_version.")
        else:
            try:
                basin = Basin(basin_id=basin_id, key=envfile.basin_key(basin_id))
            except ValueError as error:
                refusals.append(f"{error}.")
            rows = database.model_ids(settings.database_url, settings.basin_version_id)
            removed_rows = sorted(set(removes or []) & set(rows))
            facts.update(basin_id=basin_id, model_row_count=len(rows), removed_model_rows=removed_rows)
            if removes is not None and not removed_rows:
                refusals.append(
                    f"no model id the node-22 plan removed ({removes}) is a core.model_instance row of basin "
                    f"version {settings.basin_version_id}: this succession did not remove this basin."
                )
    except StepFailure as error:
        refusals.append(f"the database could not be read: {error}")
    if basin is not None:
        facts["basin_key"] = basin.key
    facts["manifest"] = _manifest_rows_of_basin(settings, basin, refusals)
    return basin, facts, refusals


def run_step(settings: Settings, basin: Basin, step: str) -> str:
    """Run one step unless its receipt exists; ``skipped`` or ``completed``.

    A step is refused, before it does anything, when a receipt it requires is missing.
    """

    target = settings.step_receipt(step)
    if os.path.lexists(target):
        return "skipped"
    missing = [str(settings.step_receipt(name)) for name in STEP_REQUIRES[step]]
    missing = [path for path in missing if not os.path.lexists(path)]
    if missing:
        raise StepFailure(
            f"Refused: the {step} step requires the receipt of {' and of '.join(STEP_REQUIRES[step])}; missing: "
            f"{missing}. The {step} step changed nothing."
        )
    try:
        succession.prepare_receipt_target(target, receipt_root=settings.receipt_root)
    except succession.SuccessionReceiptError as error:
        raise StepFailure(f"{error} The {step} step changed nothing.") from error
    facts = STEP_FUNCTIONS[step](settings, basin)
    header = receipt_header(settings, STEP_RECEIPT_SCHEMA_VERSION)
    # The step's own facts never replace the common fields.
    receipt = {**facts, **header, "basin_id": basin.basin_id, "step": step, "outcome": "completed"}
    succession.write_receipt(target, receipt)
    return "completed"


def _record_failure(settings: Settings, basin: Basin, step: str, error: Exception) -> dict[str, Any]:
    known = isinstance(error, StepFailure | RetirementRefusal)
    reason = str(error) if known else f"{type(error).__name__}: {error}"
    ways_on = [
        "Remove the cause and run the same command again: completed steps are skipped.",
        f"Nothing in this tool reverts a step; reviving the basin is manual ({RUNBOOK}).",
    ]
    receipt: dict[str, Any] = {
        **(error.details if isinstance(error, StepFailure) else {}),
        **receipt_header(settings, FAILURE_SCHEMA_VERSION),
        "basin_id": basin.basin_id,
        "step": step,
        "outcome": "failed",
        "reason": reason,
        "completed_steps": completed_steps(settings),
        "receipts": existing_receipts(settings),
        "ways_on": ways_on,
    }
    try:
        receipt["failure_receipt"] = str(write_stamped_receipt(settings, FAILURE_RECEIPT_PREFIX, receipt))
    except OSError as write_error:
        receipt["failure_receipt"] = f"could NOT be written: {write_error}"
    print(
        f"Basin retirement {settings.succession_id!r} / {settings.basin_version_id} FAILED in step {step}: {reason}",
        file=sys.stderr,
    )
    for way in ways_on:
        print(f"  - {way}", file=sys.stderr)
    print(f"Failure receipt: {receipt['failure_receipt']}", file=sys.stderr)
    return receipt


def _identity(settings: Settings, basin: Basin | None) -> dict[str, Any]:
    return {
        "succession_id": settings.succession_id,
        "basin_version_id": settings.basin_version_id,
        "basin_id": basin.basin_id if basin else None,
        "basin_key": basin.key if basin else None,
        "receipt_directory": str(settings.directory),
    }


def apply(settings: Settings) -> tuple[int, dict[str, Any]]:
    basin, _facts, refusals = preconditions(settings)
    if refusals or basin is None:
        listed = "\n".join(f"  - {refusal}" for refusal in refusals)
        raise RetirementRefusal(f"Refused before any step:\n{listed}\n{NOTHING_WRITTEN}")
    done: dict[str, str] = {}
    for step in STEPS:
        try:
            done[step] = run_step(settings, basin, step)
        except Exception as error:  # noqa: BLE001 - every failure of a step is recorded; a kill is not caught
            failure = _record_failure(settings, basin, step, error)
            return 1, {**_identity(settings, basin), "outcome": "failed", "steps": done, "failure": failure}
    print(
        f"Basin retirement {settings.succession_id!r} / {settings.basin_version_id} completed. Not done by this tool:",
        file=sys.stderr,
    )
    for note in NOT_DONE_BY_THIS_TOOL:
        print(f"  - {note}", file=sys.stderr)
    return 0, {
        **_identity(settings, basin),
        "outcome": "completed",
        "steps": done,
        "not_done_by_this_tool": list(NOT_DONE_BY_THIS_TOOL),
    }


def dry_run(settings: Settings) -> tuple[int, dict[str, Any]]:
    """Report what an apply would do.  Changes no file, writes no receipt and commits no write transaction.

    A failed precondition is listed and the read-only checks go on.  The
    missing receipts of this run's own earlier steps are not reported: an
    apply writes them on its way.  The autopipe unit is read once, not waited for.
    """

    basin, facts, found = preconditions(settings)
    refusals = [f"precondition: {refusal}" for refusal in found]
    try:
        unit: Any = autopipe.read_unit().record()
    except StepFailure as error:
        unit = f"unknown ({error})"
        refusals.append(f"autopipe unit: {error}")
    done = completed_steps(settings)
    previews: dict[str, Callable[[], tuple[dict[str, Any], list[str]]]] = {
        "exclude": lambda: steps.preview_exclude(settings, basin),
        "supersede": lambda: steps.preview_supersede(settings, preconditions_hold=not found),
        "deactivate": lambda: steps.preview_deactivate(settings),
        "verify": lambda: steps.preview_verify(settings),
    }
    reports: dict[str, Any] = {}
    for step in STEPS:
        if step in done:
            reports[step] = {"status": "completed", "receipt": str(settings.step_receipt(step))}
            continue
        reports[step], step_refusals = previews[step]()
        refusals.extend(step_refusals)
    report = {
        **_identity(settings, basin),
        "dry_run": True,
        "preconditions": facts,
        "autopipe_unit_now": unit,
        "steps": reports,
        "would_be_refused": refusals,
        "not_done_by_this_tool": list(NOT_DONE_BY_THIS_TOOL),
    }
    return (1 if refusals else 0), report
