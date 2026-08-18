# production-scheduler-orchestration Specification

## Purpose
TBD - created by archiving change m20-production-multibasin-continuous-automation. Update Purpose after archive.
## Requirements
### Requirement: Backend scheduler entrypoint

The system SHALL provide a backend scheduler entrypoint that can run once or continuously and create production forecast work for all selected registered basins.

#### Scenario: one-shot scheduler pass

WHEN an operator runs the scheduler in one-shot mode
THEN it scans configured GFS/IFS cycles, resolves active basin/model candidates, records a pass summary, and exits with non-zero status only for scheduler-level failures or configured fatal candidate failures.

#### Scenario: continuous scheduler pass

WHEN the scheduler runs continuously
THEN it uses a lock or equivalent lease to prevent concurrent duplicate scans
AND it records each pass start, finish, candidate count, and selected/skipped/failed counts.

### Requirement: Full production chain orchestration

For each selected candidate, the scheduler SHALL orchestrate download, canonical conversion, forcing production, SHUD execution, output parsing, display publication, and evidence publication using existing service and worker contracts.

#### Scenario: complete candidate chain

WHEN a candidate completes successfully
THEN raw/canonical/forcing artifacts, hydro run output, parsed river timeseries, and display product state are persisted
AND the final run status is queryable by backend APIs.

#### Scenario: retired supplemental products remain absent

WHEN retired supplemental products are absent for a basin
THEN display publication still depends on parsed q_down readiness
AND does not fabricate retired supplemental products.

### Requirement: Object-store retention respects the pipeline frontier

The scheduler's end-of-pass object-store retention SHALL NOT delete cycle-scoped
artifacts (raw, canonical, forcing) or run workspaces for any cycle at or after
the pipeline's active lower bound — the minimum of (a) the earliest cycle that
still has non-terminal work in the current pass's candidate state (selected
candidates, construction-blocked candidates, and skipped candidates whose skip
reason is not in the explicit terminal set, unknown reasons counting as
non-terminal) and (b) the current discovery window's lower bound. Wall-clock
age alone SHALL NOT be a sufficient deletion criterion while an active lower
bound is derivable.

#### Scenario: catch-up cycle exempted from retention

WHEN a replay or backfill pass is catching up and a cycle older than the
wall-clock retention cutoff is at or after the active lower bound
THEN the retention plan does not select that cycle's raw, canonical, forcing,
or run-workspace entries for deletion
AND each exempted entry is recorded in the retention receipt's skipped list
with a frontier-specific reason distinct from the not-yet-expired reason
AND the receipt carries the active lower bound, its source, and the count of
frontier-protected entries, readable both in the full receipt and after
evidence-size compaction
AND exempted entries are not size-scanned.

#### Scenario: terminal cycles remain collectable

WHEN a cycle older than the retention cutoff is before the active lower bound
(its candidates are all terminal and it is outside the discovery window)
THEN retention deletes it exactly as before this change, with unchanged
freed-bytes accounting.

#### Scenario: steady-state behavior is unchanged

WHEN the configured lookback window plus the cycle lag plus twice the largest
source cycle interval fit within the retention window
THEN the retention plan is identical, key for key, to the plan produced
without frontier awareness
AND the enabled, dry-run, and forced-dry-run gates and the protected-prefix
exemptions behave exactly as before.

#### Scenario: misconfigured windows fail safe

WHEN the configured lookback window plus cycle lag exceeds the retention
window
THEN the frontier gate exempts the affected in-window cycles instead of
allowing the produce-then-delete spin
AND the drift direction is always over-retention, never over-deletion, and is
visible in the receipt's frontier block.

#### Scenario: failed run workspace preserved for diagnosis

WHEN a run's cycle is at or after the active lower bound (including cycles in
the discovery window whose upstream data is no longer available)
THEN the run workspace under runs/ is not deleted
AND the SHUD stdout/stderr logs inside it remain readable for post-mortem.

