"""Registry classification receipt block validation and reconciliation.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scripts.scheduler_refresh.constants import (
    CUTOVER_REPLACE_TRANSITION_MODES,
    CUTOVER_RETIRE_TRANSITION_MODES,
    CUTOVER_TRANSITION_MODES,
    MAX_COLLECTION_ITEMS,
    MAX_STRING_LENGTH,
    REGISTRY_CUTOVER_REFUSAL_REASONS,
)
from scripts.scheduler_refresh.identity import (
    GENERATION_PATTERN,
    MAX_GENERATION_LENGTH,
    MAX_MODEL_ID_LENGTH,
    MODEL_ID_PATTERN,
)

_CLASSIFICATION_GROUP_KEYS = frozenset({"items", "total", "truncated"})

# #1140: optional classification keys.  `mode` records which partition
# `_classify_registry` ran (`id_only` on the dry_run early return, `full`
# otherwise).  It stays OPTIONAL because the validator also reads untrusted
# on-disk receipts written before #1140 (`reconstruct_primary_receipt` /
# `validate_current_receipt`), which carry no such key.
CLASSIFICATION_MODES = frozenset({"id_only", "full"})

# #1433: `declared_retirements` joins `mode` as OPTIONAL for the same reason —
# `reconstruct_primary_receipt` / `validate_current_receipt` read on-disk
# receipts written before the bucket existed, and a required key would
# retroactively invalidate every one of them.
# `generation` (round-1 F-A) is optional for the same reason: it is the value an
# operator must copy into a declaration, and receipts written before it existed
# are honest, not tampered.
_CLASSIFICATION_OPTIONAL_KEYS = frozenset({"mode", "declared_retirements", "generation"})

# #1433/#1553: skip-cause evidence keys copied onto a `registry_cutover_removal_refused`
# entry when bulk publish reported the model as skipped.  Same key names the
# publisher's not-publishable diagnostics use
# (`publish_scheduler_file_registry.py:675`) so operators read one vocabulary.
# `unreadable_required_files` mirrors the third discovery cause state (#1552):
# a required file that MATCHED but could not be read is neither missing nor
# invalid, and an omission here would render a `partial` refusal with every
# cause list empty.
_SKIP_CAUSE_LIST_KEYS = frozenset(
    {"missing_required_files", "invalid_required_files", "unreadable_required_files"}
)

_SKIP_CAUSE_KEYS = frozenset({"status"}) | _SKIP_CAUSE_LIST_KEYS

def _validate_registry_classification_field(receipt: Mapping[str, Any]) -> None:
    classification = receipt.get("registry_classification")
    outcome = receipt.get("outcome")
    reason = receipt.get("reason")
    requires_classification = (
        outcome in {"dry_run", "published"}
        or reason in REGISTRY_CUTOVER_REFUSAL_REASONS
    )
    if classification is None:
        if requires_classification:
            raise ValueError("receipt_classification_required")
        return
    if not isinstance(classification, Mapping):
        raise ValueError("receipt_classification_invalid")
    required = {
        "previous_registry_sha256",
        "new_registry_sha256",
        # R2-N1: partition counts are required on every classified receipt so
        # reconciliation is enforceable as equality, not just non-negative.
        "previous_model_count",
        "prospective_model_count",
        "added",
        "unchanged",
        "removed",
        "package_changed",
        "refused",
        "declared_cutovers",
    }
    keys = set(classification)
    # Same difference pattern as RECEIPT_KEYS/RECEIPT_OPTIONAL_KEYS (#1132):
    # every required key present, and no key outside required ∪ optional.
    if not required <= keys or (keys - required) - _CLASSIFICATION_OPTIONAL_KEYS:
        raise ValueError("receipt_classification_invalid")
    if "mode" in classification:
        mode = classification.get("mode")
        if not isinstance(mode, str) or mode not in CLASSIFICATION_MODES:
            raise ValueError("receipt_classification_invalid")
    if "generation" in classification:
        generation = classification.get("generation")
        # Explicit null is the id-only shape (no meaningful generation); a
        # present string must be copy-pasteable into a declaration.
        if generation is not None and (
            not isinstance(generation, str)
            or not generation
            or len(generation) > MAX_GENERATION_LENGTH
            or GENERATION_PATTERN.fullmatch(generation) is None
        ):
            raise ValueError("receipt_classification_invalid")
    for hash_field in ("previous_registry_sha256", "new_registry_sha256"):
        value = classification.get(hash_field)
        if value is not None:
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError("receipt_classification_invalid")
    previous_count = classification.get("previous_model_count")
    if previous_count is not None:
        if (
            not isinstance(previous_count, int)
            or isinstance(previous_count, bool)
            or previous_count < 0
        ):
            raise ValueError("receipt_classification_invalid")
    prospective_count = classification.get("prospective_model_count")
    if (
        not isinstance(prospective_count, int)
        or isinstance(prospective_count, bool)
        or prospective_count < 0
    ):
        raise ValueError("receipt_classification_invalid")
    for group_name in ("added", "unchanged", "removed"):
        group = classification.get(group_name)
        if not isinstance(group, Mapping) or set(group) != _CLASSIFICATION_GROUP_KEYS:
            raise ValueError("receipt_classification_invalid")
        items = group.get("items")
        if not isinstance(items, list) or len(items) > MAX_COLLECTION_ITEMS:
            raise ValueError("receipt_classification_invalid")
        for item in items:
            if (
                not isinstance(item, str)
                or not item
                or len(item) > MAX_MODEL_ID_LENGTH
                or MODEL_ID_PATTERN.fullmatch(item) is None
            ):
                # Same corpus as the schema (see MODEL_ID_PATTERN). Runtime
                # and jsonschema.Draft202012Validator must accept/reject the
                # same set of items or fixtures drift.
                raise ValueError("receipt_classification_invalid")
        _validate_group_totals(group, items)
    package_changed = classification.get("package_changed")
    _validate_object_group(
        package_changed,
        required_keys={"model_id", "old_checksum", "new_checksum"},
        optional_keys=set(),
    )
    refused = classification.get("refused")
    _validate_object_group(
        refused,
        required_keys={"model_id", "reason"},
        optional_keys={"old_checksum", "new_checksum"} | set(_SKIP_CAUSE_KEYS),
        reason_enum=REGISTRY_CUTOVER_REFUSAL_REASONS,
    )
    declared = classification.get("declared_cutovers")
    _validate_object_group(
        declared,
        required_keys={
            "model_id",
            "old_checksum",
            "new_checksum",
            "effective_cycle_utc",
            "transition_mode",
        },
        optional_keys=set(),
        transition_modes=CUTOVER_REPLACE_TRANSITION_MODES,
    )
    if "declared_retirements" in classification:
        _validate_object_group(
            classification.get("declared_retirements"),
            required_keys={
                "model_id",
                "old_checksum",
                "new_checksum",
                "effective_cycle_utc",
                "transition_mode",
            },
            optional_keys=set(),
            transition_modes=CUTOVER_RETIRE_TRANSITION_MODES,
            null_keys=frozenset({"new_checksum"}),
        )
    _enforce_registry_classification_reconciliation(
        classification, outcome=outcome, reason=reason
    )

def _enforce_registry_classification_reconciliation(
    classification: Mapping[str, Any],
    *,
    outcome: Any,
    reason: Any,
) -> None:
    """Cross-check every classification total against the reconciliation formulas.

    Governing invariants from spec.md:397-403:

    * ``unchanged + package_changed + removed == previous_count`` when the
      previous canonical registry existed.
    * ``added + unchanged + package_changed == prospective_count``.
    * ``declared_cutovers`` model_ids are a subset of ``package_changed``
      model_ids.
    * ``declared_retirements`` (#1433, optional bucket — legacy receipts read
      as empty) model_ids are a subset of ``removed`` model_ids AND
      ``declared_retirements.total <= removed.total``; the bucket is empty in
      id-only mode.
    * ``refused`` covers every ``removed`` entry not admitted by a declared
      retirement, every ``package_changed`` entry not in ``declared_cutovers``,
      and every ``declaration_invalid`` entry (including synthetic
      ``__declaration__`` markers).
    * id-only mode (``classification.mode == "id_only"``, i.e. the dry_run
      partition of ``_classify_registry``; legacy receipts with no ``mode``
      key fall back to ``outcome == "dry_run"``): ``package_changed`` may
      legitimately be zero because prospective rows carry only ids.
      The constraints the id-only classify path still guarantees ARE
      enforced (#1135): ``removed.total == 0`` (the id-only path returns
      before the removal loop), ``previous_registry_sha256``/
      ``previous_model_count`` null together or non-null together (count a
      non-boolean int >= 0), ``unchanged <= previous_model_count`` when a
      previous registry exists, ``unchanged.total == 0`` on bootstrap (a null
      ``previous_registry_sha256`` means an empty previous set, so every
      prospective row classifies as added), and ``new_registry_sha256 is
      None`` (an id-only classification only arises from dry_run, which never
      publishes).  ``refused`` may hold only the synthetic
      ``__declaration__`` marker's ``registry_cutover_declaration_invalid``
      reason — the writer appends it after ``_classify_registry`` regardless
      of dry_run — and #1144 bounds that bucket to AT MOST one untruncated
      marker row (``total == len(items) <= 1``) which never rides a
      ``dry_run`` receipt, because the declaration failure that produces it
      terminates the run as ``outcome="failed"``; the legacy outcome-keyed
      fallback keeps rejecting every refused row.  The full previous-side equality is NOT applied here:
      removals are never computed, so previous rows absent from the
      prospective set are legitimately unaccounted for.  Mode and outcome are
      cross-checked: ``dry_run`` requires ``id_only`` while ``published`` and
      ``published_receipt_failed`` require ``full``.
    * ``published``: ``refused.total == 0`` (a non-zero refusal would have
      raised before commit).
    * refusal outcomes: ``refused.total >= 1``.
    """
    def _total(group_name: str) -> int:
        group = classification.get(group_name)
        if not isinstance(group, Mapping):
            raise ValueError("receipt_classification_invalid")
        total = group.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError("receipt_classification_invalid")
        return total

    def _items(group_name: str) -> list[Any]:
        group = classification.get(group_name)
        if not isinstance(group, Mapping):
            raise ValueError("receipt_classification_invalid")
        items = group.get("items")
        if not isinstance(items, list):
            raise ValueError("receipt_classification_invalid")
        return items

    def _optional_group(group_name: str) -> tuple[int, list[Any], bool]:
        """#1433: read a bucket that legacy receipts do not carry.

        An absent bucket reads as ``(0, [], False)`` — pre-#1433 receipts on
        disk are honest, not tampered.  A PRESENT bucket is validated exactly as
        strictly as a required one.
        """
        if group_name not in classification:
            return 0, [], False
        group = classification.get(group_name)
        truncated = isinstance(group, Mapping) and group.get("truncated") is True
        return _total(group_name), _items(group_name), truncated

    added_total = _total("added")
    unchanged_total = _total("unchanged")
    removed_total = _total("removed")
    package_changed_total = _total("package_changed")
    refused_total = _total("refused")
    declared_total = _total("declared_cutovers")

    package_changed_ids = {
        item.get("model_id")
        for item in _items("package_changed")
        if isinstance(item, Mapping)
    }
    declared_ids = {
        item.get("model_id")
        for item in _items("declared_cutovers")
        if isinstance(item, Mapping)
    }
    if not declared_ids <= package_changed_ids:
        # `declared_cutovers ⊆ package_changed` per spec — any declared row
        # must also be listed in `package_changed`.
        raise ValueError("receipt_classification_invalid")

    # #1433: the same containment for the retirement bucket, plus a total-level
    # inequality.  Neither is sufficient alone: `⊆` runs on items, which
    # truncate at MAX_COLLECTION_ITEMS, and the total bounds the rows the items
    # cannot name.  What actually closes the "inflate the total, empty the
    # items" forgery is the honest-truncation shape rule in
    # `_validate_group_totals` (round-1 F-B); above 256 rows a residue remains
    # by construction — see the note there.
    retired_total, retired_items, retired_truncated = _optional_group(
        "declared_retirements"
    )
    retired_ids = {
        item.get("model_id") for item in retired_items if isinstance(item, Mapping)
    }
    removed_ids = {item for item in _items("removed") if isinstance(item, str)}
    if not retired_ids <= removed_ids:
        raise ValueError("receipt_classification_invalid")
    if retired_total > removed_total:
        raise ValueError("receipt_classification_invalid")

    prospective_count = classification.get("prospective_model_count")
    if (
        not isinstance(prospective_count, int)
        or isinstance(prospective_count, bool)
        or prospective_count < 0
    ):
        raise ValueError("receipt_classification_invalid")

    # #1140: branch on the partition `_classify_registry` actually ran, not on
    # the terminal outcome.  A dry_run that fails AFTER the precommit gate
    # carries an honest id-only classification on an `outcome="failed"`
    # receipt; keying on the outcome routed it into the full-equality branch
    # and destroyed the receipt.  Legacy (pre-#1140) receipts carry no `mode`
    # and keep the outcome-keyed selection verbatim.
    mode = classification.get("mode")
    if mode is None:
        id_only = outcome == "dry_run"
    else:
        if not isinstance(mode, str) or mode not in CLASSIFICATION_MODES:
            raise ValueError("receipt_classification_invalid")
        # Forged combinations: dry_run is the only producer of an id-only
        # classification, and a publish always classified in full mode.
        if outcome == "dry_run" and mode != "id_only":
            raise ValueError("receipt_classification_invalid")
        if outcome == "published" and mode != "full":
            raise ValueError("receipt_classification_invalid")
        # `published_receipt_failed` is only ever stamped onto an already
        # committed `published` receipt (see the primary-receipt publish
        # fallback), so it too always classified in full mode — and it is the
        # ONE outcome `reconstruct_primary_receipt` accepts off the untrusted
        # emergency slot.  Without this pin a forged emergency record could
        # claim `mode="id_only"` and skip the full previous-side equality.
        if outcome == "published_receipt_failed" and mode != "full":
            raise ValueError("receipt_classification_invalid")
        id_only = mode == "id_only"

    if id_only:
        # id-only classification: prospective rows have no checksum, so
        # package_changed/declared_cutovers stay empty by construction.
        if package_changed_total != 0 or declared_total != 0:
            raise ValueError("receipt_classification_invalid")
        # #1433: the id-only path returns before the removal loop, so it can
        # never admit a retirement either.  A present bucket must read zero; an
        # absent one already does.
        if retired_total != 0:
            raise ValueError("receipt_classification_invalid")
        # Round-1 F-A: the id-only writer pins `generation` to None (the value
        # would come from checksum-less rows), so a non-null generation on an
        # id-only classification is forged.  Absent reads as None.
        if classification.get("generation") is not None:
            raise ValueError("receipt_classification_invalid")
        if mode is None:
            # Legacy outcome-keyed fallback: behavior frozen as it was before
            # #1140 — any refused row on a dry_run receipt is forged.
            if refused_total != 0:
                raise ValueError("receipt_classification_invalid")
        else:
            # The writer appends the synthetic `__declaration__` refusal after
            # `_classify_registry` without consulting dry_run, so
            # `registry_cutover_declaration_invalid` is the only refusal an
            # id-only classification can carry; every other reason is forged.
            refused_items = _items("refused")
            if any(
                not isinstance(item, Mapping)
                or item.get("reason") != "registry_cutover_declaration_invalid"
                for item in refused_items
            ):
                raise ValueError("receipt_classification_invalid")
            # #1144: bound the bucket itself.  `_classify_registry` returns an
            # EMPTY refused list on the id-only early return, and the single
            # place that can extend it (`_registry_precommit_gate`'s
            # declaration-load failure) appends exactly one synthetic
            # `__declaration__` row — which `to_receipt` never truncates.  So
            # anything beyond one untruncated marker row is forged, including
            # a wiped `items` list carrying an inflated `total`.
            refused_group = classification.get("refused")
            if (
                not isinstance(refused_group, Mapping)
                or refused_group.get("truncated") is not False
                or refused_total != len(refused_items)
                or refused_total > 1
                or any(
                    item.get("model_id") != "__declaration__" for item in refused_items
                )
            ):
                raise ValueError("receipt_classification_invalid")
            # That declaration failure sets the refusal reason, which makes
            # `_registry_precommit_gate` raise before the dry_run receipt is
            # built, so the run terminates as `outcome="failed"`: a dry_run
            # receipt carrying ANY refusal has no legal writer.
            if outcome == "dry_run" and refused_total != 0:
                raise ValueError("receipt_classification_invalid")
        # Even in id-only mode the added+unchanged+package_changed equality must
        # bind to the pinned prospective_model_count (package_changed is 0
        # here so this reduces to added+unchanged == prospective_count).
        if added_total + unchanged_total + package_changed_total != prospective_count:
            raise ValueError("receipt_classification_invalid")
        # #1135: dry_run returns before the removal loop (`_classify_registry`
        # bails at the id-only classification), so any removed row is forged.
        if removed_total != 0:
            raise ValueError("receipt_classification_invalid")
        prev_count = classification.get("previous_model_count")
        prev_sha = classification.get("previous_registry_sha256")
        if prev_sha is None:
            # Bootstrap: no previous canonical registry means no pinned count.
            if prev_count is not None:
                raise ValueError("receipt_classification_invalid")
            # Dual of the `unchanged <= prev_count` bound below: with an empty
            # previous_by_id every prospective row classifies as added.
            if unchanged_total != 0:
                raise ValueError("receipt_classification_invalid")
        else:
            if (
                not isinstance(prev_count, int)
                or isinstance(prev_count, bool)
                or prev_count < 0
            ):
                raise ValueError("receipt_classification_invalid")
            # Upper bound only — `unchanged` are ids present in BOTH sets, so
            # it cannot exceed the previous count.  The full previous-side
            # equality does NOT hold in dry_run (removals uncomputed).
            if unchanged_total > prev_count:
                raise ValueError("receipt_classification_invalid")
        # An id-only classification only arises from dry_run, which never
        # publishes; the writer pins new_registry_sha256 to None.
        if classification.get("new_registry_sha256") is not None:
            raise ValueError("receipt_classification_invalid")
        # Terminal pin, kept here as well as on the full path: the writer sets
        # the receipt-level refusal reason and appends the refused row in one
        # action, so the two must stand or fall together.  Without it an
        # id-only receipt could keep its refusal reason while its refused
        # bucket is wiped flat.
        if reason in REGISTRY_CUTOVER_REFUSAL_REASONS and refused_total < 1:
            raise ValueError("receipt_classification_invalid")
        return

    previous_count = classification.get("previous_model_count")
    previous_sha = classification.get("previous_registry_sha256")
    # R2-N1: enforce EQUALITY, not just non-negative bounds.  The pinned
    # counts (`previous_model_count` / `prospective_model_count`) come from
    # `_classify_registry`'s own len(previous_by_id)/len(prospective_by_id)
    # so any drop or gain in the bucket totals fails validation on-disk.
    if previous_sha is None:
        # A missing previous canonical registry MUST also carry a null
        # previous_model_count (bootstrap semantics).  A non-null count with
        # no previous SHA is contradictory shape.
        if previous_count is not None:
            raise ValueError("receipt_classification_invalid")
        # Dual of the non-bootstrap equality below: with no previous registry
        # there is nothing to carry over, so no row can be unchanged,
        # package_changed, or removed.
        if unchanged_total + package_changed_total + removed_total != 0:
            raise ValueError("receipt_classification_invalid")
    else:
        if (
            not isinstance(previous_count, int)
            or isinstance(previous_count, bool)
            or previous_count < 0
        ):
            raise ValueError("receipt_classification_invalid")
        if unchanged_total + package_changed_total + removed_total != previous_count:
            raise ValueError("receipt_classification_invalid")

    if added_total + unchanged_total + package_changed_total != prospective_count:
        raise ValueError("receipt_classification_invalid")

    # refused equals every removed entry NOT admitted by a declared retirement
    # (#1433) + every package_changed entry not in declared_cutovers + every
    # entry rejected by declaration_invalid.  The declaration_invalid slice is
    # unbounded (may include the synthetic `__declaration__` marker) so we
    # assert only the lower bound.
    #
    # Round-1 F-B: deduct the NAMED retirements when the bucket is untruncated
    # (an id claimed but absent from `removed` deducts nothing), and fall back
    # to the total once truncation makes naming impossible — an honest run with
    # more than 256 retirements lists only 256 of them, and deducting the
    # intersection there would refuse a receipt the writer built correctly.
    # Either way `retired_total <= removed_total` is enforced above, so the
    # first term cannot go negative.
    retired_deduction = (
        retired_total if retired_truncated else len(retired_ids & removed_ids)
    )
    expected_min_refused = (removed_total - retired_deduction) + max(
        package_changed_total - declared_total, 0
    )
    if refused_total < expected_min_refused:
        raise ValueError("receipt_classification_invalid")

    if outcome == "published":
        # A publish that emits any refused entry contradicts the gate
        # contract (the gate would have raised before commit).
        if refused_total != 0:
            raise ValueError("receipt_classification_invalid")

    if reason in REGISTRY_CUTOVER_REFUSAL_REASONS:
        if refused_total < 1:
            raise ValueError("receipt_classification_invalid")

def _validate_group_totals(group: Mapping[str, Any], items: Sequence[Any]) -> None:
    """Bind a classification group's `total`/`truncated` to its `items`.

    Round-1 F-B: a truncated group must carry a FULL item list.  ``to_receipt``
    truncates at exactly ``MAX_COLLECTION_ITEMS``, so ``truncated=true`` with a
    short (or emptied) list has no legal writer — and without this rule a forger
    could wipe the items, inflate the total, and deflate a lower bound that is
    computed from totals.  Same shape #1144 already pins on the id-only
    ``refused`` bucket, applied at both call sites (so the identical hole in
    ``declared_cutovers`` closes with it).

    The residue is inherent and deliberate: above ``MAX_COLLECTION_ITEMS`` the
    receipt can only name the first 256 rows, so the rows beyond the cap stay
    unverifiable by item.  This rule bounds the forgery to that window instead
    of leaving it unbounded.
    """
    total = group.get("total")
    truncated = group.get("truncated")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("receipt_classification_invalid")
    if total < len(items) or not isinstance(truncated, bool):
        raise ValueError("receipt_classification_invalid")
    if truncated is not (total > len(items)):
        raise ValueError("receipt_classification_invalid")
    if truncated and len(items) != MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_classification_invalid")

def _validate_object_group(
    group: Any,
    *,
    required_keys: set[str],
    optional_keys: set[str],
    reason_enum: frozenset[str] | None = None,
    transition_modes: frozenset[str] = CUTOVER_TRANSITION_MODES,
    null_keys: frozenset[str] = frozenset(),
) -> None:
    if not isinstance(group, Mapping) or set(group) != _CLASSIFICATION_GROUP_KEYS:
        raise ValueError("receipt_classification_invalid")
    items = group.get("items")
    if not isinstance(items, list) or len(items) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_classification_invalid")
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("receipt_classification_invalid")
        keys = set(item)
        if not required_keys <= keys or (keys - required_keys) - optional_keys:
            raise ValueError("receipt_classification_invalid")
        for name, value in item.items():
            if name == "model_id":
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value) > MAX_MODEL_ID_LENGTH
                    or MODEL_ID_PATTERN.fullmatch(value) is None
                ):
                    # Same corpus as the schema (see MODEL_ID_PATTERN).
                    raise ValueError("receipt_classification_invalid")
            elif name in {"old_checksum", "new_checksum"}:
                if name in null_keys:
                    # Round-1 F-C: this bucket's contract pins the field to
                    # null, so a hex value here is a forged row — the schema
                    # says the same and both must reject the same corpus.
                    if value is not None:
                        raise ValueError("receipt_classification_invalid")
                elif value is not None:
                    if (
                        not isinstance(value, str)
                        or len(value) != 64
                        or any(character not in "0123456789abcdef" for character in value)
                    ):
                        raise ValueError("receipt_classification_invalid")
            elif name == "reason":
                if reason_enum is not None and value not in reason_enum:
                    raise ValueError("receipt_classification_invalid")
            elif name == "transition_mode":
                if value not in transition_modes:
                    raise ValueError("receipt_classification_invalid")
            elif name in _SKIP_CAUSE_LIST_KEYS:
                # #1433 skip-cause evidence: bounded list of bounded strings.
                # The writer applies the same two caps.
                if (
                    not isinstance(value, list)
                    or len(value) > MAX_COLLECTION_ITEMS
                    or any(
                        not isinstance(entry, str) or len(entry) > MAX_STRING_LENGTH
                        for entry in value
                    )
                ):
                    raise ValueError("receipt_classification_invalid")
            elif isinstance(value, str):
                if len(value) > MAX_STRING_LENGTH:
                    raise ValueError("receipt_classification_invalid")
    _validate_group_totals(group, items)
