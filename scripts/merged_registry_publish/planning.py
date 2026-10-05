"""Read both manifests, do every check and predict the merged manifest; writes nothing but a private temporary file.

Part of ``scripts/node22_publish_merged_scheduler_registry.py`` (the entry point).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow
from packages.common.source_identity import normalize_source_id
from scripts.merged_registry_publish.model import (
    _NOTHING_WRITTEN,
    OBJECT_STORE_ROOT_ENV,
    PROVIDER_STORE_ROOT_ENV,
    PROVISION_APPLY_RECEIPT_NAME,
    PROVISION_RECEIPT_SCHEMA_VERSION,
    MergedRegistryPublishError,
    Operations,
    _by_model_id,
    _json_nodes,
    _load_json_object,
    _manifest_byte_cap,
    _models_sha256,
    _Plan,
    _read_destination,
    _registry_rows,
    _Settings,
    _sha256,
)
from services.orchestrator import scheduler_file_providers as providers
from services.orchestrator.scheduler_file_providers import (
    SchedulerFileProviderError,
    publish_scheduler_registry_manifest,
)
from workers.forcing_producer.direct_grid_contract import (
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)

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
