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
unless that receipt recorded the same operations, the same canonical rows and
the same merged model list.  It then backs up both manifests, publishes the
canonical one with a compare-and-swap on the bytes it read, publishes the
mirror with the same ``generated_at`` and reads both back; if the run does not
end with both published and equal, every manifest this run committed is put
back.  It never stops or starts a timer and never runs the provider refresh.

Run it on node-22 as the owner of both manifests:
``cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from packages.common import succession_receipt as succession
from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import (
    SHARED_PROVIDER_MODE,
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    provider_destination_lock,
    read_provider_snapshot,
)
from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow
from packages.common.source_identity import normalize_source_id
from services.orchestrator import scheduler_file_providers as providers
from services.orchestrator.scheduler_file_providers import (
    SchedulerFileProviderError,
    publish_scheduler_registry_manifest,
)
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)

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


# --- new rows come from a provisioned registry --------------------------------


def _load_provisioned_rows(settings: _Settings) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ``(provisioned model ids, new rows by model_id, provision apply receipt record)``."""

    provision_id = str(settings.provision_succession_id)
    receipt_file = settings.receipt_root / provision_id / PROVISION_APPLY_RECEIPT_NAME
    try:
        receipt_content = receipt_file.read_bytes()
    except OSError as error:
        raise MergedRegistryPublishError(
            f"Refused: --replace / --add take their rows from a provisioned registry and need the provision apply "
            f"receipt of succession {provision_id!r} at {receipt_file} ({error}). Run the provision step with "
            f"--apply first, or name its succession with --provision-succession-id. {_NOTHING_WRITTEN}"
        ) from error
    receipt = _load_json_object(receipt_content, what=f"provision apply receipt {receipt_file}")
    expected = {
        "schema_version": PROVISION_RECEIPT_SCHEMA_VERSION,
        "step": "provision",
        "dry_run": False,
        "outcome": "applied",
        "succession_id": provision_id,
    }
    wrong = {key: receipt.get(key) for key, value in expected.items() if receipt.get(key) != value}
    if wrong:
        raise MergedRegistryPublishError(
            f"Refused: {receipt_file} is not the provision apply receipt of succession {provision_id!r}: "
            f"found {json.dumps(wrong, sort_keys=True)}, expected "
            f"{json.dumps({key: expected[key] for key in wrong}, sort_keys=True)}. {_NOTHING_WRITTEN}"
        )
    output_registry = receipt.get("output_registry")
    output_registry = output_registry if isinstance(output_registry, Mapping) else {}
    registry_path = settings.new_rows_registry
    if registry_path is None:
        key = output_registry.get("object_store_key")
        if not key:
            raise MergedRegistryPublishError(
                f"Refused: {receipt_file} records no object_store_key for its output_registry "
                f"({output_registry.get('path')!r} is outside the object store), so the registry written by the "
                f"provision apply must be named with --new-rows-registry. {_NOTHING_WRITTEN}"
            )
        registry_path = settings.provider_store_root / str(key)
    try:
        registry_content = read_bytes_limited_no_follow(registry_path, max_bytes=_manifest_byte_cap())
    except (OSError, SafeFilesystemError) as error:
        raise MergedRegistryPublishError(
            f"Refused: cannot read the new-rows registry {registry_path} ({error}). {_NOTHING_WRITTEN}"
        ) from error
    if len(registry_content) > _manifest_byte_cap():
        raise MergedRegistryPublishError(
            f"Refused: the new-rows registry {registry_path} is larger than {_manifest_byte_cap()} bytes. "
            f"{_NOTHING_WRITTEN}"
        )
    registry_sha256 = _sha256(registry_content)
    recorded_sha256 = str(output_registry.get("sha256") or "").removeprefix("sha256:")
    if registry_sha256 != recorded_sha256:
        raise MergedRegistryPublishError(
            f"Refused: the new-rows registry {registry_path} has sha256 {registry_sha256}, but {receipt_file} "
            f"recorded {recorded_sha256 or None} for the registry its apply wrote. It is not that file, or it "
            f"changed since. {_NOTHING_WRITTEN}"
        )
    what = f"the new-rows registry {registry_path}"
    payload = _load_json_object(registry_content, what=what)
    # Read as plain JSON so the rows stay verbatim; only the embedded checksum
    # is verified (with the publisher's own canonical serialisation), not freshness.
    if not providers._checksum_matches(payload.get("checksum"), providers._payload_checksum(payload)):
        raise MergedRegistryPublishError(
            f"Refused: {what} does not match its embedded checksum. {_NOTHING_WRITTEN}"
        )
    new_rows = _by_model_id(_registry_rows(payload, what=what), what=what)
    models = receipt.get("models")
    provisioned = [str(model.get("model_id")) for model in models if isinstance(model, Mapping)] if models else []
    if not provisioned:
        raise MergedRegistryPublishError(f"Refused: {receipt_file} lists no models. {_NOTHING_WRITTEN}")
    record = {"path": str(receipt_file), "sha256": _sha256(receipt_content)}
    return provisioned, new_rows, record


# --- checks ------------------------------------------------------------------


def _row_source(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(source, None)`` of a direct-grid row or ``(None, why it is refused)``."""

    profile = row.get("resource_profile")
    if not isinstance(profile, Mapping):
        return None, "has no resource_profile"
    section = profile.get("direct_grid_forcing")
    if profile.get("forcing_mapping_mode") != "direct_grid" or not isinstance(section, Mapping):
        return None, "is not a direct-grid row"
    raw_source = profile.get("direct_grid_source_id")
    if not raw_source:
        return None, "has no resource_profile.direct_grid_source_id"
    try:
        source = normalize_source_id(str(raw_source))
    except ValueError:
        return None, f"has an unknown resource_profile.direct_grid_source_id {raw_source!r}"
    try:
        # The same authoritative parse dispatch and readiness use for a row's source scope.
        contract = load_forcing_mapping_contract_from_manifest(
            {"forcing_mapping_mode": "direct_grid", "direct_grid_forcing": section}
        )
    except DirectGridContractError as error:
        return None, f"has an invalid direct_grid_forcing contract ({error.field}: {error})"
    applicable = contract.applicable_source_ids if contract is not None else ()
    if source not in applicable:
        return None, (
            f"has direct_grid_source_id {source!r} outside its contract's applicable_source_ids {list(applicable)}"
        )
    return source, None


