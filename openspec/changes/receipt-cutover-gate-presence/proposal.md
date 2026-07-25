# Enforce cutover_gate presence on gated receipt outcomes (#1144)

## Why

`cutover_gate` is a runtime invariant on published / dry_run /
registry-cutover-refusal receipts (`scripts/scheduler_file_provider_refresh.py:824-831`
constructs the audit block unconditionally before `publish_registry`), but
neither validator enforces its presence: the schema
(`schemas/scheduler_file_provider_refresh_receipt.schema.json:336-380`) only
requires `registry_classification` in its two `allOf` branches, and
`_validate_cutover_gate_field` (`:1829` area) early-returns when the key is
absent. Isolated probe (issue #1144): a published receipt with the whole
`cutover_gate` block deleted passes BOTH validators. Any future regression —
a `_receipt(...)` call dropping `cutover_gate=`, a whitelist projection
swallowing it (the original #1132 cause), or a #1099 repackaging losing a
pass-through — silently reopens the forensic gap #1132 closed, with every
gate green.

## Decision (route recorded)

Adopt the issue's recommended **package deal**: (1) schema presence via the
two existing `allOf` branches (`required: ["registry_classification",
"cutover_gate"]` — no new branch), (2) runtime mirror in
`_validate_cutover_gate_field` with a new distinct rejection reason
`receipt_cutover_gate_required` (same condition as
`requires_classification`), (3) runbook migration note for pre-#1132 legacy
published receipts. The schema-only alternative is rejected: it violates the
file's own schema/validator same-corpus convention (docstring at the
validator) and leaves the real production gate (`validate_current_receipt`)
open. Additionally fold in the two #1145-deferred reconciliation pins
(issue comment): id-only refused-bucket bounds and `dry_run ⇒ refused
total == 0`. These land **runtime-only** in
`_enforce_registry_classification_reconciliation`: reconciliation
invariants are runtime-only by precedent (#1140 — cross-array sums are not
expressible in the schema without contortion), while the same-corpus
convention governs the field-shape corpus, which stays mirrored.

## What Changes

- `schemas/scheduler_file_provider_refresh_receipt.schema.json`: both
  existing `allOf` branches require `cutover_gate` alongside
  `registry_classification`.
- `scripts/scheduler_file_provider_refresh.py`
  `_validate_cutover_gate_field`: missing key on
  `outcome ∈ {published, dry_run}` or
  `reason ∈ REGISTRY_CUTOVER_REFUSAL_REASONS` raises
  `receipt_cutover_gate_required`; all other outcomes keep accepting
  absence. Docstring updated.
- `_enforce_registry_classification_reconciliation` id-only mode-keyed arm:
  refused group must be untruncated with `total == len(items) <= 1` and
  every `model_id == "__declaration__"`; `outcome == "dry_run"` requires
  `refused.total == 0` (a declaration failure always terminates
  `outcome="failed"`). Legacy no-mode arm unchanged (already rejects all
  refused entries).
- `docs/runbooks/current-production-ops.md`: rewrite IN PLACE the existing
  "升级 pre-#1132 receipt" paragraph (~:705-709) and the enable-checklist
  entry (~:717-721) — both currently frame a missing `.cutover_gate` as a
  soft operator-judgement signal, which becomes contradictory once
  `validate_current_receipt` (the `--enable` path of
  `install_node22_scheduler_file_provider_refresh.sh`) hard-rejects it as
  `emergency_record_invalid`; one SUCCESSFUL (published) manual refresh
  rewrites `latest.json` first (a refused/failed refresh also rewrites it
  but stays rejected; the refresh write path itself is not blocked —
  receipt ordering uses the lenient reader). Styled after and
  cross-referencing the existing pre-#1080 legacy paragraph (~:696-703);
  the rewritten section is the shared landing spot for sibling issue
  #1143 (rollback direction).
- Tests: presence red-proofs on both validators, refusal-reason coverage,
  early-failure absence still legal (guard `test_lock_contention_receipt_omits_cutover_gate`),
  example receipt still valid, id-only refused-bucket forgeries rejected,
  legacy-receipt `validate_current_receipt` behavior pinned (the runbook
  claim's oracle).

## Out of Scope

- `cutover_gate` field-level shape constraints (already covered).
- Gate semantics, declaration schema, `--allow-uncovered-cutover` flow.
- The manifest companion receipt and CLI summary channels (separate
  schemas, separate validators — no presence invariant asserted there).
- Rollback-direction documentation beyond the shared subsection skeleton
  (#1143 owns it).
- #1099 file splits.

## Impact

- Affected specs: `scheduler-registry-refresh` — two MODIFIED
  requirements (receipt persistence gains presence enforcement; id-only
  reconciliation gains refused-bucket bounds).
- Affected code: `schemas/scheduler_file_provider_refresh_receipt.schema.json`,
  `scripts/scheduler_file_provider_refresh.py` (two validators),
  `tests/test_scheduler_file_provider_refresh.py`,
  `docs/runbooks/current-production-ops.md`.
- `schemas/examples/scheduler_file_provider_refresh_receipt.example.json`
  already carries the key — unchanged, must stay valid.
- Tightens acceptance only for forged/legacy corpora; today's writer
  output is unaffected (runner already writes the block unconditionally).
