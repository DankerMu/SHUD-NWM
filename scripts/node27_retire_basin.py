#!/usr/bin/env python
"""Retire one basin version on node-27 after its node-22 removal, in a fixed, receipt-gated order.

The node-22 half of a retirement (``scripts/node22_model_succession.py --kind
remove_basin``) takes the models of a basin out of the scheduler.  This command
is the node-27 half, with the same ``--succession-id``, for one basin version
per run:

``exclude`` -> ``supersede`` -> ``deactivate`` -> ``verify``

``exclude`` appends the basin key to the one ``AUTOPIPE_EXCLUDE_BASINS`` line of
the ingest env file (backup first, atomic replacement, every other byte kept)
and waits for an autopipe round that started after the edit.  ``supersede``
backs the runs of the basin version in ``succeeded``, ``parsed`` or
``published`` up to ``hydro-run-backup.csv`` and sets them ``superseded`` in the
same transaction; no other column is set.  ``deactivate`` runs the deactivate
preflight of every active ``core.model_instance`` row of the basin version
(selected by exact ``basin_version_id``: baseline and ``dg_*`` alike) and then
the model lifecycle operation, one row at a time.  ``verify`` waits for one more
full autopipe round and reads back that nothing was reverted.

Each completed step leaves ``retire-<step>.json`` under
``<receipt-root>/<succession-id>/retire-<basin-version-id>/``; a step whose
receipt exists is skipped, so running the same command again resumes, and a
step refuses when a receipt it requires is missing.  A failed step leaves
``retire-failed-<stamp>.json`` and the tool exits non-zero.  Nothing here
reverts a step.

Refused before anything is read from the database or written, in both modes:
``NHMS_AUTH_MODE`` or ``AUTH_BACKEND`` set; ``DATABASE_URL`` of the process not
the one in the env file (one plain unquoted line); another instance running
(an exclusive lock on ``<env file>.retire-lock``).  Refused before any step:
the succession has no node-22 finish receipt of kind ``remove_basin``; the
basin version is not in ``core.basin_version``; the plan removed no model of
it; the canonical manifest still holds a row of the basin, under this version
or any other.

Without ``--apply`` the run is a dry-run: it changes no file, writes no receipt
and commits no write transaction.  Its supersede step runs the real statements
and rolls them back (only when no precondition is refused), its deactivate step
runs the preflights only, and the autopipe unit is read once, not waited for.

The tool never moves a Basins directory, never writes ``core.basin`` or
``core.basin_version``, never sets ``updated_at`` of a run, and starts, stops,
enables or disables no unit: its one ``systemctl`` call is ``--user show``.

Run it on node-27 with the ingest environment loaded:
``cd /home/nwm/NWM && uv run --no-sync python -m scripts.node27_retire_basin``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from scripts.basin_retirement import envfile, run
from scripts.basin_retirement.autopipe import SYSTEMCTL_ENV
from scripts.basin_retirement.model import (
    BASIN_VERSION_ID_PATTERN,
    DATABASE_URL_ENV,
    DEFAULT_AUTOPIPE_WAIT_SECONDS,
    DEFAULT_ENV_FILE_KEY,
    NOTHING_WRITTEN,
    OBJECT_STORE_ROOT_ENV,
    STEPS,
    RetirementRefusal,
    Settings,
    StepFailure,
    validate_basin_version_id,
)

# The logic lives in ``scripts/basin_retirement/``; this module is the command.
__all__ = [
    "STEPS",
    "SYSTEMCTL_ENV",
    "RetirementRefusal",
    "Settings",
    "StepFailure",
    "main",
    "settings_from_arguments",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): no file is changed, no receipt is written, no write transaction is committed and no"
    " unit is started or stopped. An --apply adds the basin to AUTOPIPE_EXCLUDE_BASINS, waits for an autopipe"
    " round, supersedes the runs of the basin version, deactivates its model rows, waits for one more round and"
    " verifies."
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--succession-id",
        required=True,
        help="The succession whose node-22 remove_basin run took the basin out of the scheduler "
        "([A-Za-z0-9._-]{1,80}).",
    )
    parser.add_argument(
        "--basin-version-id",
        required=True,
        help=f"The basin version to retire, exactly as in core.basin_version ({BASIN_VERSION_ID_PATTERN.pattern}). "
        "One per run; its receipts and backups are in <receipt-root>/<succession-id>/retire-<basin-version-id>/.",
    )
    parser.add_argument("--operator-id", required=True, help="Recorded in every receipt and in the audit log.")
    parser.add_argument(
        "--reason", required=True, help="Why the basin is retired; recorded and passed to the lifecycle operation."
    )
    parser.add_argument(
        "--env-file",
        help=f"The ingest env file the autopipe sources (default: {DEFAULT_ENV_FILE_KEY} of this checkout).",
    )
    parser.add_argument(
        "--receipt-root",
        help=f"Directory holding succession receipts (default: <{OBJECT_STORE_ROOT_ENV}>/scheduler/succession).",
    )
    parser.add_argument(
        "--autopipe-wait-seconds",
        type=float,
        default=DEFAULT_AUTOPIPE_WAIT_SECONDS,
        help="How long exclude and verify each wait for an autopipe round that started after their reference "
        "(default: %(default)s). The same command waits again after a timeout.",
    )
    parser.add_argument("--apply", action="store_true", help="Run the steps. Without it the run is a dry-run.")
    return parser.parse_args(argv)


def settings_from_arguments(args: argparse.Namespace) -> Settings:
    """The settings of a run: the basin version from the command line, the database and the store from the environment.

    Reads no file and opens no connection.
    """

    try:
        succession.validate_succession_id(args.succession_id)
    except succession.SuccessionReceiptError as error:
        raise RetirementRefusal(f"{error} {NOTHING_WRITTEN}") from error
    validate_basin_version_id(args.basin_version_id)
    empty = [option for option in ("operator_id", "reason") if not str(getattr(args, option)).strip()]
    if empty:
        names = ", ".join(f"--{option.replace('_', '-')}" for option in empty)
        raise RetirementRefusal(f"Refused: {names} must not be empty. {NOTHING_WRITTEN}")
    if not args.autopipe_wait_seconds > 0:
        raise RetirementRefusal(f"Refused: --autopipe-wait-seconds must be greater than 0. {NOTHING_WRITTEN}")
    values = {name: os.environ.get(name) or "" for name in (DATABASE_URL_ENV, OBJECT_STORE_ROOT_ENV)}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RetirementRefusal(
            f"Refused: {', '.join(missing)} must be set (load the ingest env file, {DEFAULT_ENV_FILE_KEY}). "
            f"{NOTHING_WRITTEN}"
        )
    object_store_root = Path(values[OBJECT_STORE_ROOT_ENV])
    if not object_store_root.is_absolute():
        raise RetirementRefusal(f"Refused: {OBJECT_STORE_ROOT_ENV} must be an absolute path. {NOTHING_WRITTEN}")
    env_file = Path(args.env_file) if args.env_file else REPO_ROOT / DEFAULT_ENV_FILE_KEY
    return Settings(
        succession_id=args.succession_id,
        basin_version_id=args.basin_version_id,
        operator_id=args.operator_id.strip(),
        reason=args.reason.strip(),
        # Made absolute without resolving it: a symlinked env file must stay visible as one.
        env_file=Path(os.path.abspath(env_file)),
        receipt_root=(
            Path(os.path.abspath(args.receipt_root))
            if args.receipt_root
            else succession.default_receipt_root(object_store_root)
        ),
        object_store_root=object_store_root,
        database_url=values[DATABASE_URL_ENV],
        autopipe_wait_seconds=args.autopipe_wait_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report: dict[str, Any]
    try:
        # Before anything is read from the database or written, in both modes.
        run.refuse_auth_environment()
        settings = settings_from_arguments(args)
        envfile.check_database_binding(settings)
        lock = envfile.hold_instance_lock(settings)
        try:
            if args.apply:
                status, report = run.apply(settings)
            else:
                print(DRY_RUN_NOTICE, file=sys.stderr, flush=True)
                status, report = run.dry_run(settings)
        finally:
            os.close(lock)
    except (RetirementRefusal, StepFailure) as error:
        # A refusal before any step: nothing was written.
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
