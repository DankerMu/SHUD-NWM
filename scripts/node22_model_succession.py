#!/usr/bin/env python
"""Run the compute-side steps of a model succession on node-22, in their fixed order.

After the provision step on node-27, a recalibration succession needs, on
node-22: the new packages copied from the shared store to the compute store,
the state rows cloned from the old models to the new ones, the merged registry
manifest published, one provider refresh, and the scheduler timer stopped
around the last three.  This command runs them as the seven steps of
``--kind recalibration``:

``copyback`` -> ``preflight`` -> ``begin`` -> ``clone`` -> ``publish`` -> ``refresh`` -> ``finish``

``--kind cold_start`` is the succession of a structural change (mesh, river
network, any other state-compatibility surface, or a new ``cfg.ic``): the
state of the old models cannot be carried, so its six steps have no ``clone``
and no state index is read or written.  Each new model starts from the calibrated
initial condition in its package, which ``preflight`` audits (``ic-audit.json``)
and ``publish`` requires; the plan, the receipts and the reports say in
``continuity`` that the hydrograph is not continuous.  ``preflight`` of either
kind refuses a pair whose packages say it is the other kind.

``copyback`` and ``preflight`` (the kind check, then the dry-runs of the clone
tool and of the publish tool) run while the scheduler is running.  ``begin``
records whether the scheduler timer was active, stops it and waits for a
running pass to end by itself; ``finish`` starts the timer again when it was
active.  Each completed step leaves ``step-<name>.json`` under
``<receipt-root>/<succession-id>/``; a step whose receipt exists is skipped, so
running the same command again resumes, and a step refuses when the receipt of
the step before it is missing.  The clone and publish tools write their own
receipts beside them, unchanged.

Without ``--apply`` the run is a dry-run: it changes no file, writes no
receipt, issues only ``systemctl --user is-active`` queries and prints what the
apply would do as JSON.

When a step of an ``--apply`` fails, the tool does NOT start the timer.  It
exits non-zero and leaves ``succession-failed-<stamp>.json`` saying what
failed, the unit states it observed and the ways on: fix the cause and run the
same command, or give up with ``--abort --confirm-timer-start``, which starts
the timer when it was active and closes the succession id.  While a succession
that stopped the timer is neither finished nor aborted, every other succession
id is refused: it would record the timer as inactive and leave it stopped.

The tool is DB-free: it refuses when a database variable is set and never
opens a connection.  It never stops or kills the scheduler service, never
enables, disables or masks a unit and writes nothing under a systemd directory.

Run it on node-22 as the owner of both manifests, with the provider-refresh
environment loaded:
``cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_model_succession``.
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
from scripts.model_succession import plan as planning
from scripts.model_succession import run
from scripts.model_succession.model import (
    CANONICAL_MANIFEST_ENV,
    DEFAULT_PASS_WAIT_SECONDS,
    KIND_COLD_START,
    KIND_RECALIBRATION,
    MIRROR_MANIFEST_ENV,
    MIRROR_STATE_INDEX_KEY,
    OBJECT_STORE_PREFIX_ENV,
    OBJECT_STORE_ROOT_ENV,
    PROVIDER_STORE_ROOT_ENV,
    REFRESH_LOCK_ENV,
    REFRESH_RECEIPT_ROOT_ENV,
    REQUIRED_ENVIRONMENT,
    STATE_INDEX_ENV,
    STEPS_BY_KIND,
    SYSTEMCTL_ENV,
    HardStop,
    ModelSuccessionRefusal,
    Plan,
    Settings,
    StepFailure,
)

# The logic lives in ``scripts/model_succession/``; this module is the command.
__all__ = [
    "SYSTEMCTL_ENV",
    "HardStop",
    "ModelSuccessionRefusal",
    "Plan",
    "Settings",
    "StepFailure",
    "main",
    "settings_from_arguments",
]

DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): no file is changed, no receipt is written and no unit is started or stopped."
    " An --apply copies the new packages, checks that every pair is a recalibration, runs both tools' dry-runs,"
    " stops the scheduler timer, clones, publishes, runs the provider refresh and starts the timer again."
)
COLD_START_DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): no file is changed, no receipt is written and no unit is started or stopped."
    " An --apply copies the new packages, checks that every pair is a structural change, audits their packaged"
    " initial conditions, runs the publish tool's dry-run, stops the scheduler timer, publishes, runs the"
    " provider refresh and starts the timer again."
    " No state is carried from the old models: the hydrograph of these basins is not continuous."
)


def _pair(value: str) -> tuple[str, str]:
    old, separator, new = value.partition(":")
    if not separator or not old.strip() or not new.strip() or ":" in new:
        raise argparse.ArgumentTypeError(f"invalid --pair value {value!r}; expected <old_model_id>:<new_model_id>")
    return old.strip(), new.strip()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--succession-id",
        required=True,
        help="Succession this run belongs to ([A-Za-z0-9._-]{1,80}); its receipts are in "
        "<receipt-root>/<succession-id>/.",
    )
    parser.add_argument(
        "--provision-succession-id",
        help="Succession whose provision-apply.json produced the new rows (default: --succession-id).",
    )
    parser.add_argument(
        "--kind",
        required=True,
        choices=tuple(STEPS_BY_KIND),
        help=f"The kind of succession: {KIND_RECALIBRATION} carries the state of the old models through clone "
        f"rows; {KIND_COLD_START} carries none and starts each new model from the initial condition in its "
        "package. preflight refuses a pair whose packages say it is the other kind.",
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        type=_pair,
        metavar="OLD_MODEL_ID:NEW_MODEL_ID",
        help="A model replaced by its successor; one per source of each basin. Repeatable; the order is kept.",
    )
    parser.add_argument(
        "--cutover-time",
        required=True,
        metavar="YYYYMMDDHH",
        help=f"{KIND_RECALIBRATION}: valid_time of the clone rows. {KIND_COLD_START}: the cutover time the "
        "operator declares; it is recorded in continuity and not enforced.",
    )
    parser.add_argument("--operator-id", required=True)
    parser.add_argument(
        "--new-rows-registry",
        help="Registry file written by the provision --apply, as node-22 sees it. Optional when the provision "
        f"apply receipt records an object_store_key for it (resolved under {PROVIDER_STORE_ROOT_ENV}).",
    )
    parser.add_argument(
        "--receipt-root",
        help=f"Directory holding succession receipts (default: <{PROVIDER_STORE_ROOT_ENV}>/scheduler/succession).",
    )
    parser.add_argument(
        "--mirror-state-index",
        help=f"The compute-side copy of the state index (default: <{OBJECT_STORE_ROOT_ENV}>/{MIRROR_STATE_INDEX_KEY}).",
    )
    parser.add_argument(
        "--pass-wait-seconds",
        type=float,
        default=DEFAULT_PASS_WAIT_SECONDS,
        help="How long the begin step waits for a running scheduler pass to end by itself (default: %(default)s; a "
        "healthy pass has been measured at 193 minutes). The same command resumes the wait after a timeout.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Run the steps. Without it the run is a dry-run.")
    mode.add_argument(
        "--abort",
        action="store_true",
        help="Give the succession up. With --confirm-timer-start: start the scheduler timer if it was active "
        "when the succession began, write abort-<stamp>.json and close the id. Without it: report only.",
    )
    parser.add_argument("--confirm-timer-start", action="store_true", help="Confirms what --abort does.")
    parser.add_argument("--output", help="Also write the JSON report of the run to this file.")
    args = parser.parse_args(argv)
    if args.confirm_timer_start and not args.abort:
        parser.error("--confirm-timer-start is only valid with --abort")
    return args


def settings_from_arguments(args: argparse.Namespace) -> Settings:
    """The settings of a run: the plan from the command line, the paths from the provider-refresh environment."""

    planning.refuse_database()
    values = {name: os.environ.get(name) or "" for name in REQUIRED_ENVIRONMENT}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ModelSuccessionRefusal(
            f"Refused: {', '.join(missing)} must be set (load infra/env/compute.scheduler-provider-refresh.env). "
            "Nothing was written."
        )
    paths = {name: Path(value) for name, value in values.items() if name != OBJECT_STORE_PREFIX_ENV}
    relative = [f"{name}={path}" for name, path in paths.items() if not path.is_absolute()]
    if relative:
        raise ModelSuccessionRefusal(f"Refused: these must be absolute paths: {', '.join(relative)}.")
    plan = planning.build_plan(
        succession_id=args.succession_id,
        provision_succession_id=args.provision_succession_id,
        kind=args.kind,
        pairs=args.pair,
        cutover_time=args.cutover_time,
    )
    store_root, provider_root = paths[OBJECT_STORE_ROOT_ENV], paths[PROVIDER_STORE_ROOT_ENV]
    return Settings(
        plan=plan,
        operator_id=args.operator_id,
        object_store_root=store_root,
        provider_store_root=provider_root,
        object_store_prefix=values[OBJECT_STORE_PREFIX_ENV],
        canonical_manifest=paths[CANONICAL_MANIFEST_ENV],
        mirror_manifest=paths[MIRROR_MANIFEST_ENV],
        state_index=values[STATE_INDEX_ENV],
        mirror_state_index=args.mirror_state_index or str(store_root / MIRROR_STATE_INDEX_KEY),
        refresh_lock=values[REFRESH_LOCK_ENV],
        refresh_receipt_root=paths[REFRESH_RECEIPT_ROOT_ENV],
        receipt_root=Path(args.receipt_root) if args.receipt_root else succession.default_receipt_root(provider_root),
        new_rows_registry=Path(args.new_rows_registry) if args.new_rows_registry else None,
        pass_wait_seconds=args.pass_wait_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report: dict[str, Any]
    try:
        settings = settings_from_arguments(args)
        if args.abort:
            status, report = run.abort(settings, confirm_timer_start=args.confirm_timer_start)
        elif args.apply:
            status, report = run.apply(settings)
        else:
            notice = COLD_START_DRY_RUN_NOTICE if settings.plan.kind == KIND_COLD_START else DRY_RUN_NOTICE
            print(notice, file=sys.stderr, flush=True)
            status, report = run.dry_run(settings)
    except (ModelSuccessionRefusal, StepFailure) as error:
        # A refusal before any step, or a unit whose state cannot be read outside a step: nothing was written.
        print(str(error), file=sys.stderr)
        return 1
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
