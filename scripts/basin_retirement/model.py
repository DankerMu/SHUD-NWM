"""What the basin retirement tool shares: names, the two ways a run stops, the settings and the receipts.

Part of ``scripts/node27_retire_basin.py`` (the entry point).
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common import succession_receipt as succession

DATABASE_URL_ENV = "DATABASE_URL"
OBJECT_STORE_ROOT_ENV = "OBJECT_STORE_ROOT"
# The lifecycle call must run with the ingest environment only: either of these changes how a policy
# decision is evaluated.
FORBIDDEN_AUTH_ENVIRONMENT = ("NHMS_AUTH_MODE", "AUTH_BACKEND")
DEFAULT_ENV_FILE_KEY = "infra/env/node27-ingest.env"
CANONICAL_MANIFEST_KEY = "scheduler/registry/manifest-last.json"
DEFAULT_AUTOPIPE_WAIT_SECONDS = 1800.0

# It names a directory: one basin version per run, ``retire-<basin-version-id>/`` under the succession.
BASIN_VERSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,120}")

# The steps, in their fixed order, and the receipts each of them requires before it does anything.
STEPS = ("exclude", "supersede", "deactivate", "verify")
STEP_REQUIRES = {
    "exclude": (),
    "supersede": ("exclude",),
    "deactivate": ("exclude", "supersede"),
    "verify": ("deactivate",),
}

STEP_RECEIPT_SCHEMA_VERSION = "nhms.basin_retirement.step_receipt.v1"
FAILURE_SCHEMA_VERSION = "nhms.basin_retirement.failure_receipt.v1"
RUN_BACKUP_NAME = "hydro-run-backup.csv"
FAILURE_RECEIPT_PREFIX = "retire-failed"

# What the node-22 half of the succession leaves, and what this tool requires of it.
NODE22_PLAN_NAME = "plan.json"
NODE22_FINISH_NAME = "step-finish.json"
NODE22_KIND = "remove_basin"

RUNBOOK = "docs/runbooks/production-ops/operating-scope.md, section 7.6"
NOTHING_WRITTEN = "Nothing was written."
# Printed after a retirement and carried in every report: the parts of a retirement this tool leaves alone.
NOT_DONE_BY_THIS_TOOL = (
    "The Basins directory of the basin is not moved: move it to Basins-retired/ by hand, on both trees.",
    "The static geojson of the frontend is not filtered: the basin's features stay in it until it is rebuilt.",
    "The AUTOPIPE_EXCLUDE_BASINS entry must stay for as long as run directories of the basin exist in the "
    "object store: without it the next autopipe round registers the basin again.",
)


class RetirementRefusal(RuntimeError):
    """The run is refused before any step: nothing was written and no failure receipt is left."""


class StepFailure(RuntimeError):
    """A step did not complete.  The same command resumes after the cause is removed.

    ``details`` goes into the failure receipt beside the reason (the rows a ``deactivate`` had done).
    """

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True)
class Settings:
    succession_id: str
    basin_version_id: str
    operator_id: str
    reason: str
    env_file: Path
    receipt_root: Path
    object_store_root: Path
    # Never printed and never written to a receipt.
    database_url: str = field(repr=False)
    autopipe_wait_seconds: float = DEFAULT_AUTOPIPE_WAIT_SECONDS

    @property
    def succession_directory(self) -> Path:
        """Where the node-22 half left its plan and its step receipts."""

        return self.receipt_root / self.succession_id

    @property
    def directory(self) -> Path:
        """Everything this tool writes for one basin version, the env backup and the lock aside."""

        return self.succession_directory / f"retire-{self.basin_version_id}"

    def step_receipt(self, step: str) -> Path:
        return self.directory / f"retire-{step}.json"

    @property
    def run_backup(self) -> Path:
        return self.directory / RUN_BACKUP_NAME

    @property
    def env_backup(self) -> Path:
        # Beside the env file, not in the receipt root: it holds DATABASE_URL and infra/env/* is git-ignored.
        return self.env_file.with_name(f"{self.env_file.name}.bak-{self.succession_id}-{self.basin_version_id}")

    @property
    def lock_file(self) -> Path:
        return self.env_file.with_name(f"{self.env_file.name}.retire-lock")

    @property
    def manifest(self) -> Path:
        return self.object_store_root / CANONICAL_MANIFEST_KEY


@dataclass(frozen=True)
class Basin:
    """What the preconditions established about the basin version."""

    basin_id: str
    key: str  # the basin as AUTOPIPE_EXCLUDE_BASINS names it


def validate_basin_version_id(value: str) -> str:
    if not BASIN_VERSION_ID_PATTERN.fullmatch(value):
        raise RetirementRefusal(
            f"Invalid --basin-version-id {value!r}; expected {BASIN_VERSION_ID_PATTERN.pattern} (it names a "
            f"directory). {NOTHING_WRITTEN}"
        )
    return value


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
        "succession_id": settings.succession_id,
        "basin_version_id": settings.basin_version_id,
        "generated_at": utc_text(utc_now()),
        "operator_id": settings.operator_id,
        "reason": settings.reason,
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


def fsync_directory(directory: Path) -> None:
    """Make a new or renamed directory entry durable."""

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