def _sources_by_model_id(rows: Sequence[Mapping[str, Any]], *, what: str) -> dict[str, str]:
    sources: dict[str, str] = {}
    refused: list[str] = []
    for row in rows:
        source, why = _row_source(row)
        if source is None:
            refused.append(f"{row['model_id']} {why}")
        else:
            sources[str(row["model_id"])] = source
    if refused:
        raise MergedRegistryPublishError(f"Refused: {what}: {'; '.join(refused)}. {_NOTHING_WRITTEN}")
    return sources


def _check_operations(operations: Operations, canonical_ids: Sequence[str]) -> None:
    if not (operations.replace or operations.add or operations.remove):
        raise MergedRegistryPublishError("Refused: at least one of --replace, --add or --remove is required.")
    named = operations.new_model_ids + operations.retired_model_ids
    repeated = sorted({model_id for model_id in named if named.count(model_id) > 1})
    if repeated:
        raise MergedRegistryPublishError(
            f"Refused: a model_id may appear in one operation only; named more than once: {repeated}. "
            f"{_NOTHING_WRITTEN}"
        )
    absent = [model_id for model_id in operations.retired_model_ids if model_id not in canonical_ids]
    if absent:
        raise MergedRegistryPublishError(
            f"Refused: model_id to replace or remove is not in the canonical manifest: {absent}. {_NOTHING_WRITTEN}"
        )
    present = [model_id for model_id in operations.new_model_ids if model_id in canonical_ids]
    if present:
        raise MergedRegistryPublishError(
            f"Refused: new model_id is already in the canonical manifest: {present}. {_NOTHING_WRITTEN}"
        )


