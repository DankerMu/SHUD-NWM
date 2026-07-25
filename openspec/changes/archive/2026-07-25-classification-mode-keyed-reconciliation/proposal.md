# Key reconciliation on classification mode, not receipt outcome (#1140)

## Why

`_enforce_registry_classification_reconciliation` selects its lenient
id-only branch by `outcome == "dry_run"`
(`scripts/scheduler_file_provider_refresh.py:2040`), but the WRITER keys
the id-only classification on the `dry_run` flag
(`_classify_registry` early-return, `:2609-2617`). The two coincide only
on the happy path. When a dry_run refresh fails AFTER the precommit gate
(readiness derivation, `provider_invalid`, …), the failure receipt carries
`outcome="failed"` plus the honest id-only classification; the validator
routes it into the full-equality branch (`:2088-2106`), and whenever the
previous canonical registry contains a model absent from the prospective
set — exactly the removal-preview scenario dry_run exists for —
`unchanged + 0 + 0 != previous_count` raises
`receipt_classification_invalid`. `_publish_primary_receipt` then fails,
the emergency slot is dropped, and the run terminates as
`primary_receipt_failed`: the TRUE failure reason is masked AND no receipt
lands on disk at all (issue #1140 probe-verified; pre-existing from
PR #1091, direction opposite to and non-overlapping with #1135).

## Decision (route recorded)

Adopt the issue's recommended route: **persist the classification mode**
(`mode: "id_only" | "full"`) on the classification block, written by
`_classify_registry` from its own `dry_run` parameter, and select the
reconciliation branch by `mode`. Alternatives rejected per the issue's own
tradeoffs: outcome-set relaxation (备选 1) weakens tamper-resistance of
REAL publish-failure receipts whose classifications are full; dropping the
classification on dry_run failures (备选 2) discards the most valuable
forensic evidence exactly when diagnosing a failed dry_run. Legacy
receipts (no `mode` key) keep the current outcome-keyed behavior — the
validator runs on untrusted on-disk receipts via
`reconstruct_primary_receipt`/`validate_current_receipt`, so the new key
must be optional at every layer.

## What Changes

- Writer: `_RegistryClassification` gains `mode` (default `"full"`);
  `_classify_registry` sets `"id_only"` on the dry_run path; `to_receipt()`
  emits it. The sink path is unchanged — every classified receipt now
  carries the mode.
- Validator: `_validate_registry_classification_field`'s exact key-set
  admits optional `mode` (when present: must be `"id_only"` or `"full"`);
  `_enforce_registry_classification_reconciliation` branches on `mode`
  when present, falling back to `outcome == "dry_run"` for legacy
  receipts. The id-only branch's refusal constraint loosens by exactly one
  reason: the writer appends the synthetic `__declaration__` refusal AFTER
  `_classify_registry` regardless of dry_run (`:2813-2824`), so an id-only
  classification may carry refused entries whose reason is
  `registry_cutover_declaration_invalid` — any other refusal reason still
  rejects, and `package_changed`/`declared_cutovers` stay pinned to zero.
  Cross-pins: `outcome == "dry_run"` with `mode == "full"` and
  `outcome == "published"` with `mode == "id_only"` are forged
  combinations and reject.
- Schema + example: `registry_classification.properties.mode` optional
  enum `["id_only", "full"]` (NOT added to `required` — legacy receipts
  must keep validating).
- Tests: reproduction case (dry_run + pending removal + post-gate injected
  failure → `outcome="failed"`, true reason, receipt persisted to
  history/latest with classification retained); regression pins for the
  full-equality branch, the #1135 dry_run constraints, and the forged
  mode/outcome combinations; legacy (mode-less) receipts keep current
  behavior both directions.

## Out of Scope

- #1135's dry_run-branch constraints themselves (kept verbatim, now keyed
  by mode with outcome fallback).
- `_classify_registry` id-only partition semantics (writer is honest).
- The post-gate failure points themselves (`:777-783`-era readiness /
  provider errors).
- `restored_previous`/`replace_uncertain` dry_run reachability beyond what
  the mode-keyed branch fixes for free (issue keeps `failed` as the
  verified primary path).
- #1098/#1099 file splits; #1143/#1144 receipt hardening follow-ups.

## Impact

- Affected specs: `scheduler-registry-refresh` — MODIFIED requirement
  (the #1135 dry_run-binding requirement re-keyed to classification mode
  with legacy outcome fallback) + ADDED requirement (mode persistence and
  dry_run-failure receipt survivability).
- Affected code: `scripts/scheduler_file_provider_refresh.py`,
  `schemas/scheduler_file_provider_refresh_receipt.schema.json` (+example),
  `tests/test_scheduler_file_provider_refresh.py`,
  `docs/runbooks/current-production-ops.md` (classification-section `mode`
  semantics + pre-#1140 absence note, mirroring the pre-#1132 precedent).
- Receipt consumers: optional key, exact key-set validator extended the
  same way #1132 extended `RECEIPT_OPTIONAL_KEYS`; rollback direction
  (old validator rejects mode-carrying receipts) is the same pre-existing
  pattern already tracked by #1143 — noted, not expanded here.
- The merged #1096 bootstrap-sum requirement
  (`openspec/specs/scheduler-registry-refresh/spec.md:118-131`) keeps its
  outcome-phrased wording: under mode routing its invariant is equivalently
  covered by the id-only branch's three zero-pins (removed/package_changed
  zero, bootstrap unchanged zero), each strictly stronger than the summed
  form — no MODIFIED delta needed, behavior unchanged.
- The live (unarchived) change
  `openspec/changes/node22-scheduler-registry-refresh/specs/.../spec.md:380-403`
  still states the unconditional previous-side equality — pre-existing
  drift introduced by #1135, not touched here (issue-scoped: this change
  only modifies the merged capability spec).
