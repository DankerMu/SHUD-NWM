#!/usr/bin/env python
"""Publish the merged scheduler registry manifest of a model succession.

On direct-grid production the model set only changes by publishing a merged
registry manifest: the current canonical rows, minus the rows being retired,
plus rows produced by ``scripts/provision_direct_grid_scheduler_registry.py``.
This tool does that merge on node-22 and publishes it to the canonical manifest
and to the worker mirror.  It is DB-free: it refuses when a database variable
is set and never opens a connection.

The run is a dry-run unless ``--apply`` is given: it reads both manifests, does
every check, predicts the size of the merged manifest and writes nothing but
its own receipt under ``--receipt-root`` (when ``--succession-id`` is given).
``--apply`` requires the dry-run receipt of the same succession id and refuses
unless that receipt recorded the same operations, the same provision apply
receipt, the same canonical rows and the same merged model list.  It then backs
up both manifests, publishes the canonical one with a compare-and-swap on the
bytes it read, publishes the mirror with the same ``generated_at`` and reads
both back; if the run does not end with both published and equal (also when it
is interrupted or sent SIGTERM / SIGHUP), every manifest this run committed is
put back.  It never stops or starts a timer and never runs the provider refresh.

Run it on node-22 as the owner of both manifests:
``cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock
from scripts.merged_registry_publish.apply import TerminatedBySignal, _apply
from scripts.merged_registry_publish.model import (
    _NOTHING_WRITTEN,
    APPLY_RECEIPT_NAME,
    CANONICAL_MANIFEST_ENV,
    DRY_RUN_NOTICE,
    DRY_RUN_RECEIPT_NAME,
    MIRROR_MANIFEST_ENV,
    OBJECT_STORE_PREFIX_ENV,
    OBJECT_STORE_ROOT_ENV,
    PROVIDER_STORE_ROOT_ENV,
    PROVISION_APPLY_RECEIPT_NAME,
    PROVISION_RECEIPT_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    RECEIPT_STEP,
    REFRESH_LOCK_ENV,
    MergedRegistryPublishError,
    Operations,
    _contained_absolute,
    _Settings,
    refuse_database_environment,
)
from scripts.merged_registry_publish.planning import _plan
from scripts.merged_registry_publish.receipts import _receipt

# The logic lives in ``scripts/merged_registry_publish/``; this module is the
# command and the names callers import.
__all__ = [
    "APPLY_RECEIPT_NAME",
    "CANONICAL_MANIFEST_ENV",
    "DRY_RUN_NOTICE",
    "DRY_RUN_RECEIPT_NAME",
    "MIRROR_MANIFEST_ENV",
    "OBJECT_STORE_PREFIX_ENV",
    "OBJECT_STORE_ROOT_ENV",
    "PROVIDER_STORE_ROOT_ENV",
    "PROVISION_APPLY_RECEIPT_NAME",
    "PROVISION_RECEIPT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_STEP",
    "REFRESH_LOCK_ENV",
    "MergedRegistryPublishError",
    "Operations",
    "TerminatedBySignal",
    "main",
    "publish_merged_scheduler_registry",
    "refuse_database_environment",
]


# --- entry points ------------------------------------------------------------


def publish_merged_scheduler_registry(
    *,
    canonical_manifest: str | Path,
    mirror_manifest: str | Path,
    object_store_root: str | Path,
    provider_store_root: str | Path,
    object_store_prefix: str,
    operations: Operations,
    operator_id: str,
    apply: bool = False,
    succession_id: str | None = None,
    provision_succession_id: str | None = None,
    receipt_root: str | Path | None = None,
    new_rows_registry: str | Path | None = None,
    refresh_lock: str | Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Plan (default) or apply the merged publish and return the receipt of the run.

    Without ``apply`` neither manifest is written, no backup is made and no
    lock is taken.  With ``apply`` the dry-run receipt of the same succession
    must have recorded the same operations, provision apply receipt, canonical
    rows and merged model list, and the provider refresh lock is held, without
    blocking, for the whole run.  Every refusal and every failed apply raises
    ``MergedRegistryPublishError``.
    """

    refuse_database_environment()
    if succession_id is not None:
        succession.validate_succession_id(succession_id)
    elif apply:
        raise MergedRegistryPublishError("--apply requires --succession-id (and the dry-run receipt of that id).")
    if provision_succession_id is not None:
        succession.validate_succession_id(provision_succession_id)
    if not object_store_prefix:
        raise MergedRegistryPublishError(f"Refused: {OBJECT_STORE_PREFIX_ENV} is required.")
    store_root, provider_root = Path(object_store_root), Path(provider_store_root)
    settings = _Settings(
        canonical_path=Path(canonical_manifest),
        mirror_path=Path(mirror_manifest),
        object_store_root=store_root,
        provider_store_root=provider_root,
        object_store_prefix=object_store_prefix,
        operations=operations,
        operator_id=operator_id,
        succession_id=succession_id,
        provision_succession_id=provision_succession_id or succession_id,
        receipt_root=Path(receipt_root) if receipt_root else succession.default_receipt_root(provider_root),
        new_rows_registry=Path(new_rows_registry) if new_rows_registry else None,
        clock=clock,
    )
    _contained_absolute(
        settings.canonical_path, provider_root, what="the canonical manifest", root_name=PROVIDER_STORE_ROOT_ENV
    )
    _contained_absolute(settings.mirror_path, store_root, what="the worker mirror", root_name=OBJECT_STORE_ROOT_ENV)
    if settings.canonical_path == settings.mirror_path:
        raise MergedRegistryPublishError("Refused: the canonical manifest and the worker mirror are the same path.")
    if operations.new_model_ids and settings.provision_succession_id is None:
        raise MergedRegistryPublishError(
            "Refused: --replace / --add need the provision apply receipt of a succession; give --succession-id "
            f"or --provision-succession-id. {_NOTHING_WRITTEN}"
        )

    if not apply:
        return _dry_run(settings)
    if not refresh_lock:
        raise MergedRegistryPublishError(
            f"--apply refused: {REFRESH_LOCK_ENV} is required; the apply holds the provider refresh lock. "
            f"{_NOTHING_WRITTEN}"
        )
    # The rule of the provider refresh runner for the same variable: a relative path would be another lock.
    lock_path = Path(refresh_lock).expanduser()
    if not lock_path.is_absolute():
        raise MergedRegistryPublishError(
            f"--apply refused: {REFRESH_LOCK_ENV} must be an absolute path, as the provider refresh requires "
            f"(found {str(refresh_lock)!r}). {_NOTHING_WRITTEN}"
        )
    receipt: dict[str, Any] | None = None
    try:
        with provider_destination_lock(lock_path, blocking=False):
            receipt = _apply(settings)
    except ProviderAtomicError as error:
        if receipt is None:
            held = (
                " The provider refresh is running; wait for it to finish."
                if error.reason == "provider_already_running"
                else ""
            )
            raise MergedRegistryPublishError(
                f"--apply refused: the provider refresh lock {refresh_lock} could not be taken "
                f"({error.reason}).{held} {_NOTHING_WRITTEN}"
            ) from error
        # The publish and its receipt are complete; only releasing the lock failed.
        print(
            f"WARNING: the provider refresh lock {refresh_lock} was not released cleanly ({error.reason}).",
            file=sys.stderr,
        )
    return receipt