def _check_provisioned(
    operations: Operations,
    canonical_ids: Sequence[str],
    provisioned: Sequence[str],
    new_rows: Mapping[str, Any],
    receipt_path: str,
) -> list[str]:
    """Refuse a new id the provision did not produce; return ``already_published_model_ids``."""

    new_ids = operations.new_model_ids
    unprovisioned = [model_id for model_id in new_ids if model_id not in provisioned]
    if unprovisioned:
        raise MergedRegistryPublishError(
            f"Refused: new model_id is not in models[] of the provision apply receipt {receipt_path}: "
            f"{unprovisioned}. A row only enters the manifest from a provisioned registry. {_NOTHING_WRITTEN}"
        )
    missing_rows = [model_id for model_id in new_ids if model_id not in new_rows]
    if missing_rows:
        raise MergedRegistryPublishError(
            f"Refused: new model_id is not in the new-rows registry: {missing_rows}. {_NOTHING_WRITTEN}"
        )
    unaccounted = [
        model_id for model_id in provisioned if model_id not in new_ids and model_id not in canonical_ids
    ]
    if unaccounted:
        raise MergedRegistryPublishError(
            f"Refused: the provision apply receipt {receipt_path} lists model_id that no operation introduces and "
            f"that is not in the canonical manifest: {unaccounted}. Name each with --replace or --add; a "
            f"provisioned row must not be left unpublished by accident. {_NOTHING_WRITTEN}"
        )
    return [model_id for model_id in provisioned if model_id not in new_ids]


def _merge(
    canonical_rows: Sequence[dict[str, Any]],
    new_rows: Mapping[str, dict[str, Any]],
    operations: Operations,
) -> list[dict[str, Any]]:
    """Canonical order kept; a replaced row is substituted in place; added rows are appended."""

    successor = dict(operations.replace)
    merged: list[dict[str, Any]] = []
    for row in canonical_rows:
        model_id = str(row["model_id"])
        if model_id in operations.remove:
            continue
        merged.append(new_rows[successor[model_id]] if model_id in successor else row)
    merged.extend(new_rows[model_id] for model_id in operations.add)
    return merged


def _check_replacements(
    operations: Operations,
    canonical: Mapping[str, dict[str, Any]],
    new_rows: Mapping[str, dict[str, Any]],
    sources: Mapping[str, str],
) -> list[dict[str, Any]]:
    replaced: list[dict[str, Any]] = []
    moved: list[str] = []
    for old_id, new_id in operations.replace:
        old, new = canonical[old_id], new_rows[new_id]
        if str(old["basin_id"]) != str(new["basin_id"]) or sources[old_id] != sources[new_id]:
            moved.append(
                f"{old_id} ({old['basin_id']}, {sources[old_id]}) -> {new_id} ({new['basin_id']}, {sources[new_id]})"
            )
        replaced.append(
            {
                "old_model_id": old_id,
                "new_model_id": new_id,
                "basin_id": str(old["basin_id"]),
                "source_id": sources[old_id],
                "old_basin_version_id": old.get("basin_version_id"),
                "new_basin_version_id": new.get("basin_version_id"),
            }
        )
    if moved:
        raise MergedRegistryPublishError(
            f"Refused: a replace must keep basin_id and source: {'; '.join(moved)}. {_NOTHING_WRITTEN}"
        )
    return replaced


