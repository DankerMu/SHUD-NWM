"""Public data seams for the cold-tablespace installer state machine."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY, ColdTablespaceIdentity

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas/node27_cold_tablespace_install_receipt.schema.json"


class InstallInterrupted(RuntimeError):
    """Test-only process-like interruption after durable authority advancement."""


@dataclass(frozen=True)
class InstallConfig:
    """Operator settings plus a contract-issued identity.

    Production callers use the default identity.  The production CLI never
    accepts an identity argument; imported isolated-oracle callers may pass only
    an identity returned by ``make_disposable_identity``.
    """

    enforce: bool
    receipt_path: Path
    recovery_path: Path
    head_sha: str | None
    expected_uid: int
    expected_gid: int
    expected_mode: int
    expected_device_identity: str
    install_required_bytes: int
    rollback_headroom_bytes: int
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY

    @staticmethod
    def load_schema() -> dict[str, Any]:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def example_receipt(*, outcome: str, head_sha: str) -> dict[str, Any]:
        # Local import avoids a receipt/types import cycle while preserving the
        # established public class seam used by schema tests.
        from packages.common.node27_cold_tablespace_receipt import example_receipt

        return example_receipt(outcome=outcome, head_sha=head_sha)


@dataclass
class InstallDependencies:
    """Bounded external seams consumed by the one installer state machine."""

    inspect_path: Callable[[], Mapping[str, Any]]
    inspect_health: Callable[[], Mapping[str, Any]]
    inspect_backup: Callable[..., Mapping[str, Any]]
    inspect_container: Callable[[], Mapping[str, Any]]
    docker: Callable[[tuple[str, ...]], Mapping[str, Any]]
    connect: Callable[[], Any]
    inspect_target: Callable[[], Mapping[str, Any]]
    current_bind_references: Callable[[], Sequence[str]]
    stopped_bind_references: Callable[[], Sequence[str]]
    pg_tblspc_references: Callable[[], Sequence[str]]
    catalog_dependents: Callable[[], int]
    inspect_host_path_for_rollback: Callable[[], Mapping[str, Any]]
    now: Callable[[], datetime]
    ensure_host_path: Callable[[], Mapping[str, Any]] | None = None
    remove_host_path: Callable[[], bool] | None = None
    inspect_quiescence: Callable[[], Mapping[str, Any]] | None = None
    connect_readonly: Callable[[], Any] | None = None
    inspect_named_container: Callable[[str], Mapping[str, Any]] | None = None
    remove_recovery: Callable[[Path], None] | None = None
    before_receipt_publish: Callable[[Path, Mapping[str, Any]], None] | None = None
    wait_ready: Callable[[], None] | None = None
    after_phase: Callable[[str], None] | None = None
    action_log: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)


@dataclass(frozen=True)
class InstallResult:
    outcome: str
    receipt: dict[str, Any]
    schema: dict[str, Any]
