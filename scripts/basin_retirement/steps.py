"""The four steps of a basin retirement on node-27, and what a dry-run says of each.

Part of ``scripts/node27_retire_basin.py`` (the entry point).

``exclude`` -> ``supersede`` -> ``deactivate`` -> ``verify``

``exclude`` puts the basin key into ``AUTOPIPE_EXCLUDE_BASINS`` and waits for
an autopipe round that started after the edit: until then a round in flight
re-activates whatever is retired.  ``supersede`` backs the candidate runs up
and sets them ``superseded``.  ``deactivate`` takes every active model row of
the basin version through the model lifecycle operation, after a preflight of
all of them.  ``verify`` waits for one more full round and reads back that
nothing was reverted.  Nothing here reverts a step.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from packages.common.auth_policy import trusted_internal_policy_decision
from scripts.basin_retirement import autopipe, database, envfile
from scripts.basin_retirement.model import Basin, Settings, StepFailure

DEACTIVATE_ACTION = "models.deactivate"
# The transition outcomes of the lifecycle operation that mean the row is inactive now.
DONE_STATUSES = ("allowed", "already_current")
RUN_BACKUP_MODE = 0o640
REVERTED_WHY = (
    "Something on node-27 registered the basin again after the retirement: an autopipe round that had read the "
    "exclusion list before the key was in it, a round run while the key was missing, or an ingest forced by "
    "hand. Put the cause right (the exclusion entry first) and run the same command."
)


def _candidates_text() -> str:
    return " / ".join(database.CANDIDATE_STATUSES)


def exclude(settings: Settings, basin: Basin) -> dict[str, Any]:
    env = envfile.load(settings.env_file)
    # Resolved before the edit: a lock file that cannot be told fails the step without a write.
    lock_path = envfile.autopipe_lock_path(env.content, os.environ)
    reference_us = autopipe.monotonic_us()
    backup = settings.env_backup
    if basin.key in env.keys:
        facts: dict[str, Any] = {
            "file_changed": False,
            "note": "The key was already in the list: nothing was written.",
            "env_backup": str(backup) if os.path.lexists(backup) else None,
            "env_backup_outcome": "not written by this run",
            "sha256_before": env.sha256,
            "sha256_after": env.sha256,
        }
    else:
        written = envfile.add_key(settings, env, basin.key)
        # Taken after the rename: a round that started before it may have read the old list.
        reference_us = autopipe.monotonic_us()
        facts = {
            "file_changed": True,
            "env_backup": str(backup),
            "env_backup_outcome": written["backup_outcome"],
            "sha256_before": env.sha256,
            "sha256_after": written["sha256_after"],
        }
    waited = autopipe.wait_for_round(
        reference_us, timeout_seconds=settings.autopipe_wait_seconds, lock_path=lock_path
    )
    return {"basin_key": basin.key, "env_file": str(settings.env_file), **facts, "autopipe_round": waited}


def _recovered_from_backup(settings: Settings) -> dict[str, Any]:
    """The receipt of a supersede whose commit happened and whose receipt did not, or a failure naming both facts."""

    backup = settings.run_backup
    left = database.candidate_run_count(settings.database_url, settings.basin_version_id)
    if left:
        raise StepFailure(
            f"The run backup {backup} of an earlier attempt exists, and {left} runs of {settings.basin_version_id} "
            f"are in {_candidates_text()}. Either that attempt was killed before its commit, or runs were "
            "registered after it. Nothing was changed. Compare the CSV with the table, move the CSV aside, and run "
            "the same command."
        )
    facts = database.backup_facts(backup)
    return {
        **facts,
        "run_backup": str(backup),
        "run_backup_sha256": succession.file_sha256(backup),
        "updated_row_count": 0,
        "recovered_from_existing_backup": True,
        "note": "The backup of an earlier attempt was there and no run was left to supersede: the receipt is "
        "written from that backup and nothing was changed.",
    }


def supersede(settings: Settings, _basin: Basin) -> dict[str, Any]:
    backup = settings.run_backup
    if os.path.lexists(backup):
        return _recovered_from_backup(settings)
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, RUN_BACKUP_MODE)
    except OSError as error:
        raise StepFailure(f"The run backup {backup} cannot be created ({error}). No run was changed.") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), RUN_BACKUP_MODE)
            facts = database.backup_and_supersede(
                settings.database_url, settings.basin_version_id, backup=handle, backup_path=backup, commit=True
            )
    except database.CommitOutcomeUnknown:
        raise
    except BaseException:
        # Nothing was committed: a backup left behind would make the rerun stop at "backup exists".
        backup.unlink(missing_ok=True)
        raise
    return {
        **facts,
        "run_backup": str(backup),
        "run_backup_sha256": succession.file_sha256(backup),
        "recovered_from_existing_backup": False,
    }


def _lifecycle_arguments(settings: Settings, model_id: str) -> dict[str, Any]:
    return {
        "operation": "deactivate",
        "policy_decision": trusted_internal_policy_decision(
            DEACTIVATE_ACTION,
            target_type="model_instance",
            target_id=model_id,
            actor_id=f"ops:{settings.operator_id}",
            roles=("sys_admin",),
        ),
        "override_missing_active": True,
        "reason": settings.reason,
    }


def _codes(entries: Any) -> list[Any]:
    """Blockers or warnings of a preflight, as they go into a receipt."""

    return [dict(entry) if isinstance(entry, Mapping) else entry for entry in (entries or [])]


def _preflight_rows(store: Any, settings: Settings, active: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model_id in active:
        try:
            preflight = store.preflight_model_operation(model_id, **_lifecycle_arguments(settings, model_id))
        except Exception as error:  # the store raises for a missing row or a denied policy decision
            raise StepFailure(
                f"The deactivate preflight of {model_id} failed ({type(error).__name__}: {error}). No row was changed."
            ) from error
        status = preflight.get("status") if isinstance(preflight, Mapping) else None
        blockers = _codes(preflight.get("blockers")) if isinstance(preflight, Mapping) else []
        warnings = _codes(preflight.get("warnings")) if isinstance(preflight, Mapping) else []
        rows.append(
            {
                "model_id": model_id,
                "preflight_status": status,
                "preflight_blockers": blockers,
                "preflight_warnings": warnings,
                # Anything but a ready preflight without blockers stops the step before any row is changed.
                "preflight_ready": status == "ready" and not blockers,
            }
        )
    return rows


def _require_key(settings: Settings, basin: Basin, *, why: str) -> envfile.EnvFile:
    env = envfile.load(settings.env_file)
    if basin.key not in env.keys:
        raise StepFailure(
            f"The key {basin.key!r} is no longer in AUTOPIPE_EXCLUDE_BASINS of {settings.env_file}. {why}"
        )
    return env


def deactivate(settings: Settings, basin: Basin) -> dict[str, Any]:
    _require_key(
        settings,
        basin,
        why="Without it the next autopipe round re-activates every row this step deactivates. No row was changed.",
    )
    active = database.active_model_ids(settings.database_url, settings.basin_version_id)
    store = database.registry_store(settings.database_url)
    rows = _preflight_rows(store, settings, active)
    blocked = [row["model_id"] for row in rows if not row["preflight_ready"]]
    if blocked:
        raise StepFailure(
            f"The deactivate preflight is not ready for {blocked} (see model_rows in the failure receipt). No row "
            "was changed.",
            details={"model_rows": rows, "rows_done": []},
        )
    done: list[str] = []
    for row in rows:
        model_id = row["model_id"]
        try:
            # One row at a time, with the explicit policy decision; never trusted_internal=True.
            result = store.model_lifecycle_operation(model_id, **_lifecycle_arguments(settings, model_id))
        except Exception as error:  # a failure at one row must still name the rows done
            row["status"] = f"raised {type(error).__name__}"
            raise StepFailure(
                f"The deactivation of {model_id} failed ({type(error).__name__}: {error}). Rows deactivated before "
                f"it: {done}. The same command acts on the rows still active.",
                details={"model_rows": rows, "rows_done": done},
            ) from error
        status = result.get("status") if isinstance(result, Mapping) else None
        row["status"] = status
        if status not in DONE_STATUSES:
            preflight = result.get("preflight") if isinstance(result, Mapping) else None
            row["operation_blockers"] = _codes(preflight.get("blockers")) if isinstance(preflight, Mapping) else []
            raise StepFailure(
                f"The deactivation of {model_id} returned status {status!r}, not one of {list(DONE_STATUSES)} "
                f"(blockers: {row['operation_blockers']}). Rows deactivated before it: {done}. The same command "
                "acts on the rows still active.",
                details={"model_rows": rows, "rows_done": done},
            )
        audit = result.get("audit_reference")
        row["audit_log_id"] = audit.get("log_id") if isinstance(audit, Mapping) else None
        done.append(model_id)
    return {"basin_key": basin.key, "model_rows": rows, "rows_done": done}


def verify(settings: Settings, basin: Basin) -> dict[str, Any]:
    env = envfile.load(settings.env_file)
    lock_path = envfile.autopipe_lock_path(env.content, os.environ)
    waited = autopipe.wait_for_round(
        autopipe.monotonic_us(), timeout_seconds=settings.autopipe_wait_seconds, lock_path=lock_path
    )
    active = database.active_model_ids(settings.database_url, settings.basin_version_id)
    candidates = database.candidate_run_count(settings.database_url, settings.basin_version_id)
    key_present = basin.key in envfile.load(settings.env_file).keys
    reverted: list[str] = []
    if active:
        reverted.append(f"core.model_instance rows of {settings.basin_version_id} are active again: {active}")
    if candidates:
        reverted.append(f"{candidates} runs of {settings.basin_version_id} are back in {_candidates_text()}")
    if not key_present:
        reverted.append(f"the key {basin.key!r} is gone from AUTOPIPE_EXCLUDE_BASINS of {settings.env_file}")
    if reverted:
        raise StepFailure(
            f"The retirement was reverted: {'; '.join(reverted)}. {REVERTED_WHY}",
            details={
                "active_model_ids": active,
                "candidate_run_count": candidates,
                "key_in_exclusion_list": key_present,
                "autopipe_round": waited,
            },
        )
    return {
        "basin_key": basin.key,
        "active_model_row_count": 0,
        "candidate_run_count": 0,
        "key_in_exclusion_list": True,
        "autopipe_round": waited,
    }


# What a dry-run says of each step.  Each returns its part of the report and what an apply would be refused for.


def preview_exclude(settings: Settings, basin: Basin | None) -> tuple[dict[str, Any], list[str]]:
    try:
        env = envfile.load(settings.env_file)
        lock_path = envfile.autopipe_lock_path(env.content, os.environ)
    except StepFailure as error:
        return {"status": "would fail", "env_file": str(settings.env_file)}, [f"exclude: {error}"]
    report: dict[str, Any] = {
        "env_file": str(settings.env_file),
        "env_file_checks": "passed",
        "sha256_now": env.sha256,
        "autopipe_lock_path": lock_path,
    }
    if basin is None:
        report["status"] = "not predicted: the basin of the basin version is not known"
        return report, []
    present = basin.key in env.keys
    report.update(
        basin_key=basin.key,
        key_in_exclusion_list=present,
        would=(
            "write nothing (the key is already in the list)"
            if present
            else f"write the backup {settings.env_backup} and append the key to the one AUTOPIPE_EXCLUDE_BASINS line"
        )
        + f", then wait up to {settings.autopipe_wait_seconds:.0f} s for an autopipe round that started after it",
    )
    return report, []


def preview_supersede(settings: Settings, *, preconditions_hold: bool) -> tuple[dict[str, Any], list[str]]:
    backup = settings.run_backup
    if not preconditions_hold:
        # An apply would send no statement at all: the dry-run does not write to a basin it would refuse.
        return {
            "status": "not run",
            "note": "The supersede statements are run (and rolled back) only when no precondition is refused.",
        }, []
    try:
        if os.path.lexists(backup):
            left = database.candidate_run_count(settings.database_url, settings.basin_version_id)
            report: dict[str, Any] = {
                "run_backup": str(backup),
                "run_backup_exists": True,
                "candidate_run_count": left,
            }
            if left:
                return report, [
                    f"supersede: the run backup {backup} of an earlier attempt exists and {left} runs are in "
                    f"{_candidates_text()}; compare the CSV with the table, move the CSV aside, and run again."
                ]
            report["would"] = "write the receipt from the existing backup and change nothing"
            return report, []
        descriptor, name = tempfile.mkstemp(prefix="nhms-retire-basin-", suffix=".csv")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                facts = database.backup_and_supersede(
                    settings.database_url,
                    settings.basin_version_id,
                    backup=handle,
                    backup_path=temporary,
                    commit=False,
                )
        finally:
            temporary.unlink(missing_ok=True)
    except (StepFailure, OSError) as error:
        return {"status": "would fail"}, [f"supersede: {error}"]
    return {
        "statements": "executed and rolled back",
        "run_backup_exists": False,
        "row_count": facts["row_count"],
        "status_counts": facts["status_counts"],
        "updated_row_count": facts["updated_row_count"],
        "would": f"write {backup} and set these runs superseded in one transaction",
    }, []


def preview_deactivate(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    try:
        active = database.active_model_ids(settings.database_url, settings.basin_version_id)
        rows = _preflight_rows(database.registry_store(settings.database_url), settings, active)
    except StepFailure as error:
        return {"status": "would fail"}, [f"deactivate: {error}"]
    refusals = [
        f"deactivate: the preflight of {row['model_id']} is {row['preflight_status']!r} with blockers "
        f"{row['preflight_blockers']}"
        for row in rows
        if not row["preflight_ready"]
    ]
    return {
        "active_model_rows": rows,
        "would": "deactivate these rows one by one through the model lifecycle operation",
    }, refusals


def preview_verify(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    return {
        "would": f"wait up to {settings.autopipe_wait_seconds:.0f} s for one more autopipe round, then check that "
        "no model row of the basin version is active, no run of it is in "
        f"{_candidates_text()} and the key is still in the exclusion list",
    }, []