def _check_merged(
    merged: Sequence[dict[str, Any]],
    sources: Mapping[str, str],
    *,
    sources_before: Sequence[str],
    expected_count: int,
) -> None:
    _by_model_id(merged, what="the merged manifest")
    by_basin: dict[str, list[str]] = {}
    for row in merged:
        by_basin.setdefault(str(row["basin_id"]), []).append(sources[str(row["model_id"])])
    uneven = [
        f"{basin_id} has {sorted(found)}"
        for basin_id, found in sorted(by_basin.items())
        if sorted(found) != sorted(sources_before)
    ]
    if uneven:
        raise MergedRegistryPublishError(
            f"Refused: in the merged manifest every basin_id must have exactly one row for each of the sources "
            f"{sorted(sources_before)} of the canonical manifest: {'; '.join(uneven)}. Add or remove every source "
            f"of a basin together. {_NOTHING_WRITTEN}"
        )
    if len(merged) != expected_count:
        raise MergedRegistryPublishError(
            f"Refused: the merged manifest has {len(merged)} rows, expected {expected_count} "
            f"(before + adds - removes). {_NOTHING_WRITTEN}"
        )


def _publisher_refusal(error: SchedulerFileProviderError, rows: Sequence[Mapping[str, Any]], where: str) -> str:
    match = re.search(r"models\[(\d+)\]", error.field)
    model_id = rows[int(match.group(1))].get("model_id") if match else error.evidence.get("model_id")
    named = f" model_id={model_id}" if model_id else ""
    evidence = f" {json.dumps(error.evidence, sort_keys=True, default=str)}" if error.evidence else ""
    return (
        f"Refused: the publisher rejects the rows {where}: {error.reason} at {error.field}{named}{evidence}. "
        f"{_NOTHING_WRITTEN}"
    )