#### Scenario: no active lower bound provided

WHEN retention planning runs without an active lower bound (for example a
direct invocation outside a scheduler pass)
THEN retention falls back to the wall-clock criterion unchanged
AND the receipt's frontier block records that no bound was applied.

### Requirement: Out-of-pass deletion surfaces respect the pipeline frontier or fail closed

The manual cleanup CLI SHALL NOT delete cycle-scoped artifacts with less protection than the pass-side frontier exemption, and SHALL fail closed rather than fall back to unprotected wall-clock deletion when the frontier is unknown. It SHALL derive its active lower bound from the most recent scheduler pass evidence receipt's retention frontier block — excluding pre-execution reservation artifacts, selected by the receipt's recorded start time with a filename tie-break, and subject to a configurable freshness cap applied in both directions; a fresh receipt's explicit null bound SHALL be mirrored verbatim (the pass itself ran pure wall-clock, and the CLI is not stricter than the pass); a missing, unreadable, malformed, or stale receipt, a fresh receipt whose retention did not run (disabled or errored, leaving no frontier block), or any error while resolving or reading receipts SHALL force the cleanup into dry-run regardless of the execute flag, deleting nothing and recording a machine-readable frontier blocker reason in the cleanup CLI's output payload, with no bypass flag offered. The node-27 daily raw-retention process is an explicitly recorded exception to the protection-parity rule: it SHALL NOT adopt the receipt source — the pass receipts and journal live on node-22 private storage it cannot reach, and a cross-node frontier publication surface is out of this change's scope by recorded decision — and SHALL instead keep its display-watermark anchor while disclosing that decision: its summary SHALL carry an anchor block naming the anchor mode, the recorded decision, and the residual risk (backfill cycles older than the watermark minus the retention window are unprotected), and the process SHALL gain enabled and dry-run environment gates whose defaults preserve the current execute-only behaviour byte for byte (the removed dry-run CLI flags stay removed). The retention module's own no-bound wall-clock fallback is unchanged: the existing direct-invocation scenario continues to describe the module API's contract, and this requirement constrains the out-of-pass callers, which must now supply a bound or fail closed. The pass-side frontier requirement and receipt shape are unchanged by this requirement.

#### Scenario: catch-up cleanup exempts in-flight cycles

- **WHEN** the latest pass evidence receipt is fresh and carries an active lower bound, and the operator runs the cleanup CLI with execute against a store holding a cycle older than the wall-clock cutoff but at or after that bound
- **THEN** the cycle's directories are not deleted and the cleanup receipt records them as frontier-exempt skips, with the bound and a receipt-derived source label in its frontier block

#### Scenario: unknown frontier forces dry-run instead of unprotected deletion

- **WHEN** the cleanup CLI runs with execute and no pass evidence receipt is readable, or the latest receipt's recorded start time lies outside the freshness cap in either direction (a future-dated receipt cannot mint permanent freshness), or the receipt is malformed, lacks the frontier block, records a disabled or errored retention, or the resolution or read itself errors
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

### Requirement: Repeated identical no-progress reasons open a cross-pass evidence circuit

