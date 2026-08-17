# Delta: production-scheduler-orchestration (retention-frontier-out-of-pass)

## ADDED Requirements

### Requirement: Out-of-pass deletion surfaces respect the pipeline frontier or fail closed

The manual cleanup CLI SHALL NOT delete cycle-scoped artifacts with less protection than the pass-side frontier exemption, and SHALL fail closed rather than fall back to unprotected wall-clock deletion when the frontier is unknown. It SHALL derive its active lower bound from the most recent scheduler pass evidence receipt's retention frontier block — excluding pre-execution reservation artifacts, selected by the receipt's recorded start time with a filename tie-break, and subject to a configurable freshness cap; a fresh receipt's explicit null bound SHALL be mirrored verbatim (the pass itself ran pure wall-clock, and the CLI is not stricter than the pass); a missing, unreadable, malformed, or stale receipt, a fresh receipt whose retention did not run (disabled or errored, leaving no frontier block), or any error while resolving or reading receipts SHALL force the cleanup into dry-run regardless of the execute flag, deleting nothing and recording a machine-readable frontier blocker reason in the cleanup CLI's output payload, with no bypass flag offered. The node-27 daily raw-retention process is an explicitly recorded exception to the protection-parity rule: it SHALL NOT adopt the receipt source — the pass receipts and journal live on node-22 private storage it cannot reach, and a cross-node frontier publication surface is out of this change's scope by recorded decision — and SHALL instead keep its display-watermark anchor while disclosing that decision: its summary SHALL carry an anchor block naming the anchor mode, the recorded decision, and the residual risk (backfill cycles older than the watermark minus the retention window are unprotected), and the process SHALL gain enabled and dry-run environment gates whose defaults preserve the current execute-only behaviour byte for byte (the removed dry-run CLI flags stay removed). The retention module's own no-bound wall-clock fallback is unchanged: the existing direct-invocation scenario continues to describe the module API's contract, and this requirement constrains the out-of-pass callers, which must now supply a bound or fail closed. The pass-side frontier requirement and receipt shape are unchanged by this requirement.

#### Scenario: catch-up cleanup exempts in-flight cycles

- **WHEN** the latest pass evidence receipt is fresh and carries an active lower bound, and the operator runs the cleanup CLI with execute against a store holding a cycle older than the wall-clock cutoff but at or after that bound
- **THEN** the cycle's directories are not deleted and the cleanup receipt records them as frontier-exempt skips, with the bound and a receipt-derived source label in its frontier block

#### Scenario: unknown frontier forces dry-run instead of unprotected deletion

- **WHEN** the cleanup CLI runs with execute and no pass evidence receipt is readable, or the latest receipt is older than the freshness cap, is malformed, lacks the frontier block, records a disabled or errored retention, or the resolution or read itself errors
- **THEN** the cleanup is forced into dry-run, deletes nothing, and its output payload carries a frontier blocker naming the specific reason, so the operator sees what would have been deleted without anything being deleted

#### Scenario: fresh null bound mirrors the pass

- **WHEN** the latest receipt is fresh and its frontier block records a null active lower bound
- **THEN** the cleanup proceeds with the pure wall-clock criterion exactly as the pass itself did, recording the mirrored null bound, with the receipt-derived source label carried by the cleanup payload's own frontier-source field — the retention frontier block nulls the source whenever the bound is null, by the pass-side contract this change does not touch

#### Scenario: both CLI entrypoints are covered and behave identically

- **WHEN** the same store and receipt fixtures are driven through the click entrypoint and the argparse entrypoint
- **THEN** both produce the same cleanup behaviour and receipts, and both entrypoints carry test coverage

#### Scenario: node-27 raw retention disclosure and gates

- **WHEN** the node-27 raw-retention process runs under default configuration
- **THEN** its deletion behaviour is unchanged from before this change, and its summary carries an anchor block naming the display-watermark mode, the recorded keep-watermark decision, and the residual backfill risk
- **AND** setting its enabled gate off yields a disabled summary with zero deletions, and setting its dry-run gate yields collected targets with zero tree removals

#### Scenario: pass-side retention is untouched

- **WHEN** a scheduler pass runs its end-of-pass retention
- **THEN** its frontier derivation, receipt shape, and deletion semantics are byte-identical to the pre-change behaviour, and the retention entry API's signature and defaults are unchanged
