"""What the merged-registry publish tool shares: constants, the refusal, the plan types and input readers.

Part of ``scripts/node22_publish_merged_scheduler_registry.py`` (the entry point).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import (
    SHARED_PROVIDER_MODE,
    ProviderAtomicError,
    ProviderPreimage,
    read_provider_snapshot,
)
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator import scheduler_file_providers as providers
from services.orchestrator.scheduler_file_providers import SchedulerFileProviderError

RECEIPT_SCHEMA_VERSION = "nhms.model_succession.publish_receipt.v1"
RECEIPT_STEP = "publish"
DRY_RUN_RECEIPT_NAME = "publish-dry-run.json"
APPLY_RECEIPT_NAME = "publish-apply.json"
PROVISION_RECEIPT_SCHEMA_VERSION = "nhms.model_succession.provision_receipt.v1"
PROVISION_APPLY_RECEIPT_NAME = "provision-apply.json"

CANONICAL_MANIFEST_ENV = "NHMS_SCHEDULER_REGISTRY_MANIFEST"
MIRROR_MANIFEST_ENV = "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST"
OBJECT_STORE_ROOT_ENV = "OBJECT_STORE_ROOT"
PROVIDER_STORE_ROOT_ENV = "NHMS_SCHEDULER_PROVIDER_STORE_ROOT"
OBJECT_STORE_PREFIX_ENV = "OBJECT_STORE_PREFIX"
REFRESH_LOCK_ENV = "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK"

DRY_RUN_NOTICE = (
    "DRY-RUN (no --apply): neither manifest is written, no backup is made and no lock is taken"
    " (only this run's receipt, when --succession-id is given). An --apply backs up both manifests,"
    " publishes the canonical one and then the worker mirror, and puts back what it committed when the"
    " two do not end equal."
)

_NOTHING_WRITTEN = "Nothing was written."
_PROVIDER_ERRORS = (OSError, SafeFilesystemError, ProviderAtomicError, SchedulerFileProviderError)


class MergedRegistryPublishError(RuntimeError):
    """A refusal or a failed apply; ``receipt`` is the failed-apply receipt when one was written."""

    def __init__(
        self,
        message: str,
        *,
        receipt: Mapping[str, Any] | None = None,
        receipt_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = dict(receipt) if receipt is not None else None
        self.receipt_path = receipt_path


@dataclass(frozen=True)
class Operations:
    """What the run changes; ``replace`` pairs are ``(old_model_id, new_model_id)``."""

    replace: tuple[tuple[str, str], ...] = ()
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    @property
    def new_model_ids(self) -> list[str]:
        return [new for _old, new in self.replace] + list(self.add)

    @property
    def retired_model_ids(self) -> list[str]:
        return [old for old, _new in self.replace] + list(self.remove)

    def record(self) -> dict[str, Any]:
        return {
            "replace": [{"old_model_id": old, "new_model_id": new} for old, new in self.replace],
            "add": list(self.add),
            "remove": list(self.remove),
        }


@dataclass(frozen=True)
class _Settings:
    canonical_path: Path
    mirror_path: Path
    object_store_root: Path
    provider_store_root: Path
    object_store_prefix: str
    operations: Operations
    operator_id: str
    succession_id: str | None
    provision_succession_id: str | None
    receipt_root: Path
    new_rows_registry: Path | None
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class _Destination:
    """One manifest as read: the bytes, and the preimage a compare-and-swap is made against."""

    name: str
    path: Path
    containment_root: Path
    content: bytes
    preimage: ProviderPreimage

    @property
    def sha256(self) -> str:
        return str(self.preimage.sha256)


@dataclass(frozen=True)
class _Plan:
    canonical: _Destination
    mirror: _Destination
    merged_rows: list[dict[str, Any]]
    row_count_before: int
    canonical_models_sha256: str
    introduced_model_ids: list[str]
    removed_model_ids: list[str]
    already_published_model_ids: list[str]
    replaced: list[dict[str, Any]]
    provision_apply_receipt: dict[str, Any] | None
    manifest_bytes: int
    manifest_json_nodes: int

    @property
    def merged_model_ids(self) -> list[str]:
        return [str(row["model_id"]) for row in self.merged_rows]


# --- inputs ------------------------------------------------------------------


def refuse_database_environment() -> None:
    present = sorted(name for name in LIBPQ_CONNECTION_ENV_KEYS if os.environ.get(name) not in (None, ""))
    if present:
        raise MergedRegistryPublishError(
            f"Refused: this tool is DB-free and must not run with a database variable set: {', '.join(present)}. "
            f"Unset them (load only the provider-refresh environment). {_NOTHING_WRITTEN}"
        )


def _manifest_byte_cap() -> int:
    # Read at call time: the publisher bounds its own write by the same module attribute.
    return int(providers.MAX_REGISTRY_MANIFEST_BYTES)


def _manifest_node_cap() -> int:
    return int(providers.MAX_REGISTRY_MANIFEST_JSON_NODES)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _models_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """sha256 of the canonical JSON of a ``models`` array (``generated_at`` is not part of it)."""

    return _sha256(json.dumps(list(rows), sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _json_nodes(value: Any) -> int:
    """Count JSON value nodes as the registry reader does: keys are not nodes, every value is."""

    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return visited


def _contained_absolute(path: Path, root: Path, *, what: str, root_name: str) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise MergedRegistryPublishError(f"Refused: {what} {path} and {root_name} {root} must be absolute paths.")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise MergedRegistryPublishError(
            f"Refused: {what} {path} is not under {root_name} {root}. {_NOTHING_WRITTEN}"
        ) from error


def _read_destination(name: str, path: Path, containment_root: Path) -> _Destination:
    try:
        content, preimage = read_provider_snapshot(
            path, containment_root=containment_root, max_bytes=_manifest_byte_cap()
        )
        parent = os.stat(path.parent)
    except (OSError, SafeFilesystemError, ProviderAtomicError) as error:
        reason = getattr(error, "reason", None) or str(error)
        raise MergedRegistryPublishError(
            f"Refused: cannot read the {name} manifest {path} ({reason}). {_NOTHING_WRITTEN}"
        ) from error
    # What the atomic provider writer would refuse at publish time is refused
    # here, before either manifest is touched.
    if preimage.uid != os.geteuid() or preimage.mode != SHARED_PROVIDER_MODE:
        raise MergedRegistryPublishError(
            f"Refused: the {name} manifest {path} must be owned by the user running this tool and have mode "
            f"{SHARED_PROVIDER_MODE:04o} (found uid={preimage.uid}, mode={preimage.mode or 0:04o}); the publisher "
            f"would refuse it. {_NOTHING_WRITTEN}"
        )
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise MergedRegistryPublishError(
            f"Refused: the directory of the {name} manifest, {path.parent}, must be owned by the user running "
            f"this tool and not be writable by group or other (found uid={parent.st_uid}, "
            f"mode={stat.S_IMODE(parent.st_mode):04o}); the publisher would refuse its lock. {_NOTHING_WRITTEN}"
        )
    return _Destination(name=name, path=path, containment_root=containment_root, content=content, preimage=preimage)


def _load_json_object(content: bytes, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MergedRegistryPublishError(f"Refused: {what} is not valid JSON: {error}. {_NOTHING_WRITTEN}") from error
    if not isinstance(payload, dict):
        raise MergedRegistryPublishError(f"Refused: {what} must be a JSON object. {_NOTHING_WRITTEN}")
    return payload


def _registry_rows(payload: Mapping[str, Any], *, what: str) -> list[dict[str, Any]]:
    """Return the ``models`` of a registry file as the JSON objects it holds."""

    if payload.get("schema_version") != providers.REGISTRY_MANIFEST_SCHEMA_VERSION:
        raise MergedRegistryPublishError(
            f"Refused: {what} has schema_version {payload.get('schema_version')!r}, expected "
            f"{providers.REGISTRY_MANIFEST_SCHEMA_VERSION!r}. {_NOTHING_WRITTEN}"
        )
    rows = payload.get("models")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise MergedRegistryPublishError(f"Refused: {what} must hold a models list of objects. {_NOTHING_WRITTEN}")
    unnamed = [index for index, row in enumerate(rows) if not row.get("model_id") or not row.get("basin_id")]
    if unnamed:
        raise MergedRegistryPublishError(
            f"Refused: {what} has rows without model_id or basin_id at index {unnamed}. {_NOTHING_WRITTEN}"
        )
    return rows


def _by_model_id(rows: Sequence[dict[str, Any]], *, what: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        model_id = str(row["model_id"])
        if model_id in indexed:
            duplicates.append(model_id)
        indexed[model_id] = row
    if duplicates:
        raise MergedRegistryPublishError(
            f"Refused: {what} lists a model_id more than once: {sorted(set(duplicates))}. {_NOTHING_WRITTEN}"
        )
    return indexed