def _validate_with_publisher(
    settings: _Settings,
    merged: Sequence[Mapping[str, Any]],
    new_rows: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    """Return ``(bytes, JSON nodes)`` of the merged manifest as the real publisher writes it.

    The publisher is run on a file in a private temporary directory, so its own
    validation, byte bound and node bound decide; the file, its lock and the
    directory are deleted.  Nothing is created in either manifest directory.
    """

    base = Path(tempfile.gettempdir()).resolve()
    for manifest in (settings.canonical_path, settings.mirror_path):
        directory = manifest.parent.resolve()
        if base == directory or base.is_relative_to(directory):
            raise MergedRegistryPublishError(
                f"Refused: the temporary directory {base} is inside the manifest directory {directory}; a "
                f"dry-run must not create files there. Point TMPDIR elsewhere. {_NOTHING_WRITTEN}"
            )
    # Resolved: the atomic writer refuses a destination below a symlinked directory.
    scratch = Path(tempfile.mkdtemp(prefix="nhms-publish-merged-registry-", dir=base))
    common: dict[str, Any] = {
        "object_store_prefix": settings.object_store_prefix,
        "require_direct_grid": True,
        "generated_at": settings.clock(),
    }
    try:
        target = scratch / "merged-manifest.json"
        try:
            publish_scheduler_registry_manifest(
                merged, target, object_store_root=settings.object_store_root, **common
            )
        except SchedulerFileProviderError as error:
            where = f"of the merged manifest (packages resolved under {OBJECT_STORE_ROOT_ENV})"
            raise MergedRegistryPublishError(_publisher_refusal(error, merged, where)) from error
        content = target.read_bytes()
        if new_rows:
            # The publisher verifies each manifest_uri / package_checksum under
            # the root it is given; the new packages must also be on the shared store.
            try:
                publish_scheduler_registry_manifest(
                    new_rows, scratch / "new-rows.json", object_store_root=settings.provider_store_root, **common
                )
            except SchedulerFileProviderError as error:
                where = f"being introduced (packages resolved under {PROVIDER_STORE_ROOT_ENV})"
                raise MergedRegistryPublishError(_publisher_refusal(error, new_rows, where)) from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return len(content), _json_nodes(json.loads(content))


def _plan(settings: _Settings) -> _Plan:
    """Read both manifests and do every check; writes nothing outside a private temporary directory."""

    canonical = _read_destination("canonical", settings.canonical_path, settings.provider_store_root)
    mirror = _read_destination("mirror", settings.mirror_path, settings.object_store_root)
    if canonical.sha256 != mirror.sha256:
        raise MergedRegistryPublishError(
            f"Refused: the canonical manifest {canonical.path} (sha256 {canonical.sha256}) and the worker mirror "
            f"{mirror.path} (sha256 {mirror.sha256}) differ before the change. Run the provider refresh first so "
            f"that both hold one generation. {_NOTHING_WRITTEN}"
        )
    what = f"the canonical manifest {canonical.path}"
    canonical_rows = _registry_rows(_load_json_object(canonical.content, what=what), what=what)
    canonical_by_id = _by_model_id(canonical_rows, what=what)
    operations = settings.operations
    _check_operations(operations, list(canonical_by_id))

    new_rows: dict[str, dict[str, Any]] = {}
    already_published: list[str] = []
    provision_record: dict[str, Any] | None = None
    if operations.new_model_ids:
        provisioned, new_rows, provision_record = _load_provisioned_rows(settings)
        already_published = _check_provisioned(
            operations, list(canonical_by_id), provisioned, new_rows, provision_record["path"]
        )
    introduced = [new_rows[model_id] for model_id in operations.new_model_ids]

    sources = _sources_by_model_id(canonical_rows, what=f"row of {what}")
    sources_before = sorted(set(sources.values()))
    sources.update(_sources_by_model_id(introduced, what="row of the new-rows registry"))
    replaced = _check_replacements(operations, canonical_by_id, new_rows, sources)
    merged = _merge(canonical_rows, new_rows, operations)
    _check_merged(
        merged,
        sources,
        sources_before=sources_before,
        expected_count=len(canonical_rows) + len(operations.add) - len(operations.remove),
    )
    manifest_bytes, manifest_json_nodes = _validate_with_publisher(settings, merged, introduced)
    return _Plan(
        canonical=canonical,
        mirror=mirror,
        merged_rows=merged,
        row_count_before=len(canonical_rows),
        canonical_models_sha256=_models_sha256(canonical_rows),
        introduced_model_ids=operations.new_model_ids,
        removed_model_ids=operations.retired_model_ids,
        already_published_model_ids=already_published,
        replaced=replaced,
        provision_apply_receipt=provision_record,
        manifest_bytes=manifest_bytes,
        manifest_json_nodes=manifest_json_nodes,
    )


# --- receipts ----------------------------------------------------------------


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _receipt(
    settings: _Settings,
    plan: _Plan,
    *,
    dry_run: bool,
    outcome: str,
    sha256_after: Mapping[str, str | None] | None = None,
    backups: Mapping[str, Path] | None = None,
    manifest_generated_at: str | None = None,
    manifest_bytes: int | None = None,
    dry_run_receipt: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    def destination(item: _Destination) -> dict[str, Any]:
        return {
            "path": str(item.path),
            "sha256_before": item.sha256,
            "sha256_after": (sha256_after or {}).get(item.name),
            "backup_path": str(backups[item.name]) if backups else None,
        }

    size = plan.manifest_bytes if manifest_bytes is None else manifest_bytes
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "succession_id": settings.succession_id,
        "provision_succession_id": settings.provision_succession_id,
        "step": RECEIPT_STEP,
        "dry_run": dry_run,
        "outcome": outcome,
        "generated_at": _utc_text(settings.clock()),
        "operator_id": settings.operator_id,
        "host": socket.gethostname(),
        "git_commit": succession.git_commit(),
        "operations": settings.operations.record(),
        "row_count_before": plan.row_count_before,
        "row_count_after": len(plan.merged_rows),
        "introduced_model_ids": plan.introduced_model_ids,
        "removed_model_ids": plan.removed_model_ids,
        "already_published_model_ids": plan.already_published_model_ids,
        "replaced": plan.replaced,
        "merged_model_ids": plan.merged_model_ids,
        "canonical": destination(plan.canonical),
        "mirror": destination(plan.mirror),
        "canonical_models_sha256_before": plan.canonical_models_sha256,
        "manifest_generated_at": manifest_generated_at,
        "manifest_bytes": size,
        "manifest_bytes_limit": _manifest_byte_cap(),
        "manifest_bytes_remaining": _manifest_byte_cap() - size,
        "manifest_json_nodes": plan.manifest_json_nodes,
        "manifest_json_nodes_limit": _manifest_node_cap(),
        "manifest_json_nodes_remaining": _manifest_node_cap() - plan.manifest_json_nodes,
    }
    if plan.provision_apply_receipt is not None:
        receipt["provision_apply_receipt"] = plan.provision_apply_receipt
    if dry_run_receipt is not None:
        receipt["dry_run_receipt"] = dict(dry_run_receipt)
    if reason is not None:
        receipt["reason"] = reason
    return receipt


def _load_dry_run_receipt(settings: _Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(dry-run receipt, its path record)`` or refuse naming the expected path."""

    succession_id = str(settings.succession_id)
    path = settings.receipt_root / succession_id / DRY_RUN_RECEIPT_NAME
    try:
        content = path.read_bytes()
    except OSError as error:
        raise MergedRegistryPublishError(
            f"--apply requires the dry-run receipt of succession {succession_id!r} at {path} ({error}). "
            f"Run the same command without --apply and with the same --succession-id first. {_NOTHING_WRITTEN}"
        ) from error
    payload = _load_json_object(content, what=f"dry-run receipt {path}")
    expected = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "step": RECEIPT_STEP,
        "dry_run": True,
        "outcome": "planned",
        "succession_id": succession_id,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise MergedRegistryPublishError(
            f"{path} is not a publish dry-run receipt of succession {succession_id!r}. {_NOTHING_WRITTEN}"
        )
    return payload, {"path": str(path), "sha256": _sha256(content)}


def _require_same_as_dry_run(dry_run: Mapping[str, Any], dry_run_path: str, field: str, current: Any) -> None:
    predicted = dry_run.get(field)
    if predicted != current:
        raise MergedRegistryPublishError(
            f"--apply refused: {field} differs from the dry-run receipt {dry_run_path}. "
            f"dry-run: {json.dumps(predicted, sort_keys=True)}; this run: {json.dumps(current, sort_keys=True)}. "
            f"A changed plan, or a canonical manifest whose models changed since the dry-run, needs a new "
            f"--succession-id and its own dry-run (with --provision-succession-id naming the unchanged "
            f"provision). {_NOTHING_WRITTEN}"
        )


# --- apply -------------------------------------------------------------------


def _write_backup(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, SHARED_PROVIDER_MODE), "wb") as handle:
        os.fchmod(handle.fileno(), SHARED_PROVIDER_MODE)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _current_sha256(destination: _Destination) -> str | None:
    try:
        return capture_provider_preimage(
            destination.path, containment_root=destination.containment_root, max_bytes=_manifest_byte_cap()
        ).sha256
    except _PROVIDER_ERRORS:
        return None


def _put_back(destination: _Destination, committed: ProviderPreimage | None) -> tuple[str | None, str]:
    """Return ``(current sha256, state)`` with state ``unchanged`` / ``restored`` or what went wrong.

    A destination this run committed is restored against the preimage its
    publish committed, never against a re-captured one: if anybody rewrote it
    since, the compare-and-swap fails and those bytes stay.
    """

    current = _current_sha256(destination)
    if current == destination.sha256:
        return current, "unchanged"
    if committed is None:
        return current, "changed, but not by a commit this run observed; left as it is"
    try:
        atomic_replace_provider_bytes(
            destination.path,
            destination.content,
            containment_root=destination.containment_root,
            max_bytes=_manifest_byte_cap(),
            expected_preimage=committed,
        )
    except _PROVIDER_ERRORS as error:
        reason = getattr(error, "reason", None) or str(error)
        return _current_sha256(destination), f"committed by this run, could not be restored ({reason}); left as it is"
    current = _current_sha256(destination)
    if current != destination.sha256:
        return current, "restored, but the read-back differs from the bytes read"
    return current, "restored"


def _failure(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    return f"{type(error).__name__}: {reason or error}"


def _apply(settings: _Settings) -> dict[str, Any]:
    """Publish both manifests; the caller holds the provider refresh lock."""

    dry_run, dry_run_record = _load_dry_run_receipt(settings)
    dry_run_path = dry_run_record["path"]
    _require_same_as_dry_run(dry_run, dry_run_path, "operations", settings.operations.record())
    succession_dir = settings.receipt_root / str(settings.succession_id)
    apply_target = succession_dir / APPLY_RECEIPT_NAME
    # The receipt directory is checked before any backup or write.
    succession.prepare_receipt_target(apply_target, receipt_root=settings.receipt_root)

    plan = _plan(settings)
    _require_same_as_dry_run(dry_run, dry_run_path, "canonical_models_sha256_before", plan.canonical_models_sha256)
    _require_same_as_dry_run(dry_run, dry_run_path, "merged_model_ids", plan.merged_model_ids)

    started = settings.clock()
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    destinations = (plan.canonical, plan.mirror)
    backups = {
        item.name: item.path.with_name(f"{item.path.name}.bak-{settings.succession_id}-{stamp}")
        for item in destinations
    }
    failed_target = succession_dir / f"publish-apply-failed-{stamp}.json"
    taken = [str(path) for path in (*backups.values(), failed_target) if os.path.lexists(path)]
    if taken:
        raise MergedRegistryPublishError(
            f"--apply refused: {taken} already exist(s) for the stamp {stamp} of this second; a backup or a "
            f"receipt is never overwritten. Run the same command again. {_NOTHING_WRITTEN}"
        )

    # From here on every attempt that does not publish leaves a failed-apply receipt.
    committed: dict[str, ProviderPreimage] = {}

    def observer(name: str) -> Callable[[ProviderPreimage], None]:
        def observe(value: ProviderPreimage) -> None:
            committed[name] = ProviderPreimage.from_value(value)

        return observe

    manifest_generated_at: str | None = None
    try:
        for item in destinations:
            # The very bytes whose preimage the compare-and-swap below is made against.
            _write_backup(backups[item.name], item.content)
        for item in destinations:
            published = publish_scheduler_registry_manifest(
                plan.merged_rows,
                item.path,
                object_store_root=settings.object_store_root,
                object_store_prefix=settings.object_store_prefix,
                require_direct_grid=True,
                generated_at=started,
                expected_preimage=item.preimage,
                commit_observer=observer(item.name),
            )
            manifest_generated_at = str(published["generated_at"])
        after = {
            item.name: capture_provider_preimage(
                item.path, containment_root=item.containment_root, max_bytes=_manifest_byte_cap()
            )
            for item in destinations
        }
        if not all(
            item.name in committed and after[item.name].sha256 == committed[item.name].sha256 for item in destinations
        ) or after["canonical"].sha256 != after["mirror"].sha256:
            raise MergedRegistryPublishError(
                "the read-back after both publishes differs: canonical sha256 "
                f"{after['canonical'].sha256}, mirror sha256 {after['mirror'].sha256}"
            )
    except BaseException as error:
        # Also an interrupt: a canonical manifest published without its mirror must not be left behind.
        _fail_apply(
            settings,
            plan,
            error,
            committed=committed,
            backups=backups,
            failed_target=failed_target,
            dry_run_record=dry_run_record,
        )

    sha256_after = {name: preimage.sha256 for name, preimage in after.items()}
    receipt = _receipt(
        settings,
        plan,
        dry_run=False,
        outcome="published",
        sha256_after=sha256_after,
        backups=backups,
        manifest_generated_at=manifest_generated_at,
        manifest_bytes=after["canonical"].size,
        dry_run_receipt=dry_run_record,
    )
    try:
        succession.write_receipt(apply_target, receipt)
    except OSError as error:
        # Nothing is rolled back: the publish is complete, only its receipt is missing.
        raise MergedRegistryPublishError(
            f"The publish is complete but unreceipted: both manifests were published and read back equal "
            f"(sha256 {sha256_after['canonical']}, manifest_generated_at {manifest_generated_at}), but the apply "
            f"receipt {apply_target} could NOT be written: {error}. Nothing was rolled back. Backups of the "
            f"previous bytes: {backups['canonical']} and {backups['mirror']}. Do not re-run this succession; "
            "record these values by hand and continue with the provider refresh."
        ) from error
    print(f"Publish receipt written: {apply_target}", file=sys.stderr)
    return receipt


def _fail_apply(
    settings: _Settings,
    plan: _Plan,
    error: BaseException,
    *,
    committed: Mapping[str, ProviderPreimage],
    backups: Mapping[str, Path],
    failed_target: Path,
    dry_run_record: Mapping[str, Any],
) -> NoReturn:
    """Put back what this run committed, write the failed-apply receipt and raise the outcome."""

    states: dict[str, str] = {}
    sha256_after: dict[str, str | None] = {}
    # Mirror first: the reverse of the publish order.
    for item in (plan.mirror, plan.canonical):
        sha256_after[item.name], states[item.name] = _put_back(item, committed.get(item.name))
    back = all(state in {"unchanged", "restored"} for state in states.values())
    conflict = getattr(error, "reason", None) == "provider_preimage_changed"
    if back:
        outcome = "rolled_back" if committed else "refused"
    elif conflict and not committed:
        # The compare-and-swap refused the first write: this run changed neither manifest.
        outcome = "refused"
    else:
        outcome = "inconsistent"
    summary = (
        f"canonical {plan.canonical.path}: {states['canonical']} (sha256 now {sha256_after['canonical']}, "
        f"read {plan.canonical.sha256}); mirror {plan.mirror.path}: {states['mirror']} (sha256 now "
        f"{sha256_after['mirror']}, read {plan.mirror.sha256})"
    )
    reason = f"{_failure(error)}. {summary}"
    receipt = _receipt(
        settings,
        plan,
        dry_run=False,
        outcome=outcome,
        sha256_after=sha256_after,
        backups=backups,
        dry_run_receipt=dry_run_record,
        reason=reason,
    )
    try:
        succession.write_receipt(failed_target, receipt)
        receipted = f"Receipt: {failed_target}."
    except OSError as receipt_error:
        receipted = f"The failed-apply receipt {failed_target} could NOT be written: {receipt_error}."
    if outcome == "refused":
        consequence = (
            "This run changed neither manifest"
            + (" (the canonical manifest was changed by another writer since it was read)" if conflict else "")
            + f". The same --succession-id can be retried. Backups: {backups['canonical']}, {backups['mirror']}."
        )
    elif outcome == "rolled_back":
        consequence = (
            "Every manifest this run committed was restored to the bytes read; both are back at the previous "
            f"generation. The same --succession-id can be retried. Backups: {backups['canonical']}, "
            f"{backups['mirror']}."
        )
    else:
        consequence = (
            "The two manifests may now DIFFER, and the workers refuse to submit while they do. Two ways out: "
            f"(1) restore both from this run's backups (copy {backups['canonical']} over {plan.canonical.path} "
            f"and {backups['mirror']} over {plan.mirror.path}), or (2) publish the mirror: run the provider "
            "refresh, which republishes the canonical rows to both. Compare the two sha256 before and after."
        )
    raise MergedRegistryPublishError(
        f"--apply {outcome}: {reason}. {consequence} {receipted}",
        receipt=receipt,
        receipt_path=failed_target,
    ) from error


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
    must have recorded the same operations, canonical rows and merged model
    list, and the provider refresh lock is held, without blocking, for the
    whole run.  Every refusal and every failed apply raises
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
    receipt: dict[str, Any] | None = None
    try:
        with provider_destination_lock(Path(refresh_lock), blocking=False):
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
