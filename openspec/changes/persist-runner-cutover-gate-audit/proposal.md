# Persist the cutover_gate audit block in the runner refresh receipt (#1132)

## Why

R2-A1 (PR #1091) made the cutover gate auditable so that bypass runs and
gated runs are distinguishable from persisted artifacts. The design doc for
#1097 names three audit channels ("CLI summary, runner receipt, manifest
companion receipt") and `services/orchestrator/scheduler_file_providers.py:623-625`
repeats the claim — but the runner channel never persists the block: the
runner constructs `runner_cutover_gate_audit`
(`scripts/scheduler_file_provider_refresh.py:807-817`) and passes it to the
publisher (`:865`), the returned manifest receipt (which embeds the
normalized block) flows into `_provider_evidence`'s 11-field whitelist
projection (`:1601-1621`) and the block is dropped; the on-disk refresh
receipt (`:1573-1598`) has no `cutover_gate` key. Consequence: for a
routine systemd/runner refresh there is NO on-disk evidence whether the
gate ran enforced or bypassed — exactly the indistinguishability R2-A1 set
out to eliminate. Only the manual CLI summary channel actually records it.

## Decision (triage recorded)

Issue #1132 was filed needs-triage on "persist vs de-claim the channel".
This change adopts the RECOMMENDED route — **persist** — on repo-context
grounds: the design doc and code comments already claim the channel, the
alternative "只兑现一半" leaves routine runner refreshes forensically
blind, and #1141's cross-review added mutant evidence (comment on #1132)
that the strongest-looking CLI e2e provides zero normalizer coverage. The
bundled sub-decision (also per the issue's recommended route): the three
CLI stderr failure payloads route through the shared normalizer — this is
NEW audited behavior extending the three persisted channels to failure
diagnostics (stderr is not one of the #1097 requirement's persisted
channels), so it gets its own ADDED requirement rather than riding on the
existing one.

## What Changes

- `scripts/scheduler_file_provider_refresh.py`: the receipt builder
  `_receipt` gains an optional `cutover_gate` parameter; the registry
  publish path (the audit block is constructed unconditionally at `:811`,
  covering both the `:822` direct-grid and `:865` all-basin branches)
  passes its constructed audit block, and the builder persists
  `normalize_cutover_gate_audit(block)` (shared single definition point —
  import from `packages.scheduler.registry_audit`) as a top-level optional
  receipt key. Paths reached before the block is constructed (lock
  contention, provider-preimage failures) omit the key. The runtime
  receipt validator gains a matching `cutover_gate` check so schema and
  `_validate_receipt` keep rejecting the same corpus (the file's own
  stated invariant).
- `schemas/scheduler_file_provider_refresh_receipt.schema.json`: top-level
  optional `cutover_gate` object — `mode` enum
  (enforced/bypassed_allow_uncovered_cutover/not_wired), `declaration_env`
  string-or-null, `declaration_present` boolean, required all three,
  `additionalProperties: false`. Example file updated (schema currently
  has top-level `additionalProperties: false` at `:6` — without the schema
  change the key cannot land).
- `scripts/publish_scheduler_file_registry.py`: the audit block is
  normalized ONCE before the `try` (never inside an except handler), and
  the success summary (`:1243`) plus the three stderr failure payloads
  (`:1251/:1255/:1267`) all share that normalized value.
- Tests: (a) runner-level assertion that the on-disk refresh receipt's
  `cutover_gate` equals the runner-constructed three fields (enforced +
  declaration_present true AND false variants); (b) CLI wiring test —
  monkeypatch the normalizer to return a sentinel and assert the stderr
  payload carries the sentinel (pins that the failure path actually calls
  the normalizer; a value-equality assertion alone cannot, since legal
  literals are fixed points of normalization — the false-confidence
  mutant evidence on #1132); (c) schema/example validation green.

## Out of Scope

- Normalizer field-validation strength (#1131, merged).
- Cutover gate semantics, declaration schema.
- Other refresh-receipt fields, orphan/residue structures.
- The #1099 split of this file (this change intentionally lands first —
  dependency noted in the issue).

## Impact

- Affected specs: `scheduler-registry-refresh` (two ADDED requirements:
  runner receipt persists the audit block; CLI failure diagnostics carry a
  normalizer-produced block).
- Affected code: `scripts/scheduler_file_provider_refresh.py`,
  `scripts/publish_scheduler_file_registry.py`,
  `schemas/scheduler_file_provider_refresh_receipt.schema.json` (+example),
  `tests/test_scheduler_file_provider_refresh.py`,
  `tests/test_publish_scheduler_file_registry.py`.
- The #1097 design-doc three-channel sentence becomes true as written; no
  doc edits needed.
- Receipt consumers: the schema is the contract; adding an OPTIONAL key
  preserves every existing reader. The runtime validator's allowed-set IS
  exact-match (`RECEIPT_KEYS`/`RECEIPT_OPTIONAL_KEYS`, `:174`/`:1645`), so
  `cutover_gate` must join `RECEIPT_OPTIONAL_KEYS` (never `RECEIPT_KEYS`) —
  see tasks 1.2. Schema `declaration_env` carries `maxLength: 512` to
  match the runtime `MAX_STRING_LENGTH` bound.
