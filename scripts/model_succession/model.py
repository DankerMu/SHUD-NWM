"""What the model succession tool shares: names, the three ways a run stops, the plan and the settings.

Part of ``scripts/node22_model_succession.py`` (the entry point).
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession
from scripts.merged_registry_publish.model import (
    CANONICAL_MANIFEST_ENV,
    MIRROR_MANIFEST_ENV,
    OBJECT_STORE_PREFIX_ENV,
    OBJECT_STORE_ROOT_ENV,
    PROVIDER_STORE_ROOT_ENV,
    REFRESH_LOCK_ENV,
)

STATE_INDEX_ENV = "NHMS_SCHEDULER_STATE_INDEX"
REFRESH_RECEIPT_ROOT_ENV = "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT"
SYSTEMCTL_ENV = "NHMS_MODEL_SUCCESSION_SYSTEMCTL"
DEFAULT_SYSTEMCTL = "/usr/bin/systemctl"
REQUIRED_ENVIRONMENT = (
    OBJECT_STORE_ROOT_ENV,
    PROVIDER_STORE_ROOT_ENV,
    OBJECT_STORE_PREFIX_ENV,
    CANONICAL_MANIFEST_ENV,
    MIRROR_MANIFEST_ENV,
    STATE_INDEX_ENV,
    REFRESH_LOCK_ENV,
    REFRESH_RECEIPT_ROOT_ENV,
)
MIRROR_STATE_INDEX_KEY = "scheduler/state-index/index-last.json"

TIMER_UNIT = "nhms-compute-scheduler.timer"
SERVICE_UNIT = "nhms-compute-scheduler.service"
REFRESH_UNIT = "nhms-scheduler-file-provider-refresh.service"
# The refresh unit's TimeoutStartSec is 7200 s; the blocking start is given a little longer.
REFRESH_START_TIMEOUT_SECONDS = 7500
DEFAULT_PASS_WAIT_SECONDS = 14400

KIND_RECALIBRATION = "recalibration"
STEPS = ("copyback", "preflight", "begin", "clone", "publish", "refresh", "finish")
# The steps that run while the scheduler is still running: a failure there happened before the timer was touched.
STEPS_BEFORE_TIMER = ("copyback", "preflight")
# The steps that change what the scheduler reads, or start it again: the scheduler must be stopped for them.
STEPS_NEEDING_STOPPED_SCHEDULER = ("clone", "publish", "refresh", "finish")

PLAN_SCHEMA_VERSION = "nhms.model_succession.plan.v1"
STEP_RECEIPT_SCHEMA_VERSION = "nhms.model_succession.step_receipt.v1"
TIMER_RECORD_SCHEMA_VERSION = "nhms.model_succession.timer_before_stop.v1"
FAILURE_SCHEMA_VERSION = "nhms.model_succession.failure_receipt.v1"
ABORT_SCHEMA_VERSION = "nhms.model_succession.abort_receipt.v1"
PLAN_NAME = "plan.json"
TIMER_RECORD_NAME = "timer-before-stop.json"
CLONE_DRY_RUN_NAME = "clone-dry-run.json"
CLONE_APPLY_NAME = "clone-apply.json"

RUNBOOK = "docs/runbooks/production-ops/recalibration-and-archive.md, section 5.7.1"
_NOTHING_WRITTEN = "Nothing was written."
# What ``step-finish.json`` and the report of an apply say was done to the timer.
TIMER_STARTED = "started"
TIMER_LEFT_STOPPED = "left_stopped_was_inactive_at_begin"
# Said wherever a new --succession-id is advised: the id that stopped the timer is the one that starts it.
ABORT_BEFORE_NEW_ID = (
    "If this succession has already stopped the scheduler timer (its timer-before-stop.json records "
    "timer_was_active true), give it up first: the command line it was first applied with and --abort "
    "--confirm-timer-start instead of --apply. Only that starts the timer again; a new id is refused while this "
    "one holds the timer."
)


class ModelSuccessionRefusal(RuntimeError):
    """The run is refused before any step: nothing was written and no failure receipt is left."""


class StepFailure(RuntimeError):
    """A step did not complete.  The same command resumes after the cause is removed."""

    hard_stop = False


class TimerNotStopped(StepFailure):
    """The ``stop`` of the scheduler timer failed, or the timer was still running after it."""


class HardStop(StepFailure):
    """A step ended in a state this tool does not retry; the runbook's manual procedure takes over."""

    hard_stop = True


@dataclass(frozen=True)
class Plan:
    succession_id: str
    provision_succession_id: str
    kind: str
    pairs: tuple[tuple[str, str], ...]  # (old_model_id, new_model_id), in command-line order
    cutover_time: str  # YYYYMMDDHH

    @property
    def old_ids(self) -> list[str]:
        return [old for old, _new in self.pairs]

    @property
    def new_ids(self) -> list[str]:
        return [new for _old, new in self.pairs]

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pairs": [{"old_model_id": old, "new_model_id": new} for old, new in self.pairs],
            "cutover_time": self.cutover_time,
            "provision_succession_id": self.provision_succession_id,
        }


@dataclass(frozen=True)
class Settings:
    plan: Plan
    operator_id: str
    object_store_root: Path
    provider_store_root: Path
    object_store_prefix: str
    canonical_manifest: Path
    mirror_manifest: Path
    state_index: str
    mirror_state_index: str
    refresh_lock: str
    refresh_receipt_root: Path
    receipt_root: Path
    new_rows_registry: Path | None
    pass_wait_seconds: float

    @property
    def directory(self) -> Path:
        return self.receipt_root / self.plan.succession_id

    def step_receipt(self, step: str) -> Path:
        return self.directory / f"step-{step}.json"


@dataclass(frozen=True)
class Inputs:
    """What the checks before any step established."""

    provision_receipt: dict[str, Any]  # {"path", "sha256"}
    new_rows_registry: dict[str, Any]  # {"path", "sha256"}
    new_rows: dict[str, dict[str, Any]]  # the plan's new rows, by model_id


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    """The JSON object in ``path``; ``OSError`` / ``ValueError`` when it cannot be read as one."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return payload


def receipt_header(settings: Settings, schema_version: str) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "succession_id": settings.plan.succession_id,
        "generated_at": utc_text(utc_now()),
        "operator_id": settings.operator_id,
        "host": socket.gethostname(),
        "git_commit": succession.git_commit(),
    }


def write_stamped_receipt(settings: Settings, prefix: str, receipt: Mapping[str, Any]) -> Path:
    """Exclusive-create ``<prefix>-<UTC stamp>.json``; a second one within the same second gets a counter."""

    stamp = utc_stamp(utc_now())
    for suffix in ("", *(f"-{number}" for number in range(2, 100))):
        target = settings.directory / f"{prefix}-{stamp}{suffix}.json"
        if os.path.lexists(target):
            continue
        succession.write_receipt(target, receipt)
        return target
    raise OSError(f"no free receipt name for {prefix}-{stamp} in {settings.directory}")


def completed_steps(settings: Settings) -> list[str]:
    return [step for step in STEPS if os.path.lexists(settings.step_receipt(step))]


def existing_receipts(settings: Settings) -> list[str]:
    try:
        return sorted(entry.name for entry in settings.directory.iterdir() if entry.name.endswith(".json"))
    except OSError:
        return []