The scheduler SHALL detect, across consecutive fully-observed passes, a
subject that keeps reporting the same no-progress reason, and surface it
as an observe-only circuit marker — because the repeating shapes that
motivated this requirement (a permanently-classified failure re-judged
every pass, a deliberately non-convergent held reservation, a
predecessor-pending stall) never touch any retry counter, so without
cross-pass aggregation they repeat silently until a human happens to
look. A fully-observed pass is one that reaches the complete-pass
evidence write; early-exit, pre-lock, lock-contended, and
resource-limit-aborted passes neither observe nor touch the persisted
tracker state, so an aborted pass can never clear accumulated counts.
When enabled with threshold N (default 3; a non-positive threshold
disables the feature entirely, byte-for-byte preserving today's behavior
— no state file, no payload key even on the bounded-compaction path, no
log line), the tracker persists its state in a JSON file under the
evidence root (surviving the one-shot process model; enabled
fully-observed passes always rewrite the file, and a missing or corrupt
state file resets counting to empty with a distinguishing `state_reset`
marker of `"missing"` or `"corrupt"` and never fails the pass), and
observes the pass's already-assembled uncompacted evidence payload
through two adapters: candidate rows from the candidate and
blocked-candidate lists whose status is `blocked` with a non-empty
reason (skipped-candidate rows are excluded because their status remains
`selected` and they include successful skips; a row flagged
`operator_action_required` in its state evidence carries that flag into
the circuit entry as an annotation), and reserved-unbound reconcile
outcome rows keyed by action and reason class, read only when the
reconcile segment completed and its outcome key is present — an
adapter whose source is absent from the pass preserves its existing
entries instead of clearing them. Counting is strictly consecutive per
subject: the same (subject, reason) pair increments, a changed reason
resets the count to one, and a subject absent while its adapter's source
is present is cleared. A pair reaching N appears in the pass evidence
under a top-level `no_progress_circuit` block (open entries capped at 50
with a truncation count) and in one aggregated
`SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN` warning per fully-observed pass.
Under evidence byte pressure the block is the first thing shed, at every
layer: an initial serialization that exceeds the byte budget only because
of the block is retried once with the block dropped before the size
verdict stands, and the bounded-compaction rebuild likewise drops the
block before any pre-existing field is summarized or dropped — so the
size gate, every compaction stage, and the pass's terminal status are
byte-for-byte what they would be had this feature never existed; the
warning and the persisted counts are unaffected, and the absence of the
block in an over-budget pass does not mean no circuit is open. A tracker
state-file write that fails to land is surfaced, never silent: the pass's
block carries `state_write_failed: true`, a distinct
`SCHEDULER_NO_PROGRESS_CIRCUIT_STATE_WRITE_FAILED` warning is logged, and
a removable non-regular leftover temp file (a dangling symlink, an empty
directory) is deleted so the next pass self-heals; an unremovable
leftover cannot self-heal but keeps the failure surfaced on every pass —
counting is never frozen silently. The observation path as a whole
fails open: an unexpected observation error logs its own warning and
skips the block for that pass instead of failing the pass. The circuit is evidence only:
it never alters scheduling decisions, retries, terminal statuses, or the
closed reconciliation vocabularies.

#### Scenario: the same reason repeating across passes opens the circuit

WHEN a subject reports the identical no-progress reason in N consecutive
fully-observed one-shot passes over a shared evidence root
THEN the Nth pass's evidence carries a `no_progress_circuit.open` entry
for that (subject, reason) pair with its consecutive-pass count and
first/last pass identifiers, and the pass logs one aggregated
circuit-open warning

#### Scenario: progress or change breaks the streak, absence of the source does not

WHEN the subject reports a different reason, disappears from a pass
whose adapter source is present, or the pass is healthy
THEN the count resets (changed reason), the entry is cleared (absence
with source present), or no observation is produced at all (healthy
pass, empty open list, no warning) — while a pass whose adapter source
is itself absent (a failed reconcile segment, a dry run) and any
early-exit or aborted pass leave the persisted counts untouched

#### Scenario: disabling the feature preserves today's behavior

WHEN the configured threshold is zero or negative
THEN no state file is read or written, the evidence payload gains no new
key on either the plain or the bounded-compaction path, and no circuit
log line is emitted

#### Scenario: the tracker survives the one-shot process model

WHEN each pass runs in a fresh scheduler process against the same
evidence root
THEN the consecutive count accumulates across processes via the persisted
state file, enabled fully-observed passes always rewrite the file, and a
corrupt or missing state file resets counting to empty with the
corresponding `state_reset` marker instead of failing the pass

