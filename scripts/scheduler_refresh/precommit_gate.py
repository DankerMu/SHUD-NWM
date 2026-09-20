"""Registry classification and the #1080 registry cutover precommit gate.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from scripts.publish_scheduler_file_registry import SchedulerRegistryPublishError
from scripts.scheduler_refresh.config import _enforce_workspace_bounds
from scripts.scheduler_refresh.constants import (
    MAX_COLLECTION_ITEMS,
    MAX_ORPHAN_EVIDENCE,
    MAX_ORPHANS,
    MAX_STRING_LENGTH,
    RefreshError,
)
from scripts.scheduler_refresh.cutover_declaration import (
    _load_cutover_declaration,
    _prospective_registry_content,
    _prospective_registry_generation,
)
from scripts.scheduler_refresh.identity import _rows_have_identical_identity


@dataclass
class _RegistryClassification:
    """In-flight classification decision produced by ``_registry_precommit_gate``.

    Populated before the gate raises so the exception handler can still emit
    ``registry_classification`` on a refusal receipt.
    """

    previous_registry_sha256: str | None = None
    new_registry_sha256: str | None = None
    # R2-N1: carry the exact previous/prospective row counts on the
    # classification so ``_enforce_registry_classification_reconciliation``
    # can enforce EQUALITY (not just non-negative bounds) against the
    # partition counts.  A validated on-disk receipt then catches a stale
    # classification whose totals silently drop or gain a row.
    previous_model_count: int | None = None
    prospective_model_count: int = 0
    # #1140: which partition the classifier actually ran.  ``_classify_registry``
    # keys its lenient id-only early return on ``dry_run``, and the receipt
    # validator has to select the same branch — reading the terminal outcome
    # instead misroutes every dry_run that fails AFTER the gate.
    mode: str = "full"
    # Round-1 F-A: the prospective generation this classification bound to.  It
    # is what an operator must copy into a cutover/retire declaration, and the
    # refusal receipt was previously the one place that did NOT carry it — the
    # runbook had to send operators to a dry_run, which classifies a DIFFERENT
    # model set.  Stays None on the id-only path, where the value would be
    # derived from checksum-less rows (same rule as `new_registry_sha256`).
    generation: str | None = None
    added: list[str] = dataclass_field(default_factory=list)
    unchanged: list[str] = dataclass_field(default_factory=list)
    removed: list[str] = dataclass_field(default_factory=list)
    package_changed: list[dict[str, str]] = dataclass_field(default_factory=list)
    refused: list[dict[str, Any]] = dataclass_field(default_factory=list)
    declared_cutovers: list[dict[str, Any]] = dataclass_field(default_factory=list)
    # #1433: removals admitted by a `transition_mode: "retire"` declaration
    # entry — a subset of `removed` that carries no refusal.
    declared_retirements: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def to_receipt(self) -> dict[str, Any]:
        def id_group(values: Sequence[str]) -> dict[str, Any]:
            total = len(values)
            items = list(values)[:MAX_COLLECTION_ITEMS]
            return {"items": items, "total": total, "truncated": total > len(items)}

        def obj_group(values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            total = len(values)
            items = [dict(item) for item in values[:MAX_COLLECTION_ITEMS]]
            return {"items": items, "total": total, "truncated": total > len(items)}

        return {
            "previous_registry_sha256": self.previous_registry_sha256,
            "new_registry_sha256": self.new_registry_sha256,
            "previous_model_count": self.previous_model_count,
            "prospective_model_count": int(self.prospective_model_count),
            "mode": self.mode,
            "generation": self.generation,
            "added": id_group(sorted(self.added)),
            "unchanged": id_group(sorted(self.unchanged)),
            "removed": id_group(sorted(self.removed)),
            "package_changed": obj_group(
                sorted(self.package_changed, key=lambda item: item["model_id"])
            ),
            "refused": obj_group(sorted(self.refused, key=lambda item: item["model_id"])),
            "declared_cutovers": obj_group(
                sorted(self.declared_cutovers, key=lambda item: item["model_id"])
            ),
            "declared_retirements": obj_group(
                sorted(self.declared_retirements, key=lambda item: item["model_id"])
            ),
        }

def _skip_cause_evidence(
    skipped_models: Mapping[str, Mapping[str, Any]] | None, model_id: str
) -> dict[str, Any] | None:
    """Return bounded skip-cause evidence for ``model_id`` (or ``None``).

    ``None`` means bulk publish never reported the model as skipped — the
    removal is a disappeared model directory rather than an unpublishable
    package, and the refusal entry carries no skip-cause keys.  That absence is
    the discriminator operators read (#1433).
    """
    row = (skipped_models or {}).get(model_id)
    if not isinstance(row, Mapping):
        return None

    def _bounded_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item)[:MAX_STRING_LENGTH] for item in value[:MAX_COLLECTION_ITEMS]]

    return {
        "status": str(row.get("status") or "")[:MAX_STRING_LENGTH],
        "missing_required_files": _bounded_list(row.get("missing_required_files")),
        "invalid_required_files": _bounded_list(row.get("invalid_required_files")),
        "unreadable_required_files": _bounded_list(row.get("unreadable_required_files")),
    }

def _classify_registry(
    *,
    previous: Sequence[Mapping[str, Any]] | None,
    prospective: Sequence[Mapping[str, Any]],
    previous_sha256: str | None,
    new_sha256: str | None,
    generation: str,
    declaration: Mapping[str, Any] | None,
    dry_run: bool,
    skipped_models: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[_RegistryClassification, str | None]:
    """Return (classification, refusal_reason).

    Refusal semantics only apply to real publishes; ``dry_run`` reports the
    id-only shape without failing so operators can preview additions.
    """
    result = _RegistryClassification(
        previous_registry_sha256=previous_sha256,
        new_registry_sha256=None if dry_run else new_sha256,
        # Round-1 F-A: publish the generation the declaration must bind to.  The
        # id-only path derives it from rows that carry no checksums, so the
        # value would not be the one a real refresh binds — omit it there.
        generation=None if dry_run else generation,
    )
    prospective_by_id: dict[str, Mapping[str, Any]] = {}
    for row in prospective:
        model_id = str(row.get("model_id") or "")
        if not model_id:
            raise RefreshError("provider_invalid")
        if model_id in prospective_by_id:
            # A duplicate model_id in the prospective set is a data-shape bug
            # upstream; refuse before any canonical replacement.
            raise RefreshError("provider_invalid")
        prospective_by_id[model_id] = row
    previous_by_id: dict[str, Mapping[str, Any]] = {}
    if previous is not None:
        for row in previous:
            model_id = str(row.get("model_id") or "")
            if not model_id:
                raise RefreshError("provider_invalid")
            if model_id in previous_by_id:
                raise RefreshError("provider_invalid")
            previous_by_id[model_id] = row
    # R2-N1: pin the exact partition counts here (not derived from the sum of
    # bucket totals) so the receipt validator can enforce EQUALITY against a
    # tampered classification.
    result.prospective_model_count = len(prospective_by_id)
    result.previous_model_count = (
        None if previous is None else len(previous_by_id)
    )

    # In dry-run mode prospective rows carry only id/basin_id (no checksum),
    # so checksum-based drift cannot be observed and we do a lenient id-only
    # classification.  Operators preview additions this way.
    if dry_run:
        # #1140: record the partition actually taken so the receipt validator
        # can reconcile against the id-only rules no matter which terminal
        # outcome the run ends on.
        result.mode = "id_only"
        for model_id in prospective_by_id:
            if model_id in previous_by_id:
                result.unchanged.append(model_id)
            else:
                result.added.append(model_id)
        # dry_run does not evaluate removals or refuse; the strict gate only
        # runs on real publish.
        return result, None

    for model_id, row in prospective_by_id.items():
        if model_id not in previous_by_id:
            result.added.append(model_id)
            continue
        previous_row = previous_by_id[model_id]
        if _rows_have_identical_identity(row, previous_row):
            result.unchanged.append(model_id)
        else:
            result.package_changed.append(
                {
                    "model_id": model_id,
                    "old_checksum": str(previous_row.get("package_checksum") or ""),
                    "new_checksum": str(row.get("package_checksum") or ""),
                }
            )
    for model_id in previous_by_id:
        if model_id not in prospective_by_id:
            result.removed.append(model_id)

    declaration_entries: dict[str, Mapping[str, Any]] = {}
    declaration_generation: str | None = None
    if declaration is not None:
        declaration_generation = str(declaration.get("generation") or "")
        for entry in declaration.get("entries") or []:
            declaration_entries[str(entry.get("model_id") or "")] = entry

    # Declaration must bind to this exact prospective generation.  A stale or
    # rebuilt registry cannot accidentally activate a formerly-approved
    # transition.
    generation_matches = (
        declaration is None or declaration_generation == generation
    )

    def refuse_declaration(model_id: str, entry: Mapping[str, Any] | None) -> None:
        result.refused.append(
            {
                "model_id": model_id,
                "old_checksum": str((entry or {}).get("old_checksum") or "") or None,
                "new_checksum": str((entry or {}).get("new_checksum") or "") or None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )

    # 1. Every declaration entry must name a model this refresh can act on.
    #    A `replace` entry must be in the prospective registry — an operator
    #    cannot cutover an unknown model.  A `retire` entry (#1433) is
    #    validated against the REMOVAL set instead: by definition its model is
    #    absent from the prospective registry, so the prospective membership
    #    test would reject every legal retirement.  Retiring a model that is
    #    still being published, or one that was never canonical, is invalid.
    removed_ids = set(result.removed)
    unknown_declaration_ids = [
        model_id
        for model_id, entry in declaration_entries.items()
        if (
            model_id not in removed_ids
            if str(entry.get("transition_mode") or "") == "retire"
            else model_id not in prospective_by_id
        )
    ]
    declaration_invalid = bool(unknown_declaration_ids) or not generation_matches
    for model_id in unknown_declaration_ids:
        refuse_declaration(model_id, declaration_entries[model_id])
    if not generation_matches:
        # Attach a synthetic marker so operators see the generation mismatch
        # without echoing the DECLARATION's value back.  The prospective side is
        # deliberately public since round-1 F-A (`classification.generation`) —
        # it is what the operator has to write into the declaration; what stays
        # out of the receipt is the stale value they wrote last time.
        result.refused.append(
            {
                "model_id": "__declaration__",
                "old_checksum": None,
                "new_checksum": None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )

    # 2. For each package_changed row: look for a matching declaration entry
    #    with correct old/new checksums.  Accept it when everything aligns,
    #    otherwise record the specific invalidity mode.
    undeclared: list[Mapping[str, Any]] = []
    for changed in result.package_changed:
        model_id = changed["model_id"]
        entry = declaration_entries.get(model_id)
        if entry is None:
            undeclared.append(changed)
            continue
        old_matches = str(entry.get("old_checksum")) == changed["old_checksum"]
        new_matches = str(entry.get("new_checksum")) == changed["new_checksum"]
        if declaration_invalid or not old_matches or not new_matches:
            declaration_invalid = True
            refuse_declaration(model_id, entry)
            continue
        result.declared_cutovers.append(
            {
                "model_id": model_id,
                "old_checksum": changed["old_checksum"],
                "new_checksum": changed["new_checksum"],
                "effective_cycle_utc": str(entry.get("effective_cycle_utc") or ""),
                "transition_mode": str(entry.get("transition_mode") or ""),
            }
        )

    # 3. Any package_changed row not covered by a valid declaration is
    #    undeclared drift.
    for changed in undeclared:
        result.refused.append(
            {
                "model_id": changed["model_id"],
                "old_checksum": changed["old_checksum"],
                "new_checksum": changed["new_checksum"],
                "reason": "registry_cutover_undeclared",
            }
        )

    # 4. Removals are admissible ONLY through a declared retirement (#1433);
    #    every other removal stays refused exactly as #1080 required.
    #
    #    Two passes, deliberately stricter than rule 2's single pass: rule 2
    #    lets an early valid row into `declared_cutovers` before a later entry
    #    invalidates the declaration, which is tolerable because a replacement
    #    only swaps a checksum.  Dropping a canonical row is destructive, so
    #    admission must not depend on the iteration order of `removed`: pass 1
    #    settles the poison bit, pass 2 admits only when the declaration is
    #    valid as a whole.
    matched_retirements: list[tuple[str, Mapping[str, Any]]] = []
    unmatched_removals: list[str] = []
    for model_id in result.removed:
        entry = declaration_entries.get(model_id)
        if entry is None or str(entry.get("transition_mode") or "") != "retire":
            # No entry at all, or a `replace` entry naming this model (already
            # refused by rule 1 as an unknown id): the removal itself is
            # undeclared.
            unmatched_removals.append(model_id)
            continue
        previous_checksum = (
            str(previous_by_id[model_id].get("package_checksum") or "") or None
        )
        if str(entry.get("old_checksum")) != previous_checksum:
            # Same semantics as rule 2's checksum mismatch: poison the
            # declaration and refuse the ENTRY.  This removal gets exactly one
            # refusal row — no additional `removal_refused` row for it.
            declaration_invalid = True
            refuse_declaration(model_id, entry)
            continue
        matched_retirements.append((model_id, entry))

    for model_id, entry in matched_retirements:
        if declaration_invalid:
            # A declaration invalidated by ANY entry (rule 1, rule 2, or a
            # sibling retirement) admits no retirement in this run.
            refuse_declaration(model_id, entry)
            continue
        result.declared_retirements.append(
            {
                "model_id": model_id,
                "old_checksum": str(entry.get("old_checksum") or "") or None,
                "new_checksum": None,
                "effective_cycle_utc": str(entry.get("effective_cycle_utc") or ""),
                "transition_mode": str(entry.get("transition_mode") or ""),
            }
        )

    for model_id in unmatched_removals:
        refusal: dict[str, Any] = {
            "model_id": model_id,
            "old_checksum": str(previous_by_id[model_id].get("package_checksum") or "") or None,
            "new_checksum": None,
            "reason": "registry_cutover_removal_refused",
        }
        # #1433: when bulk publish reported the model as skipped, say WHY the
        # row disappeared.  Only removal refusals carry this evidence.
        evidence = _skip_cause_evidence(skipped_models, model_id)
        if evidence is not None:
            refusal.update(evidence)
        result.refused.append(refusal)

    # Decide the single refusal reason to raise (declaration-invalid takes
    # priority so operators see the schema problem first, then removal, then
    # undeclared drift).
    if declaration_invalid:
        return result, "registry_cutover_declaration_invalid"
    if unmatched_removals:
        return result, "registry_cutover_removal_refused"
    if undeclared:
        return result, "registry_cutover_undeclared"
    return result, None

def _registry_precommit_gate(
    workspace: Path,
    packages: Sequence[Mapping[str, Any]],
    registry_models: Sequence[Mapping[str, Any]],
    *,
    previous_registry_bytes: bytes | None,
    previous_registry_sha256: str | None,
    prospective_generated_at: datetime,
    cutover_declaration_env: str | None,
    dry_run: bool,
    classification_sink: Callable[[dict[str, Any]], None],
    now: datetime | None = None,
    skipped_models: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Precommit gate.

    Order: classification/declaration first (semantic refusals should fail
    fast without paying for workspace enumeration), then workspace/orphan
    bounds.  Classification is delivered to ``classification_sink`` even on
    refusal so the receipt path can attach the payload.
    """
    orphan_items = [item for item in packages if item.get("status") == "published"]

    previous_models: list[dict[str, Any]] | None
    if previous_registry_bytes is None:
        previous_models = None
    else:
        try:
            previous_payload = json.loads(previous_registry_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry could not be parsed.",
                details={"provider_reason": "provider_invalid", "provider_phase": "precommit"},
            ) from error
        raw_models = previous_payload.get("models") if isinstance(previous_payload, Mapping) else None
        if not isinstance(raw_models, list):
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry has no model list.",
                details={"provider_reason": "provider_invalid", "provider_phase": "precommit"},
            )
        previous_models = [dict(row) for row in raw_models]

    prospective_generation = _prospective_registry_generation(
        registry_models, generated_at=prospective_generated_at
    )
    _, new_sha = _prospective_registry_content(
        registry_models, generated_at=prospective_generated_at
    )

    declaration: dict[str, Any] | None = None
    reference_now = now or datetime.now(UTC)
    declaration_load_error: RefreshError | None = None
    try:
        declaration = _load_cutover_declaration(cutover_declaration_env, now=reference_now)
    except RefreshError as error:
        declaration_load_error = error

    classification, refusal_reason = _classify_registry(
        previous=previous_models,
        prospective=registry_models,
        previous_sha256=previous_registry_sha256,
        new_sha256=new_sha,
        generation=prospective_generation,
        declaration=declaration,
        dry_run=dry_run,
        skipped_models=skipped_models,
    )
    if declaration_load_error is not None:
        # Surface the file-level load failure without needing the operator to
        # inspect provider evidence; treat it as declaration-invalid.
        classification.refused.append(
            {
                "model_id": "__declaration__",
                "old_checksum": None,
                "new_checksum": None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )
        refusal_reason = "registry_cutover_declaration_invalid"

    classification_sink(classification.to_receipt())

    if refusal_reason is not None:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
            "Registry cutover gate refused canonical replacement before commit.",
            details={
                "provider_reason": refusal_reason,
                "provider_phase": "precommit",
                "discovered_total": len(packages),
                "attempted_total": len(packages),
                "created_total": len(orphan_items),
                "packages": [
                    {
                        "status": item.get("status"),
                        "orphan_id": hashlib.sha256(
                            str(item.get("manifest_uri") or "").encode("utf-8")
                        ).hexdigest()[:32],
                    }
                    for item in orphan_items[:MAX_ORPHAN_EVIDENCE]
                ],
            },
        )

    try:
        _enforce_workspace_bounds(workspace)
        if len(orphan_items) > MAX_ORPHANS:
            raise RefreshError("orphan_limit_exceeded")
        if not registry_models or len(registry_models) > MAX_ORPHANS:
            raise RefreshError("orphan_limit_exceeded")
    except RefreshError as error:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
            "Refresh bounds failed before canonical registry replacement.",
            details={
                "provider_reason": error.reason,
                "provider_phase": "precommit",
                "discovered_total": len(packages),
                "attempted_total": len(packages),
                "created_total": len(orphan_items),
                "packages": [
                    {
                        "status": item.get("status"),
                        "orphan_id": hashlib.sha256(
                            str(item.get("manifest_uri") or "").encode("utf-8")
                        ).hexdigest()[:32],
                    }
                    for item in orphan_items[:MAX_ORPHAN_EVIDENCE]
                ],
            },
        ) from error