def _dry_run(settings: _Settings) -> dict[str, Any]:
    target = (
        settings.receipt_root / settings.succession_id / DRY_RUN_RECEIPT_NAME
        if settings.succession_id is not None
        else None
    )
    if target is not None and os.path.lexists(target):
        raise succession.SuccessionReceiptError(
            f"Receipt {target} already exists and is never overwritten; a changed plan needs a new "
            "--succession-id (with --provision-succession-id naming the unchanged provision)."
        )
    plan = _plan(settings)
    receipt = _receipt(settings, plan, dry_run=True, outcome="planned")
    if target is not None:
        succession.prepare_receipt_target(target, receipt_root=settings.receipt_root)
        try:
            succession.write_receipt(target, receipt)
        except OSError as error:
            raise succession.SuccessionReceiptError(f"Cannot write the dry-run receipt {target}: {error}") from error
        print(f"Publish receipt written: {target}", file=sys.stderr)
    return receipt


def _replace_pair(value: str) -> tuple[str, str]:
    old, separator, new = value.partition(":")
    if not separator or not old.strip() or not new.strip() or ":" in new:
        raise argparse.ArgumentTypeError(f"invalid --replace value {value!r}; expected <old_model_id>:<new_model_id>")
    return old.strip(), new.strip()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--replace",
        action="append",
        default=[],
        type=_replace_pair,
        metavar="OLD_MODEL_ID:NEW_MODEL_ID",
        help="Substitute a canonical row in place by a provisioned row of the same basin and source. Repeatable.",
    )
    parser.add_argument(
        "--add", action="append", default=[], metavar="NEW_MODEL_ID",
        help="Append a provisioned row. Repeatable; every source of a new basin must be added together.",
    )
    parser.add_argument(
        "--remove", action="append", default=[], metavar="MODEL_ID",
        help="Drop a canonical row. Repeatable; every source of a basin must be removed together.",
    )
    parser.add_argument("--operator-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up and publish both manifests. Without it the run is a dry-run. Requires --succession-id, "
        f"that id's dry-run receipt and {REFRESH_LOCK_ENV}.",
    )
    parser.add_argument(
        "--succession-id",
        help="Succession this run belongs to ([A-Za-z0-9._-]{1,80}). The run writes one receipt, never "
        "overwritten, to <receipt-root>/<succession-id>/publish-dry-run.json or publish-apply.json "
        "(publish-apply-failed-<stamp>.json for an apply that did not publish).",
    )
    parser.add_argument(
        "--provision-succession-id",
        help="Succession whose provision-apply.json produced the new rows (default: --succession-id).",
    )
    parser.add_argument(
        "--receipt-root",
        help=f"Directory holding succession receipts (default: <{PROVIDER_STORE_ROOT_ENV}>/scheduler/succession).",
    )
    parser.add_argument(
        "--new-rows-registry",
        help="Registry file written by the provision --apply. Optional when the provision apply receipt records "
        f"an object_store_key for its output_registry (resolved under {PROVIDER_STORE_ROOT_ENV}).",
    )
    parser.add_argument(
        "--canonical-manifest",
        default=os.getenv(CANONICAL_MANIFEST_ENV),
        help=f"Canonical registry manifest (default: {CANONICAL_MANIFEST_ENV}).",
    )
    parser.add_argument(
        "--mirror-manifest",
        default=os.getenv(MIRROR_MANIFEST_ENV),
        help=f"Worker mirror of the registry manifest (default: {MIRROR_MANIFEST_ENV}).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        values = {
            CANONICAL_MANIFEST_ENV: args.canonical_manifest,
            MIRROR_MANIFEST_ENV: args.mirror_manifest,
            OBJECT_STORE_ROOT_ENV: os.getenv(OBJECT_STORE_ROOT_ENV),
            PROVIDER_STORE_ROOT_ENV: os.getenv(PROVIDER_STORE_ROOT_ENV),
            OBJECT_STORE_PREFIX_ENV: os.getenv(OBJECT_STORE_PREFIX_ENV),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise MergedRegistryPublishError(f"Refused: {', '.join(missing)} must be set. {_NOTHING_WRITTEN}")
        if not args.apply:
            print(DRY_RUN_NOTICE, flush=True)
        receipt = publish_merged_scheduler_registry(
            canonical_manifest=str(values[CANONICAL_MANIFEST_ENV]),
            mirror_manifest=str(values[MIRROR_MANIFEST_ENV]),
            object_store_root=str(values[OBJECT_STORE_ROOT_ENV]),
            provider_store_root=str(values[PROVIDER_STORE_ROOT_ENV]),
            object_store_prefix=str(values[OBJECT_STORE_PREFIX_ENV]),
            operations=Operations(replace=tuple(args.replace), add=tuple(args.add), remove=tuple(args.remove)),
            operator_id=args.operator_id,
            apply=args.apply,
            succession_id=args.succession_id,
            provision_succession_id=args.provision_succession_id,
            receipt_root=args.receipt_root,
            new_rows_registry=args.new_rows_registry,
            refresh_lock=os.getenv(REFRESH_LOCK_ENV),
        )
    except (MergedRegistryPublishError, succession.SuccessionReceiptError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
