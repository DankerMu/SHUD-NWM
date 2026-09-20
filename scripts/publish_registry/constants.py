"""Constants and shared shapes of the Basins scheduler-registry publisher.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100; every
name here is verbatim from that module and stays importable from it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# v2 (#1080 round-2 R2-A1): summary now carries a required top-level
# `cutover_gate` audit block so a `--allow-uncovered-cutover` bypass leaves a
# persisted marker (versus the byte-identical v1 shape between gate-passing
# and bypass runs).  See design.md D7 sub-decision on `cutover_gate`.
SCHEMA_VERSION = "nhms.scheduler.basins_file_registry_publish.v2"

# Env name owned by scheduler_file_provider_refresh._registry_precommit_gate;
# hard-coded here so the CLI can audit it even when the refresh module is not
# imported (bootstrap path).
CUTOVER_DECLARATION_ENV_NAME = "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"

# #1104: `main()` does not populate `expected_preimage`, so this CLI has NO
# code-level protection against overwriting a provider refresh that commits
# between our snapshot and our own commit.  The protection is an operator
# prohibition documented in the runbook; every run announces it on stderr.
# Deliberately free of the substring `allow-uncovered-cutover` so it stays
# distinguishable from the bypass banner.
OPERATOR_GATE_WARNING = (
    "WARNING: manual publisher concurrency is operator-gated, not CAS-gated. "
    "Confirm nhms-scheduler-file-provider-refresh.timer is inactive/disabled "
    "AND nhms-scheduler-file-provider-refresh.service is not activating/active "
    "before publishing: systemctl --user status "
    "nhms-scheduler-file-provider-refresh.timer "
    "nhms-scheduler-file-provider-refresh.service --no-pager. "
    "See docs/runbooks/current-production-ops.md (manual publisher CLI)."
)

DEFAULT_PACKAGE_VERSION_TEMPLATE = "vbasins-{slug_id}-{content_hash}-{source_hash}"

DEFAULT_SOURCE_POLICY = {
    "forcing_source": "node27_raw_handoff",
    "allowed_cycle_hours_utc": [0, 12],
}

CALIBRATION_OVERRIDE_STAGING_DIR_NAME = "overridden-basins"

CALIBRATION_OVERRIDE_PATH_ENV_NAME = "NHMS_CALIBRATION_OVERRIDES_PATH"

REPAIR_STAGING_DIR_NAMES = ("repaired-basins", CALIBRATION_OVERRIDE_STAGING_DIR_NAME)

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# The single "declared but not applied" reason token.  Two other places pin the
# same vocabulary and must move with it: `CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS`
# in `scripts/scheduler_file_provider_refresh.py` (which imports this constant)
# and the `reason_not_applied` enum in
# `schemas/scheduler_file_provider_refresh_receipt.schema.json`, which the
# refresh receipt is validated against before it is published.
CALIBRATION_OVERRIDE_NOT_SELECTED_REASON = "basin_not_selected_for_this_run"

@dataclass(frozen=True)
class PublishContext:
    model: dict[str, Any]
    inventory_path: Path
    repair: dict[str, Any] | None = None
    source_lineage_model: dict[str, Any] | None = None
    # #1832: the declared calibration overrides that were applied to THIS
    # context's staging copy, in manifest shape.  ``None`` (never ``[]``) when
    # the basin is absent from the declaration.
    calibration_overrides: tuple[dict[str, Any], ...] | None = None

class WorkspaceBudget(Protocol):
    def ensure_directory(self, path: Path) -> None: ...

    def write_json(self, path: Path, payload: Mapping[str, Any]) -> None: ...

    def copy_tree(self, source: Path, target: Path) -> None: ...

    def copy_file(self, source: Path, target: Path) -> None: ...

    def write_bytes(self, path: Path, content: bytes) -> None: ...

    def reserve_external_write(self, path: Path, size: int) -> None: ...

    def finalize_external_write(self, path: Path, size: int) -> None: ...

    def verify_external_write(self, path: Path) -> None: ...

    def rescan(self) -> None: ...
