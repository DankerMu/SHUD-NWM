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

The manual cleanup CLI SHALL NOT delete cycle-scoped artifacts with less protection than the pass-side frontier exemption, and SHALL fail closed rather than fall back to unprotected wall-clock deletion when the frontier is unknown. It SHALL derive its active lower bound from the most recent scheduler pass evidence receipt's retention frontier block — excluding pre-execution reservation artifacts, selected by the receipt's recorded start time with a filename tie-break, and subject to a configurable freshness cap applied in both directions; a fresh receipt's explicit null bound SHALL be mirrored verbatim (the pass itself ran pure wall-clock, and the CLI is not stricter than the pass); a missing, unreadable, malformed, or stale receipt, a fresh receipt whose retention did not run (disabled or errored, leaving no frontier block), or any error while resolving or reading receipts SHALL force the cleanup into dry-run regardless of the execute flag, deleting nothing and recording a machine-readable frontier blocker reason in the cleanup CLI's output payload, with no bypass flag offered. The cleanup payload SHALL disclose which evidence directory was consulted: the frontier blocker carries an `evidence_dir` key holding the absolute path actually probed (explicitly null only when the directory itself could not be resolved), and the ok path carries the same key at the payload top level alongside the frontier-source field — so a silently mis-resolved workspace root (the relative default under a wrong working directory) is distinguishable from genuinely missing evidence without reading source code. The node-27 daily raw-retention process is an explicitly recorded exception to the protection-parity rule: it SHALL NOT adopt the receipt source — the pass receipts and journal live on node-22 private storage it cannot reach, and a cross-node frontier publication surface is out of this change's scope by recorded decision — and SHALL instead keep its display-watermark anchor while disclosing that decision: its summary SHALL carry an anchor block naming the anchor mode, the recorded decision, and the residual risk (backfill cycles older than the watermark minus the retention window are unprotected), and the process SHALL gain enabled and dry-run environment gates whose defaults preserve the current execute-only behaviour byte for byte (the removed dry-run CLI flags stay removed). The retention module's own no-bound wall-clock fallback is unchanged: the existing direct-invocation scenario continues to describe the module API's contract, and this requirement constrains the out-of-pass callers, which must now supply a bound or fail closed. The pass-side frontier requirement and receipt shape are unchanged by this requirement.

#### Scenario: catch-up cleanup exempts in-flight cycles

- **WHEN** the latest pass evidence receipt is fresh and carries an active lower bound, and the operator runs the cleanup CLI with execute against a store holding a cycle older than the wall-clock cutoff but at or after that bound
- **THEN** the cycle's directories are not deleted and the cleanup receipt records them as frontier-exempt skips, with the bound and a receipt-derived source label in its frontier block

#### Scenario: unknown frontier forces dry-run instead of unprotected deletion

- **WHEN** the cleanup CLI runs with execute and no pass evidence receipt is readable, or the latest receipt's recorded start time lies outside the freshness cap in either direction (a future-dated receipt cannot mint permanent freshness), or the receipt is malformed, lacks the frontier block, records a disabled or errored retention, or the resolution or read itself errors
- **THEN** the cleanup is forced into dry-run, deletes nothing, and its output payload carries a frontier blocker naming the specific reason and the `evidence_dir` it probed (null only when the directory could not be resolved at all), so the operator sees what would have been deleted and where the CLI looked without anything being deleted

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
- **THEN** nothing in this requirement changes its frontier derivation, receipt shape, or deletion semantics: this requirement constrains the out-of-pass callers only, and any later change to the pass-side contract is governed by the "Object-store retention" requirements rather than by this one
- **AND** an out-of-pass caller SHALL NOT delete a surface the pass itself would not delete under the same configuration

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

### Requirement: An unreadable warm-start env toggle never enables the terminal-skip shortcut

The scheduler SHALL treat the `NHMS_REQUIRE_FORECAST_WARM_START` compat
toggle as three-valued — explicitly enabled, explicitly disabled
(including unset, which parses to the default of disabled), or unreadable
(the orchestrator env config failed to parse for any reason, related to
the flag or not) — and SHALL allow the completed-cycle terminal-skip
shortcut only when the toggle is explicitly disabled, because collapsing
"the check could not be completed" into "the check answered no" silently
short-circuits the §8 gating decision for a journal-complete cycle and
leaves the underlying env typo unattributable: on the db-free main path
the pass then crashes at some later unguarded env read with no clue which
variable failed, and on the predecessor-backfill path a journal-complete
predecessor is silently admitted (the swallow site already records the
error's type name into emission evidence — what is missing is the
variable-level attribution and the fail-closed disposition). An
unreadable toggle first logs one
`SCHEDULER_WARM_START_ENV_UNREADABLE` warning per scheduler instance
carrying the parse error (the root-cause env is readable straight from
the log), then takes the strict warm-start path. On the strict-path
branches that read the env again (the legacy landing and the
warm-continue / blocked-predecessor tail) the same parse failure
re-raises — a loud, attributable failure consistent with how every other
`OrchestratorConfig.from_env()` call site propagates; the early-return
decision branches that never read the env return their evidence with
only the warning. Either shape is acceptable and neither is a silent
skip; no degraded parallel mode is invented for the unreadable state. On the
backfill path the same change applies when the predecessor's strict
evaluation lands on an env-re-reading branch: the swallowed error becomes a
`predecessor_gate_failed` skip with the warning already logged — fail-closed
instead of silently admitting (a predecessor landing on a ready-class
early-return branch keeps its pre-change admitted outcome; one landing on a
block-class early-return branch tightens from admitted to blocked — also
fail-closed). This backfill
contract is pinned at the emitter seam: in a live pass the candidate loop's
own unguarded env read fails the pass before the emitter runs, so the
silent-admission shape is constructible only by driving the emitter
directly. Explicit values preserve
today's behavior byte-for-byte: explicitly disabled plus a
durably-complete pipeline still terminal-skips (the D8.9 compat flow),
and explicitly enabled still emits §8 evidence with no new logging.

#### Scenario: an unrelated env typo fails loudly and attributably instead of silently skipping

WHEN the orchestrator env config fails to parse (for example an unrelated
`FORECAST_HORIZON_HOURS=abc`) while a candidate's pipeline is
journal-complete on the db-free path
THEN the terminal-skip shortcut is not taken, the strict warm-start path
is entered and — on a branch that reads the env again — the parse
failure surfaces as a raised error (an early-return branch instead
returns its evidence), and one warning naming the parse failure was
logged first — the operator can read the broken variable from the log
instead of guessing

#### Scenario: the backfill path fails closed instead of silently admitting

WHEN the same unreadable env occurs on the predecessor-backfill path with a
journal-complete predecessor whose strict evaluation lands on an
env-re-reading branch
THEN the strict-path error is recorded as a predecessor-gate failure
(skipping, not admitting, the predecessor) and the unreadable-toggle
warning has been logged

#### Scenario: explicit values keep the compat behavior

WHEN the toggle parses successfully
THEN explicitly disabled (or unset) plus a journal-complete pipeline
still takes the terminal-skip shortcut, explicitly enabled still takes
the strict path, and no unreadable-toggle warning is logged

### Requirement: Run-workspace deletion recognizes only canonical run identities

Retention SHALL admit a `runs/` workspace directory into deletion
adjudication only when its name matches a canonical run-id shape — the
forecast shape, the cycle-cohort shape, or the analysis shape (whose
cycle is its start timestamp), defined once and shared with the
journal's parsers — and the cycle token at the canonical position parses
as a real `%Y%m%d%H` timestamp; any directory name matching none of the
canonical shapes is skipped as unparseable and preserved. The previous
criterion — scan underscore-separated tokens and delete on the first one
that happens to parse as a timestamp — treated any stray directory
containing a ten-digit token (a manual salvage capture, a debugging
snapshot, a foreign writer's output) as an expired run workspace, and
could bind a forecast run to the wrong embedded timestamp. The shape
check does not validate the source segment against the closed source
set, so a stray name that happens to imitate a full canonical shape is
still collected — the over-acceptance surface is sharply narrowed, not
closed. Recognized runs keep today's three-tier adjudication (retention
window, frontier exemption, deletion) byte for byte.

#### Scenario: non-run directories are preserved instead of deleted

WHEN retention scans a `runs/` directory whose name does not match a
canonical run-id shape (for example `manual_salvage_2020010100_keepme`)
THEN the directory is skipped as unparseable and preserved, where the
previous token-scan criterion would have planned it for deletion

#### Scenario: the cycle token is taken from the canonical position

WHEN a forecast run directory name contains an additional
timestamp-like token before the cycle position
THEN the cycle is taken from the canonical position, not the first
parseable token

#### Scenario: legitimate runs are still collected

WHEN retention scans expired forecast-shaped, cycle-cohort-shaped
(optional suffix included), and analysis-shaped run directories
THEN all three remain eligible for collection, with the same cycle
binding as before the change (the analysis shape binds to its start
timestamp, matching both the analysis lane's own cycle_time and the
previous criterion's value)

### Requirement: Orchestration dispatch failures record an attributable traceback tail in run evidence

The model run evidence SHALL carry, whenever the scheduler's
orchestration dispatch catch-all converts an unexpected exception into
per-candidate submission failures, a truncated traceback tail alongside
the sanitized error message — sufficient to attribute the raising frame
(file and line), passed through the same evidence-safety sanitization
as the message — so an occasional production failure can be located
from evidence alone instead of guessed from a bare message string.

#### Scenario: an unexpected orchestration exception is attributable

WHEN `orchestrate_cycle` raises an unexpected exception during a
scheduler pass
THEN every affected candidate's run evidence records the sanitized
message AND a truncated, sanitized traceback tail naming the raising
frame, and the evidence remains schema-compatible for existing consumers

### Requirement: Retention covers every configured run-workspace root

The scheduler's end-of-pass retention SHALL be able to reclaim aged `runs/`
workspaces under run-workspace roots other than the object-store root — the
scheduler workspace root and the object-store copyback root — using an
independent retention window, while leaving every non-`runs/` prefix on those
additional roots untouched.

Additional-root coverage SHALL be gated by an explicit enablement switch whose
default is disabled, so that adopting this capability is an operational decision
separate from shipping it. When the switch is disabled, the retention plan SHALL
be identical, key for key, to the plan produced before this capability existed.

Additional roots SHALL be swept `runs/`-only. Cycle-scoped prefixes (`raw`,
`canonical`, `forcing`) on an additional root SHALL NOT be selected for deletion
under any configuration, because the copyback root's `forcing/` tree is the
node-27 display API's live disk-only serving surface: its queryable window is
exactly the set of cycle directories retained there, so reclaiming it on the
scheduler's window would silently shrink the display's history.

Every configured retention root SHALL pass the same pre-resolution admission
rules. An unset primary or additional root SHALL be a no-op. An explicitly empty
or blank primary root SHALL be rejected as `primary_root_blank`, and a relative
primary root SHALL be rejected as `primary_root_not_absolute`, before `Path`
construction or resolution. The scheduler pass SHALL preserve and hand retention
the constructor-time raw primary-root value before scheduler configuration
normalizes it; the cleanup CLI SHALL hand retention the raw environment value.
Thus no deletion surface can be derived from the process working directory or the
scheduler workspace. A built-in default SHALL NOT become an additional deletion
root, and the existing `extra_root_not_absolute` reason remains stable.

The admitted resolved root set SHALL contain no duplicate or pair whose potential
retention target trees intersect. Every root's potential targets include
`runs/<canonical_run_id>/**`; the primary root's potential targets additionally
include `raw|canonical|forcing/<source>/<valid_cycle>/**`. A root at or below one
of another root's potential target trees SHALL conflict even when the target is
not currently expired or present. Directory ancestry outside those lanes SHALL
NOT conflict: a parent workspace and child object-store with disjoint `runs/` and
cycle-prefix trees SHALL both be admitted. The primary root SHALL take precedence
over a conflicting additional root; among additional roots, the first accepted
configured root SHALL take precedence. Equal aliases SHALL retain the existing
silent single-sweep deduplication behavior, while an unequal conflicting root
SHALL be rejected as `root_overlap` with a `conflicting_root` field naming the
accepted winner. No rejected root SHALL contribute a plan entry or freed-byte
count.

The retention receipt SHALL make every entry attributable: its schema version
remains `nhms.production_scheduler.retention.v2`, each planned, deleted, skipped,
and failed entry carries the absolute root it belongs to, and the receipt carries
a block naming the additional-root switch state, window, cutoff, and admitted
resolved additional roots. Entry keys remain root-relative, so the root field disambiguates
identically named runs across roots. Root-admission failures SHALL be represented
in the existing skipped evidence without weakening the v2 contract.

The additional-root block SHALL survive scheduler evidence size compaction, so a
pass large enough to have its per-entry retention detail stripped still discloses
which window governed the additional roots. Deletion failures on an additional
root SHALL be recorded per entry and SHALL NOT abort the sweep or the pass,
including failures raised as safe-filesystem errors rather than OS errors.

Deletion on an additional root SHALL stay inside that root. An additional root
whose `runs/` entry is a symbolic link SHALL be skipped with a recorded reason
rather than followed. Once an ordinary directory `runs/<canonical_run_id>` has
been selected as expired, retention SHALL remove the whole workspace, unlinking
any descendant symbolic-link entries without following them. Symbolic-link
targets SHALL remain untouched. A completed removal SHALL leave no workspace to
be selected or failed again on the next pass; an actual removal error SHALL retain
the existing per-entry failed semantics and count no freed bytes for that entry.

The adjudication order, the pipeline-frontier exemption, the protected-prefix and
static-segment protections, the published-artifact protection, and the contract
that cleanup never aborts scheduling SHALL all apply unchanged.

#### Scenario: additional root reclaims its aged run workspaces

- **WHEN** additional-root coverage is enabled and the scheduler workspace root differs from the object-store root
- **AND** the workspace root holds `runs/<canonical_run_id>` whose cycle is older than the additional-root cutoff and older than the active lower bound
- **THEN** retention selects that run workspace for deletion, reclaiming its
  `input/`, `output/`, `logs/`, and `state_checkpoint_recovery/` contents
- **AND** the receipt entry names the workspace root as its root.

#### Scenario: cycle-scoped prefixes on an additional root are never selected

- **WHEN** additional-root coverage is enabled and an additional root holds
  `raw/`, `canonical/`, or `forcing/` trees with cycle directories older than
  every configured cutoff
- **THEN** retention selects none of them, in plan or in deletion
- **AND** the node-27 display's disk-resident forcing history on that root is
  unaffected.

#### Scenario: the two retention windows are independent

- **WHEN** a run workspace with the same cycle exists under both the object-store
  root and an additional root
- **AND** that cycle is older than the object-store window's cutoff but still
  within the additional-root window
- **THEN** retention deletes the object-store root's copy
- **AND** records the additional root's copy as retained inside its window.

#### Scenario: disabled switch preserves the previous plan exactly

- **WHEN** additional-root coverage is disabled
- **THEN** the retention plan, deletions, skips, and freed byte total are
  identical to the behaviour before this capability, key for key
- **AND** no additional root is scanned.

#### Scenario: invalid primary root never becomes a derived deletion root

- **WHEN** the direct API, scheduler-pass constructor/environment, or cleanup-CLI environment supplies `OBJECT_STORE_ROOT` as `""`, whitespace, or `"relative/store"`
- **AND** aged retention-shaped trees exist under CWD and under the location scheduler normalization would derive beneath the workspace
- **THEN** blank values record `primary_root_blank` and the relative value records `primary_root_not_absolute`
- **AND** neither location is scanned, planned, or removed and the physical trees remain intact.

#### Scenario: coincident roots are swept once

- **WHEN** additional-root coverage is enabled and every configured root resolves
  to the same absolute path
- **THEN** each target appears exactly once in the plan
- **AND** the freed byte total counts each reclaimed directory exactly once.

#### Scenario: intersecting retention target trees are rejected deterministically

- **WHEN** primary A and additional B, or two additional roots A and B in either configuration order, place one root at or below another root's `runs/<canonical_run_id>` potential target tree
- **OR** an additional root lies at or below primary A's `raw|canonical|forcing/<source>/<valid_cycle>` potential target tree
- **THEN** the primary wins, or the first accepted additional wins when no primary is involved
- **AND** the loser is omitted from scanning and records `root_overlap` with `conflicting_root` naming the winner
- **AND** no loser target or duplicate freed-byte contribution is produced.

#### Scenario: directory ancestry with disjoint retention lanes is admitted

- **WHEN** a configured workspace root A is the parent of primary object-store `A/object-store`, or one additional root is an ordinary child of another outside every canonical run target
- **AND** each root has an aged canonical workspace under its own `runs/` tree
- **THEN** both roots are admitted and their targets are planned and removed independently exactly once
- **AND** no `root_overlap` skip or duplicate freed-byte contribution is produced.

#### Scenario: window attribution survives evidence compaction

- **WHEN** a pass selects enough additional-root targets that its retention
  evidence is size-compacted and per-entry detail is stripped
- **THEN** the compacted retention block still carries the additional-root block
  naming the switch state, window, cutoff, and resolved roots
- **AND** it still carries the pipeline-frontier block.

#### Scenario: the pass forwards its configured additional roots

- **WHEN** a scheduler pass runs its end-of-pass retention with additional-root
  coverage enabled, and both the scheduler workspace root and the object-store
  copyback root are explicitly configured
- **THEN** both non-conflicting roots are among the admitted resolved additional
  roots recorded in the receipt
- **AND** an aged run workspace under either of them is selected, attributed to
  the root it belongs to.

#### Scenario: an additional root defaulted rather than configured is not swept

- **WHEN** the scheduler workspace root is not explicitly configured, so its
  value comes from the built-in default
- **THEN** it is not forwarded as an additional root, and nothing beneath it is
  selected for deletion
- **AND** the admitted resolved additional roots recorded in the receipt do not
  include it.

#### Scenario: a relative additional root is discarded, not resolved

- **WHEN** an additional root is configured with a relative value
- **THEN** it is discarded with a recorded reason and never resolved
- **AND** no directory under the process working directory is selected for
  deletion.

#### Scenario: an unset or blank additional root is discarded, not resolved

- **WHEN** an additional root is configured as unset, empty, or blank
- **THEN** it is discarded before any path resolution and never appears among the
  admitted resolved additional roots
- **AND** no directory under the process working directory is selected for
  deletion.

#### Scenario: a failed removal on an additional root is isolated

- **WHEN** removing one selected run workspace on an additional root fails,
  whether with an OS error or a safe-filesystem error
- **THEN** that entry is recorded as failed with a readable reason
- **AND** the remaining selected entries are still removed, the freed byte total
  counts only successful removals, and neither the sweep nor the pass aborts.

#### Scenario: a symlinked runs directory does not extend the deletion surface

- **WHEN** additional-root coverage is enabled and an additional root's `runs/`
  entry is a symbolic link pointing outside that root
- **THEN** retention skips that root with a recorded reason and selects nothing
- **AND** no path outside the resolved root appears in the plan or is removed.

#### Scenario: descendant links are unlinked without following

- **WHEN** a selected additional-root run workspace contains top-level and nested symbolic links to targets outside the root
- **THEN** retention unlinks the links, removes the complete run workspace in that pass, and leaves every target byte-identical
- **AND** a second pass neither plans nor fails that removed workspace.

#### Scenario: a missing additional root is a silent no-op

- **WHEN** additional-root coverage is enabled and a configured admitted
  additional root does not exist, or exists without a `runs/` directory
- **THEN** retention records no targets for it and raises nothing
- **AND** the scheduling pass completes normally.

### Requirement: Reconciliation-pending candidates are partial non-success evidence

Scheduler candidate evidence SHALL treat cycle terminal `reconciling` and stage/candidate statuses `submit_result_ambiguous` and `reconcile_unverified` as incomplete reconciliation outcomes. Such evidence SHALL be partial and non-successful, but SHALL NOT manufacture a failed candidate, a confirmed submission, or a proven absence of submission.

Submission confirmation and submit-call provenance SHALL remain separate. A confirmed Slurm identity makes `submitted=true` and `slurm_submit_called=true`. When the chain producer has crossed the gateway boundary and durably recorded an accepted-submit ambiguous result but has no confirmed Slurm identity, candidate evidence SHALL keep `submitted=false` while carrying `slurm_submit_called=unknown_after_attempt`; execution/no-mutation proofs and bounded evidence SHALL preserve the same uncertainty and SHALL NOT emit `slurm_submit_proven_absent=true`. A reconciliation status token without producer-owned gateway-attempt provenance SHALL NOT by itself manufacture either positive or unknown submit evidence.

#### Scenario: Reconciling cycle candidate cannot report final success

- **WHEN** a cycle-derived candidate remains active while its cycle terminal is `reconciling`
- **THEN** the candidate status SHALL be `reconciling`
- **THEN** `final_candidate_success` SHALL be false
- **THEN** the candidate SHALL contribute to producer `partial_count`
- **THEN** it SHALL NOT contribute to `failed_count`

#### Scenario: Stage reconciliation statuses share the non-success classifier

- **WHEN** candidate evidence carries `submit_result_ambiguous` or `reconcile_unverified`
- **THEN** the same non-success predicate SHALL reject final success
- **THEN** existing failed-status classification SHALL remain false for both statuses

#### Scenario: Confirmed first dispatch survives same-cycle pending projection

- **GIVEN** a scheduler candidate's initial full-array stage has a confirmed Slurm master job identity
- **WHEN** either a nested partial retry or an outer whole-array retry ends reconciliation-pending and the pass artifact is produced
- **THEN** the candidate model-run evidence SHALL retain `submitted=true` and `slurm_submit_called=true`
- **THEN** execution proof SHALL retain a positive `submitted_count` and `slurm_submit_count`
- **THEN** `slurm_submit_proven_absent` SHALL be false and no-mutation proof SHALL NOT claim `slurm_submit_called=false`
- **THEN** evidence compaction SHALL preserve those facts

#### Scenario: Multi-hop retry history preserves confirmed submission proof

- **GIVEN** a scheduler candidate's current stage confirmed a Slurm master before one or more empty-ID same-stage retry results
- **WHEN** the final retry ends reconciliation-pending without its own Slurm identity and the pass artifact is produced
- **THEN** model-run and execution proof SHALL retain the earlier confirmed submission facts
- **THEN** persisted and bounded evidence SHALL keep a positive submit count and `slurm_submit_proven_absent=false`
- **THEN** raw retry metadata and durable rows SHALL remain attributed to their original attempts

#### Scenario: Gateway-crossed bare ambiguity preserves unknown submit-call provenance

- **GIVEN** a candidate's first submission reaches the gateway and the accepted-submit producer durably records `submission_ambiguous`
- **WHEN** the result has no confirmed Slurm identity and the pass artifact is produced
- **THEN** model-run evidence SHALL carry `submitted=false` and `slurm_submit_called=unknown_after_attempt`
- **THEN** execution proof SHALL carry zero confirmed submits, a nonzero unknown-submit count, `slurm_submit_outcome=unknown_after_attempt`, `slurm_submit_proven_absent=false`, and `mutation_outcome=unknown_after_attempt`
- **THEN** no-mutation proof, persisted evidence, and bounded compaction SHALL preserve `slurm_submit_called=unknown_after_attempt`

#### Scenario: Pending status without attempt provenance remains proven no-submit

- **WHEN** a hand-built or replayed scheduler candidate has a reconciliation-pending status but carries neither a confirmed Slurm identity nor producer-owned gateway-attempt provenance
- **THEN** model-run evidence SHALL keep `submitted=false` and `slurm_submit_called=false`
- **THEN** execution proof SHALL keep `slurm_submit_count=0` and `slurm_submit_proven_absent=true`, and no-mutation proof SHALL retain false/proven-absent submit evidence
- **THEN** persisted and bounded evidence SHALL NOT turn the pending token itself into positive or `unknown_after_attempt` submission proof

#### Scenario: True no-submit remains proven absent

- **WHEN** a candidate reaches a failed or blocked result before the gateway submission boundary and carries no confirmed Slurm identity
- **THEN** submit-call evidence SHALL remain false, execution proof SHALL retain `slurm_submit_proven_absent=true`, and bounded evidence SHALL preserve that control

### Requirement: Operators can atomically demote a manually verified-dead comment-unobservable reservation

The file-journal scheduler SHALL expose a row-scoped operator CLI that converts a current accepted-submit cohort master from the exact held state (`status=reserved`, no bound or matched Slurm job id, `submit_outcome=submit_result_ambiguous`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`) to `status=reservation_lost` with the distinct `operator_verified_absence` decision only when the operator supplies explicit confirmation, operator identity, a timezone-aware check time, a bounded non-empty verification note, and exact persisted submission-attempt and attempt-anchor expectations. The transition SHALL execute under the cycle lock, reject every stale or mismatched request without changing journal bytes, clear the post-state reason class, and atomically append the cohort master, eligible active member failure projections, and a durable audit event containing the operator evidence and prior accounting blocker. That authority append is the operation's commit point. A later direct/latest derived-projection failure SHALL NOT turn the committed demotion into a reported failure: the command SHALL return committed success with bounded non-secret projection warnings, while journal replay remains authoritative and a repeated request remains a zero-write CAS refusal. The command SHALL be file-journal-only and SHALL behave identically through the click and argparse entrypoints.

#### Scenario: Exact confirmed request records one audited demotion

- **WHEN** an operator has independently verified absence and invokes `demote-reserved-job` with all required confirmation, operator, attempt, and anchor values matching the exact held master
- **THEN** the command exits zero with a stable JSON receipt, the master becomes `reservation_lost/operator_verified_absence` with a null reason class, matching active member rows are projected to `failed/SLURM_RESERVATION_LOST`, and one operator audit event records the prior blocker and verification evidence in the same durable append

#### Scenario: Missing confirmation or evidence is rejected before writing

- **WHEN** `--confirm`, operator identity, timezone-aware check time, or the bounded verification note is missing or invalid
- **THEN** both CLI entrypoints exit non-zero, report the validation error, and leave the journal byte-identical

#### Scenario: Stale or wrong durable state loses the compare-and-swap

- **WHEN** the job id does not name a current master, or any current status, binding, outcome, source, decision, reason class, submission attempt, or attempt anchor differs from the supplied held-row expectation
- **THEN** the command exits non-zero and writes no master, member, event, sequence, or materialized-latest record

#### Scenario: Concurrent successor cannot be demoted

- **WHEN** another actor binds, permits, releases, demotes, or reclaims the reservation before the operator transition obtains the cycle lock
- **THEN** the locked re-read detects the changed authority state and the stale operator request writes nothing

#### Scenario: State and audit evidence fail together before commit

- **WHEN** validation or append of any master, member, or audit-event record fails before the authority batch commit
- **THEN** neither the operator decision nor any partial member/event evidence becomes durable

#### Scenario: Derived projection failure after commit is reported as committed

- **WHEN** the authority batch commits and a later direct-job or latest materialization write fails
- **THEN** the command still reports the demotion as committed, carries a bounded non-secret warning naming each failed projection, attempts the remaining independent projections, and does not append another operator decision when the same request is retried

#### Scenario: Automatic fail-closed behavior remains unchanged

- **WHEN** reconcile runs on a cluster that does not store job comments and no operator command is invoked
- **THEN** it continues to record `accounting_unavailable/comment_accounting_unproven`, keeps the row `reserved`, and never infers absence from the empty comment query

#### Scenario: PostgreSQL and manual retry surfaces do not gain this authority

- **WHEN** a caller uses the PostgreSQL repository, the HTTP/manual retry API, or a generic file-journal evidence transition
- **THEN** no `operator_verified_absence` demotion capability is exposed and `reserved` remains outside the manual-retry source statuses

#### Scenario: Release validation does not manufacture a production incident

- **WHEN** a read-only census of the active production file journal finds no naturally occurring master in the exact held pre-state
- **THEN** release evidence SHALL use the deterministic held-to-reclaim chain, fault/refusal matrix, final-head CI, and recorded census; it SHALL NOT stop the production scheduler, force gateway unavailability, inject or rewrite journal authority, or submit a real cohort merely to create a live receipt

#### Scenario: A natural held-row incident retains an in-situ receipt

- **WHEN** a naturally occurring exact held master is independently confirmed dead with name/time/user/account `sacct` and `squeue` evidence and the guarded command is used operationally
- **THEN** the incident record SHALL retain the success receipt and durable audit event, a stale or repeated zero-write refusal, the fresh reclaim attempt and anchor, exactly one cohort resubmission, and the cleanup boundary

#### Scenario: Non-dedicated accepted-submit writers cannot persist the operator decision

- **WHEN** the submit-attempt commit writer receives an accepted transition carrying `operator_verified_absence`, the cohort defer or cohort task-projection writer receives the raw decision token, ordinary pipeline-job upsert receives the token while creating or upgrading a current-contract row, the legacy `transition_pipeline_job_submit_evidence` path receives a transition carrying the token, or `record_pipeline_job_reconciliation` receives the token
- **THEN** every non-dedicated writer rejects it with `file_journal_authority_transition_requires_typed_api` before row construction, lock acquisition, durable mutation, or event, the journal stays byte-identical, and existing legitimate current-contract decisions, legacy decisions, and non-token legacy upgrades still apply unchanged

#### Scenario: Committed reclaim never strands a pre-sbatch live reservation

- **WHEN** the public old-ID operator recovery path commits the reclaim authority append and a derived direct or inventory projection write then fails before any submission
- **THEN** the failure SHALL NOT be reported as an uncommitted failure that leaves a live `reserved` row: the flow either completes the single submission path or transitions the row to a non-live retryable authority state under the lock, and the next public pass does not fail with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: The receipt locator uses the one safe journal-root authority

- **WHEN** `--journal-root` is a symlink loop or a literal unexpanded tilde path
- **THEN** the loop root fails through the typed operational error path before the authority append with no traceback and zero journal bytes, and the tilde root's success receipt locator equals the expanded authority root actually used by repository reads and writes

### Requirement: Hydro run status sets have one definition and a parity lock

The hydro-run durable-success status set SHALL be one shared object defined in `scheduler_state_types`, consulted by the scheduler's candidate decision, by the SQL completed-pipeline probe, by the file journal's completed-pipeline probes, by the forecast trigger's completion check, and by the durable-output predicate; the names `COMPLETED_HYDRO_STATUSES` on `chain` and `chain_repository` SHALL remain importable and SHALL bind to that same object. The hydro-run error-code-clearing status set consulted by the file journal's status write and by the recorded-failure-code reader SHALL likewise be one shared `frozenset` defined in `scheduler_state_types`, with the reader's private name kept as an alias to it.

The hydro-run active status set SHALL be one shared object defined in `scheduler_state_types` holding exactly `created`, `staged`, `pending`, `submitted` and `running`; the names `ACTIVE_HYDRO_STATUSES` on `chain` and `chain_repository` SHALL remain importable and SHALL bind to that same object, and the file journal SHALL import the name from `scheduler_state_types`. `pending` is an active status because it is the value manual retry writes to `hydro_run` once the retry job is submitted, and the scheduler candidate decision and the manual-retry blocker lane already treat it so. The SQL active-pipeline probe's hydro arm SHALL therefore match `pending`, so that a `hydro_run` row at `pending` whose cycle has no `pipeline_job` row answers "active" on the database lane as it does on the decision lane. The file journal's active-pipeline probe SHALL match `pending` under its existing candidate-scoped terminal-completion suppression (#1472), which is NOT mirrored by the SQL probe and is unchanged; and the file journal's attempt-scoped write paths — submit-attempt rejection, retry permission and operator-verified demotion — SHALL treat a `pending` member row of the affected attempt exactly as they treat `created`, `staged`, `submitted` and `running` rows (rewrite to `failed` with the path's error code), and the cohort task projection SHALL treat a `pending` row as retryable so a reconciled succeeded task rewrites it to `succeeded` and a reconciled failed task rewrites it to `failed` with the task's error code. Repairing a stale `pending` row on the database lane is not part of this requirement.

Every member of the hydro-run active, durable-success and code-clearing sets other than `"complete"` SHALL be a member of the `hydro.run_status` enum as declared by the migrations, where the parity lock SHALL derive that member table by sweeping every migration file for the enum's `CREATE TYPE` and `ADD VALUE` statements with the type identifier written bare or double-quoted on either segment, SHALL fail closed on any `RENAME VALUE` or `RENAME TO` statement for the type because it does not model renames, and a migration change SHALL select the parity lock in CI. `"complete"` SHALL stay in the durable-success and code-clearing sets as the one named exception: it is unreachable on the database lane (closed enum) and is not produced by any production writer on the file-journal lane, but that lane does not validate `hydro_run.status` and its test construction face uses it, so removing it would change file-journal decisions. The membership of the durable-success and code-clearing sets SHALL be unchanged by this consolidation.

#### Scenario: Aliases are the same object

- **WHEN** `chain.COMPLETED_HYDRO_STATUSES`, `chain_repository.COMPLETED_HYDRO_STATUSES`, the file journal's imported `COMPLETED_HYDRO_STATUSES`, the scheduler decision module's imported `DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain_forecast_trigger._completed_hydro_statuses()` are compared by identity with `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain.ACTIVE_HYDRO_STATUSES`, `chain_repository.ACTIVE_HYDRO_STATUSES` and the file journal's imported `ACTIVE_HYDRO_STATUSES` are compared by identity with `scheduler_state_types.ACTIVE_HYDRO_STATUSES`
- **THEN** each `is` the same object, and `scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES` and the file journal's imported name `is` `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`, which is a `frozenset` of six members

#### Scenario: SQL active probe counts a pending hydro run

- **WHEN** `PsycopgOrchestratorRepository.has_active_pipeline` is called for a source, cycle and model whose `hydro.hydro_run` row has `status = 'pending'` and no `ops.pipeline_job` row exists for the cycle at all
- **THEN** the bound hydro-status parameter contains `pending` and, against a real Postgres, the probe returns `True`

#### Scenario: Inline consumer consults the shared durable set

- **WHEN** a sentinel status is added to the shared durable-success set for the duration of a probe
- **THEN** `_durable_shud_output_exists({"hydro_status": sentinel})` returns `True`, and after the sentinel is discarded it returns `False`

#### Scenario: A fake member turns the lock red

- **WHEN** any alias site is rebound to a fresh literal, or a status outside the `hydro.run_status` enum other than `"complete"` is added to the durable-success set
- **THEN** the parity test fails

#### Scenario: Quoted identifiers and renames in migrations

- **WHEN** a migration adds a value with the type written as `hydro."run_status"` or `"hydro"."run_status"`
- **THEN** the sweep counts the value exactly as for the bare identifier
- **WHEN** a migration renames a value of the type or renames the type
- **THEN** the parity lock fails with a message naming that migration file and stating that the oracle does not model renames

#### Scenario: Journal lane counts a pending hydro run

- **WHEN** a file-journal latest view carries a candidate-matching `hydro_run` at `pending` and no pipeline-job rows
- **THEN** the journal's `has_active_pipeline` returns `True`
- **WHEN** a cohort member's hydro row is `pending` at the attempt that `permit_pipeline_job_retry` marks lost, or a `pending` row meets a reconciled `succeeded` task in `project_forecast_cohort_tasks`
- **THEN** the row is rewritten to `failed` / `SLURM_RESERVATION_LOST` in the first case and to `succeeded` in the second (a reconciled `failed` task would rewrite it to `failed` with the task's error code), exactly as a `running` row would be

#### Scenario: Decision-lane answers and every non-pending answer are unchanged

- **WHEN** the scheduler, chain, retry and journal suites run against the consolidated sets
- **THEN** every existing assertion passes with no assertion edits other than the SQL-parameter pin that gains `pending`, including the fixtures that write `hydro_status="complete"` on the file-journal lane

### Requirement: A journal-predecessor identity quarantine retry SHALL consult the per-model forcing witness

The scheduler SHALL consult the per-model forcing witness for quarantine retries:
when the journal predecessor identity quarantine replaces a completed-type skip
with a retry that restarts at `forecast`, the scheduler SHALL consult the same
upstream-artifact guard the strict warm-start lane uses for the candidate's own
`(source, cycle, basin_version_id, model_id)` before that retry can be emitted,
on every lane where no later consultation runs. When no forcing witness is
found, the decision SHALL be the existing stable missing-forcing blocker (reason
`missing_forcing_package_uri` or `forcing_version_row_absent`), which satisfies
the stable missing-forcing blocker contract; the guard's existing copyback leg
MAY instead yield its own named copyback blocker. The quarantine
circuit-breaker `blocked` exit submits no work and SHALL keep its reason absent
from every forced-resubmit whitelist. A `manual_retry_requested` decision SHALL
NOT be rewritten by this consultation.

Automatic re-entry of the forcing stage for a candidate that landed in the
stable missing-forcing blocker SHALL NOT be emitted by any unattended scheduler
lane; such candidates are drained by operator action (forcing backfill for the
renamed model identities, and — on the strict warm-start lane only — the explicit
single-cycle repair authorization).

#### Scenario: Quarantine retry without own forcing blocks

- **WHEN** a candidate's completed-type skip is quarantined for a stale journal
  predecessor identity, no strict warm-start evidence is present, and no forcing
  witness exists for the candidate's own model
- **THEN** the candidate SHALL be `blocked` with reason
  `missing_forcing_package_uri` or `forcing_version_row_absent`
- **AND** the evidence SHALL carry the forcing provenance
- **AND** no forecast work SHALL be submitted

#### Scenario: Quarantine retry with own forcing is unchanged

- **WHEN** the same quarantine fires and the candidate's own forcing package is
  witnessed
- **THEN** the retry decision, its reason, its `restart_stage: "forecast"`, and
  the submission SHALL be unchanged

#### Scenario: The quarantine blocker is drainable

- **WHEN** a quarantine retry is replaced by the missing-forcing blocker
- **THEN** the decision SHALL satisfy the stable missing-forcing blocker contract,
  so the forcing backfill for renamed model identities drains it

### Requirement: Bounded pass evidence SHALL retain the operator-relevant retry policy and the promised runbook slug

The bounded candidate summary SHALL retain whichever of the `retry_policy` fields `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required` the producing arm actually wrote, including false and zero values, so that a size-bounded pass still shows an operator the pin and budget state of a blocked candidate. Retention is per-field and absence-preserving: a field the producer never emits SHALL stay absent rather than be materialised, and a field the producer emits as `false` or `0` SHALL be retained rather than dropped. The summary SHALL additionally retain the refused restart stage of a candidate blocked by the sink guard on the confirmed re-entry restart stage. That stage is the refusal's only triage key — it is what tells an operator which pre-forecast input to repair — and it is the one field a size-bounded pass must not drop, since the refusal exists precisely for the case an operator has to act on. The retained value SHALL be the candidate's own restart-stage key as written, not the stage the guard computed from it, so that the summary shows what the candidate carried. Retention is the same per-field, absence-preserving rule as above: a refusal whose restart-stage key is absent or null SHALL keep the field absent rather than materialise it, and SHALL stay identifiable by its own blocked decision. One consequence follows and is deliberate. Two of the sink's refusals are **stage-independent** — the fresh-full-chain exclusion blanks the effective stage while the candidate's own key still reads something, and an unreadable confirmation block is refused before the stage is compared at all — so either can be refused while its own key reads exactly `forecast`. A stage-based refusal cannot: a candidate whose key reads `forecast` is admitted, not refused. Therefore a summarized refusal showing `forecast` is necessarily one of the two stage-independent kinds, and never a stage-based one. That is the one triage inference a summarized pass still supports, and it SHALL be pinned by a test that also establishes its premise — that such a candidate is in fact admitted — rather than asserted. Telling the two stage-independent kinds apart requires the unsummarized pass, where the refusal's remaining fields live. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

#### Scenario: Summarized pass keeps the retry policy of a budget-exhausted candidate
- **WHEN** pass evidence is bounded-summarized and contains a blocked candidate whose decision is `blocked_strict_warm_start_init_state_mismatch`
- **THEN** the summary entry SHALL carry that candidate's `attempt`, `retry_limit`, and `manual_retry_required` — the fields the strict warm-start budget arm writes
- **AND** a false or zero value SHALL be retained rather than dropped

#### Scenario: Summarized pass keeps the occurrence count of a quarantine-blocked candidate
- **WHEN** pass evidence is bounded-summarized and contains a blocked candidate whose decision is `blocked_journal_predecessor_identity_quarantine`
- **THEN** the summary entry SHALL carry that candidate's `occurrences` and `manual_retry_required` — the fields the §8.7 quarantine arm writes
- **AND** the budget arm's `attempt` and `retry_limit` SHALL stay absent rather than be materialised

#### Scenario: Summarized pass keeps the refused restart stage of a sink-blocked re-entry
- **WHEN** pass evidence is bounded-summarized and contains a candidate blocked by the sink guard for carrying a confirmation at a named restart stage other than `forecast`
- **THEN** the summary entry SHALL carry that candidate's refused restart stage
- **AND** the refusal SHALL remain identifiable by its own blocked decision after summarization
- **AND WHEN** the refused candidate carried no restart stage at all
- **THEN** the field SHALL stay absent rather than be materialised, and the row SHALL still be identifiable by that blocked decision
- **AND WHEN** the refusal was stage-independent — the fresh-full-chain exclusion blanked the effective stage, or the confirmation block could not be read — and the candidate's own restart-stage key reads `forecast`
- **THEN** the summary SHALL carry `forecast`, the two SHALL be indistinguishable from each other in a summarized pass, and no stage-based refusal SHALL be able to produce that value
- **AND** that last clause SHALL be established by showing the same candidate is admitted when its confirmation is readable and the fresh-full-chain marker is absent, not by assertion alone

#### Scenario: The promised runbook slug exists
- **WHEN** the display API returns a manual-action 409 naming a `recovery_runbook` slug
- **THEN** a file of that slug SHALL exist under `docs/runbooks/`

### Requirement: A breaker- or budget-blocked candidate SHALL re-enter only through a pinned one-shot operator confirmation

An operator SHALL be able to record a re-entry confirmation for one `(source_id, cycle_time, model_id)` and one of the decisions `blocked_journal_predecessor_identity_quarantine` or `blocked_strict_warm_start_init_state_mismatch` via the `confirm-operator-reentry` subcommand, which SHALL default to dry-run and write only with `--attest`. The confirmation SHALL be stored as a file-journal `forecast_cycle` pipeline event with a dedicated `event_type` (`operator_reentry_confirmation`) that the manual-retry marker reader does not adopt, and SHALL carry the operator, reason, request id, and an integer pin; a breaker confirmation SHALL also carry the recorded stale `init_state_id` it authorizes. The writer SHALL refuse when the journal records no completed identity for the target; for the breaker decision it SHALL refuse when the breaker is not engaged, when the recorded token differs from the live recorded `init_state_id`, or when the pin differs from the model's live quarantine rerun count; for the budget decision it SHALL refuse when the pin differs from the model's live budget re-entry count. The quarantine rerun count SHALL be the number of cohort MASTER rows for the cycle whose quarantine-rerun provenance names the model, regardless of their terminal status and of the identity they recorded, so every quarantine rerun accepted for submission increments it at acceptance time (by one per accepted reservation; a re-reservation of the same run may add another, which only over-counts). The budget re-entry count SHALL be the number of cohort MASTER rows for the cycle whose budget re-entry provenance names the model — provenance stamped by the reservation writer whenever the reserved basin's evidence carries an `operator_reentry_confirmation` block naming the budget's blocked decision — regardless of terminal status, job id, or retry suffix, so every budget re-entry accepted for submission increments it at acceptance time (by one per accepted reservation; a re-reservation of the same run may add another, which only over-counts). Neither count SHALL depend on the stage-scoped retry attempt or on job-id prefixes.

Both provenance stamps SHALL key on the surviving `operator_reentry_confirmation` evidence block rather than on the reserved basin's `decision` literal alone: the `decision` literal is not stable under the scheduler's post-match decision rewrites, while the block is preserved across them, so the projection SHALL NOT depend on which rewrite fired. A reserved basin whose evidence carries an `operator_reentry_confirmation` block naming one of the two confirmable decisions SHALL be stamped with that decision's provenance whatever its final `decision` literal reads. The quarantine projection's decision-literal trigger SHALL remain in force so that an ordinary (unconfirmed) quarantine rerun still increments the quarantine rerun count.

Every event descending from a confirmation SHALL either move the count that confirmation is pinned to, at the reservation writer, or be refused before it can submit; there SHALL be no third kind. In particular, whether a confirmation is consumed SHALL NOT depend on the outcome of a stage the operator did not authorize. Because the provenance is stamped only at a forecast-cohort reservation, a rewrite that restarts the confirmed retry at a stage other than `forecast` submits work that reaches the reservation — and therefore moves the count — only if that other stage succeeds; on its failure the submission happens, the count does not move, and the confirmation stays armed for a further re-entry. Such a rewrite SHALL therefore be refused rather than allowed to submit.

This SHALL be enforced at the sink rather than at each rewrite site. After every candidate writer has run — including the §8.6 predecessor emission, which prepends to the same list after the main loop — the scheduler SHALL make one pass over the final admitted-candidate list and refuse any candidate that carries an `operator_reentry_confirmation` block whose restart stage is not exactly `forecast`. The check SHALL be a positive comparison against `forecast`, so an absent or null restart stage is refused on the same footing as a named earlier or later one: a candidate whose restart stage the chain cannot resolve starts at the chain's first stage, which is before `forecast`. It SHALL be keyed on the candidate's own `state_evidence`, evaluated by the same expression — and under the same fresh-full-chain exclusion — that the run manifest's own top-level restart stage is derived from, so the guard's key and the manifest's key cannot diverge. The chain resolves its start from that manifest key first and falls back to the manifest's embedded `state_evidence` when it is absent — consulting the embedded restart-from key as well — and then takes the earliest stage across the cohort's basins — chain-side behaviour this change neither alters nor tests, described here only because the divergence argument below depends on it, and tracked separately as #2416. The guard's computed stage and the stage the chain would resolve are therefore not always equal, and the contract is not that they are: it is that **every divergence is in the refusing direction**. Whenever the guard's key is truthy the chain reads that same value, so they agree; whenever it is falsy or blanked by the fresh-full-chain exclusion, the guard computes no stage and the positive comparison refuses the candidate, whatever the chain would have resolved from the embedded evidence or the restart-from key. A confirmed candidate can therefore be refused for a stage the chain would not have started at, but never admitted for one it would. The check SHALL NOT be keyed on the originating decision's evidence: a rewrite may build the candidate's evidence independently and overwrite its restart stage without ever touching the decision it descends from, so a decision-keyed check would not see that rewrite at all. The refusal SHALL be a dedicated named `blocked` decision, absent from both forced-resubmit whitelists, which submits nothing, consumes nothing, leaves the confirmation armed and matching, and whose evidence SHALL carry both the offending restart stage and the confirmation block, with a `retry_policy` naming the `node22-control-plane-manual-recovery` runbook. The guard SHALL distinguish a candidate that carries no confirmation from one whose confirmation it cannot read. An **absent** `operator_reentry_confirmation` key is the ordinary unconfirmed candidate and SHALL be left alone. A key that is **present but not a readable confirmation block** SHALL be refused by the same blocked decision, carrying a field that says so and names the value's type: the guard's contract is fail-closed, and a confirmation it cannot parse is one it cannot establish is absent either — treating an unreadable block as no block would make the one shape an attacker or a corrupting writer controls the one shape that bypasses the check. The refusal SHALL be decided before the restart-stage comparison, so a candidate with an unreadable block is refused whatever its restart stage reads. This case is expected to be unreachable in production — every writer of the block emits it from one closed field set — so the requirement is a floor on the guard's behaviour, not a claim that the shape occurs. The operator's path out is to repair the pre-forecast input the rewrite was reacting to — the canonical readiness index or the raw manifest identity — out of band; the next pass then restarts the confirmed re-entry at `forecast`, stamps it at reservation, and consumes the signature exactly once.

Every site that writes a candidate's restart stage SHALL be accounted for against this sink by exactly one of three closures: (i) a test that measurably reaches the site with a confirmed candidate; (ii) a proof that the site runs strictly before any confirmation can match, or that its output cannot enter the admitted-candidate list at all; or (iii) a proof that the site's output reaches a confirmed candidate only as the value of that candidate's own top-level restart-stage key — the key the sink reads — along a named path of helpers, none of which renames that key, nests it, or removes the `operator_reentry_confirmation` block, and that the sink's positive check is total over that key's value domain, refusing every value that is not exactly `forecast`. A closure of this kind SHALL name every helper on the path; a path that can strip the confirmation block is not closed by it, because the sink would no longer see the candidate as confirmed. Being unreached by the current fixtures SHALL NOT count as any of the three. The accounting SHALL state the file scope it searched and, for every file that contains the restart-stage key but is excluded, the measured reason for the exclusion — reads only, downstream of the sink, or not on a candidate-producing path. An unstated scope SHALL NOT be treated as an empty one. The budget writer cannot see whether the budget is exhausted (the stage-scoped attempt depends on scheduler-side candidate identity), so a budget confirmation written before exhaustion stays armed and is honoured once the budget is exhausted while its pin still equals the live count; this residual (#2400) is an operator procedure obligation — confirm only a target the newest pass lists as budget-blocked — not a scheduler guarantee.

When the §8.7 quarantine breaker is engaged, the scheduler SHALL emit the existing `journal_predecessor_identity_mismatch` retry instead of the breaker `blocked` decision if and only if a matching confirmation exists whose pin equals the model's current quarantine rerun count; the discovery-side backfill selection SHALL treat such a confirmed model as having real work so its cycle keeps the execution slot. When the strict warm-start retry budget is exhausted, the scheduler SHALL emit the existing `strict_warm_start_terminal_init_state_mismatch` retry instead of the budget `blocked` decision if and only if a matching confirmation exists whose pin equals the model's current budget re-entry count. Pin comparison SHALL be exact equality. A re-entry retry SHALL carry an `operator_reentry_confirmation` evidence block and SHALL still pass the per-model forcing witness on its lane; when the witness is absent the candidate SHALL land in the named missing-forcing `blocked` decision, SHALL NOT submit, and its confirmation SHALL stay armed. The explicit missing-forcing repair policy SHALL additionally refuse, at the policy itself, any candidate whose decision evidence carries an `operator_reentry_confirmation` block — ahead of the exact-cycle identity, direct-grid, blocker-contract, warm-state and raw-manifest preconditions — and SHALL record a named refusal reason in the repair evidence, so that a confirmed candidate is never reclassified into a repair retry that restarts at `forcing` and the operator sees the repair's own refusal rather than a downstream one. This policy-level refusal is keyed on the decision's evidence because that is what the policy is handed; it does not replace the sink guard, which is what makes the invariant total. The policy SHALL be reached on every lane, not only the strict warm-start one: it is invoked both from the strict warm-start path and from the ordinary warm-admission path, and the latter invokes it before it tests whether a strict warm-start state exists at all. Whether it writes repair evidence therefore turns on the repair authorization, not on the lane. The policy has exactly two early returns, and naming them precisely matters because the two differ in what the operator can later see. The first tests the repair **switch**, not a cycle match: when the repair is not enabled at all, the policy returns the decision untouched and writes no repair evidence. The second tests the decision's shape — absent, not `blocked`, or `blocked` for some reason other than the stable missing-forcing blocker — and again returns it untouched and writes nothing; in that second case the candidate stays on whatever decision it already had, which need not be a `blocked` one at all. Neither early return is a cycle check. An enabled repair whose authorized cycle does not match this candidate is therefore **not** an early return: it enters the policy and is refused with a named reason recorded in the repair evidence, which is how an operator distinguishes "the repair considered this candidate and declined" from "the repair never looked". Those two early returns are the only gates: neither one tests the lane, so wherever the policy does proceed it applies the confirmation refusal above, which is stated once and is not lane-specific. The operator SHALL instead backfill the model's own forcing, after which the confirmed re-entry restarts at `forecast`, is stamped at reservation, and moves the count to N+1, at which point the confirmation no longer matches. A candidate carrying no confirmation SHALL keep the existing repair behaviour unchanged. Confirmations SHALL be read journal-direct through a repository accessor that reads the cycle's full event rows regardless of model filtering and forecast-cycle terminal status; a repository without the accessor SHALL behave as if no confirmation exists. The blocked decisions SHALL remain absent from both forced-resubmit whitelists, the global retry limit SHALL NOT change, and the scoring and filtering surfaces SHALL NOT write to the journal. The blocked evidence `retry_policy` SHALL name the `confirm-operator-reentry` command and the `node22-control-plane-manual-recovery` runbook.

#### Scenario: Confirmed breaker re-entry runs once and the breaker re-engages
- **GIVEN** a candidate blocked by the quarantine breaker whose quarantine rerun count is N
- **WHEN** an operator attests a confirmation with pin N
- **THEN** the next pass SHALL submit a real replacement forecast carrying quarantine provenance
- **AND WHEN** that rerun completes re-recording the same stale token
- **THEN** the quarantine rerun count SHALL become N+1, the confirmation SHALL no longer match, and the candidate SHALL return to `blocked` with no submission

#### Scenario: A rerun that records a different stale token does not reuse the confirmation
- **WHEN** the confirmed rerun completes recording a different stale token whose own occurrence count equals the old pin
- **THEN** the quarantine rerun count SHALL still become N+1, the confirmation SHALL NOT match, and the candidate SHALL be `blocked` with no submission

#### Scenario: A failed confirmed rerun does not restore the confirmation
- **WHEN** the confirmed quarantine rerun was accepted for submission and then failed at the compute layer while the recorded identity is still stale
- **THEN** the quarantine rerun count SHALL already equal N+1 from the accepted rerun, the confirmation SHALL NOT match on any later pass, and no further quarantine rerun SHALL be submitted on that confirmation

#### Scenario: No second submission while the rerun is in flight
- **WHEN** a pass runs after the confirmed re-entry was submitted but before the rerun completes
- **THEN** no further forecast submission SHALL occur for that candidate

#### Scenario: Confirmed budget re-entry without raising the global limit
- **GIVEN** a strict warm-start candidate blocked with `attempt >= retry_limit` and budget re-entry count M
- **WHEN** an operator attests a confirmation with pin M
- **THEN** the next pass SHALL submit one retry without any change to `NHMS_SCHEDULER_RETRY_LIMIT`
- **AND** once that retry is accepted for submission the budget re-entry count SHALL be M+1, and every later pass SHALL keep the candidate `blocked` with no further submission on that confirmation, whether the rerun succeeds, fails, or was reserved under a different job-id prefix than the retries that exhausted the budget

#### Scenario: Stale or absent confirmation keeps the fail-stop
- **WHEN** no confirmation exists, or its pin differs from the live value
- **THEN** the breaker and budget decisions SHALL remain `blocked` with no submission

#### Scenario: Re-entry without the model's own forcing stays blocked
- **WHEN** a matching breaker confirmation exists on the non-strict lane but the candidate's own forcing witness is absent
- **THEN** the candidate SHALL land in the named missing-forcing `blocked` decision and SHALL NOT submit

#### Scenario: An explicit missing-forcing repair refuses a confirmed candidate
- **GIVEN** a candidate on the strict lane whose budget is exhausted, whose forcing witness is absent, and for which a matching confirmation pinned to the live budget re-entry count N exists
- **WHEN** the operator additionally enables the explicit missing-forcing repair for that exact cycle
- **THEN** the repair policy SHALL refuse the candidate with a named refusal reason naming the confirmation, the candidate SHALL stay in the missing-forcing `blocked` decision, and nothing SHALL submit
- **AND** the budget re-entry count SHALL still be N and the confirmation SHALL still match, so the operator's signature is not spent

#### Scenario: Backfilling the forcing lets the confirmed re-entry consume the signature exactly once
- **GIVEN** the refused candidate above, whose confirmation is still pinned to N
- **WHEN** the operator backfills the model's own forcing and the next pass runs with no repair flag and no new confirmation
- **THEN** the confirmed re-entry SHALL restart at `forecast`, the reserved cohort master SHALL carry the budget re-entry provenance for that model, and the budget re-entry count SHALL become N+1
- **AND** the confirmation SHALL NOT match on any later pass, which SHALL return the candidate to `blocked` with no submission

#### Scenario: A confirmed candidate rewritten to restart before `forecast` is refused at the sink
- **GIVEN** a matching confirmation pinned to the live count, and a candidate whose canonical readiness is incomplete so the raw-manifest branch rewrites its restart stage to `convert` without touching the decision it descends from
- **WHEN** the pass builds its candidate list, with the explicit missing-forcing repair not authorized
- **THEN** the candidate SHALL NOT be admitted, SHALL land in the dedicated sink refusal decision naming `convert` and the confirmation, and nothing SHALL submit
- **AND** the run manifest SHALL NOT carry that restart stage, the count SHALL be unchanged, and the confirmation SHALL still match on the next pass

#### Scenario: The sink refusal covers a predecessor candidate prepended after the main loop
- **WHEN** the offending confirmed candidate is emitted by the §8.6 predecessor emitter, which prepends to the list after the main loop has ended
- **THEN** it SHALL be refused on the same footing as a main-loop candidate

#### Scenario: A confirmed candidate with no restart stage at all is refused
- **WHEN** a confirmed candidate reaches the end of the pass carrying no restart stage
- **THEN** it SHALL be refused by the same check, because a blank top-level key does not mean the chain starts at its first stage — the chain falls back to the manifest's embedded state evidence and then to the restart-from key before resolving — so the guard cannot read a blank key as `forecast`, and refusing is the direction the divergence contract above requires

#### Scenario: A candidate whose confirmation block cannot be read is refused, and one with no block is not
- **GIVEN** a candidate whose `operator_reentry_confirmation` key holds something that is not a readable confirmation block — a null, a string, a number, a list
- **WHEN** the pass reaches the sink
- **THEN** the candidate SHALL be refused by the sink's blocked decision, carrying a field that marks the block unreadable and names the value's type
- **AND** the refusal SHALL hold even when the candidate's restart stage reads exactly `forecast`, because the decision is taken before the stage comparison
- **AND WHEN** the key is absent altogether
- **THEN** the candidate SHALL be left alone, because an absent key is the ordinary unconfirmed candidate and refusing it would block every ordinary retry

#### Scenario: Re-entry on the non-strict lane stays blocked even with the repair authorized
- **GIVEN** a matching breaker confirmation on the non-strict lane whose forcing witness is absent
- **WHEN** the operator enables the explicit missing-forcing repair for that exact cycle
- **THEN** the candidate SHALL stay in the missing-forcing `blocked` decision, SHALL NOT submit, and its confirmation SHALL stay armed

### Requirement: The released-identity recovery listing SHALL isolate malformed journal rows without swallowing budget refusals

`query_released_identity_blocked_jobs` and the `recover-released-identity-blocked-reservation` command SHALL skip an individual flat pipeline-job row whose read fails, in both the first flat scan and the cycle-scoped confirming read, with a single-row content-validation reason (a closed, enumerated set covering malformed JSON, non-object payloads, record type, schema, identity, cycle-time, identity-field mismatch, accepted-submit evidence validation, and JSON node/depth limits), and SHALL continue the scan. Every skip SHALL appear in the command receipt as `skipped` entries carrying the path and reason, with a `skipped_count`. Every other journal error — including file, depth, record, and byte budget refusals, unreadable files, and containment faults — SHALL still propagate. The targeted `--job-id` mode and the unscoped whole-tree fallback leg are unchanged. Candidate-state and pipeline-job reads used by the scheduler SHALL keep failing closed on the same rows.

#### Scenario: One malformed row does not blind the recovery listing
- **WHEN** the flat pipeline-jobs directory holds one invariant-invalid row next to wedged released-identity rows
- **THEN** the list receipt SHALL include the wedged rows
- **AND** SHALL report the malformed row under `skipped` with reason `file_journal_evidence_invariant_invalid`
- **AND** this SHALL hold when the malformed row belongs to the same cycle as the wedged rows or has a filename that does not resolve to a cycle

#### Scenario: Budget refusal still raises
- **WHEN** the scan exceeds the record budget while a malformed row is also present
- **THEN** the command SHALL fail with `file_journal_record_limit_exceeded`

### Requirement: Operator-action decisions SHALL be enumerable from db-free pass evidence

The scheduler CLI SHALL provide a read-only `list-operator-actions` subcommand that scans the most recent N terminal scheduler pass evidence files under the evidence root (`--evidence-root`, defaulting to `NHMS_SCHEDULER_EVIDENCE_ROOT`), excluding `.pre_execution.json` files and ordering by modification time. It SHALL identify blocked candidates by decision literal — `permanent_failure`, `cancelled_manual_retry_required`, `blocked_strict_warm_start_init_state_mismatch`, `blocked_journal_predecessor_identity_quarantine`, `blocked_operator_reentry_restart_stage_refused` — not by the `manual_retry_required` flag, so that bounded-summarized passes remain enumerable. That set SHALL equal the set of decisions the db-free scheduler writes a **literal** `manual_retry_required: true` on, because a decision missing from it is answered with `exit 0` ("nothing waits") while the runbook still prescribes an operator action for it. The writers that instead set the flag from an expression SHALL be enumerated and dispositioned rather than ignored, so that a new one cannot enter unnoticed. The two that exist (`scheduler_state_failure.py:514` `retry_downstream` and `:1954` `retry_failed`, both `failure["permanent"]`) are outside the listed set because neither can be built with the flag true: `:498` returns `None` for a permanent failure before the first dict is built, and for the second the guard is at the call site — `scheduler_state_decision.py:385` returns the permanent `blocked` decision before the `retry_failed` return point at `:412`. The same `_failure_retry()` evidence reaching the missing-forcing channel (`scheduler_state_decision.py:373`) is likewise safe: every return point there goes through `_artifact_blocker_evidence` (`scheduler_state_failure.py:909`), which writes its own `decision` (`:927`) and its own literal `manual_retry_required: false` (`:947`) and inherits neither. It SHALL also list each model named in a not-selected `source_cycles` entry whose `selection_reason` is `journal_predecessor_identity_quarantine_breaker_engaged`, because a breaker-released cycle never reaches candidate construction. Each listed action SHALL carry `candidate_id`, `source_id`, `cycle_time`, `model_id`, `decision`, `reason`, `attempt`, `retry_limit`, `occurrences`, `recorded_init_state_id` (null when absent), and first/last seen pass. When one action appears in several scanned passes it SHALL be listed once, and apart from `first_seen_pass` and the seen count **every** value field of that entry SHALL be the value carried by `last_seen_pass` — the operator feeds `recorded_init_state_id` from this receipt into `confirm-operator-reentry`, which refuses a stale token — except that a `candidate_id` absent from the newest pass (the breaker-released leg carries none) SHALL fall back to a known one. The command SHALL exit `1` when at least one action is listed, `0` when none, and `2` when the evidence root is missing or unreadable or `--passes` is not an integer `>= 1`; an individual unreadable pass file — including one deleted between the directory scan and its own `stat`, as the evidence retention timer may do at any time — SHALL be reported under `unreadable_passes` and SHALL NOT abort the scan. A scanned pass SHALL be decidable only when it is readable and its terminal status belongs to a closed allowlist of statuses known to be written only after candidate construction ran (a post-construction status missing from the allowlist errs toward undecidable), and it is not a size-fallback product (`resource_limit_blocked` carrying `limit.pre_limit_status`), because that product empties `source_cycles` and so cannot show breaker-released cycles — its summarized blocked candidates SHALL still be listed; every other pass (for example `lock_contended`, `preflight_blocked`, or an unknown status) SHALL be reported under `non_evaluating_passes` with its status. A transparent pass is one whose status belongs to a closed set — `lock_contended` and `preflight_blocked` — known either to have evaluated nothing or to have written its full candidate lists and `source_cycles`; a transparent pass never hides anything. When no action is listed and at least one scanned pass dropped its candidate lists under the evidence byte budget, or a pass file vanished between the directory scan and its `stat` (its modification time was never read, so it cannot be placed in the scan's time order at all and vetoes `exit 0` wherever it sat), or no scanned pass is evaluating and scope-complete, or any pass that is neither evaluating-and-scope-complete, nor transparent, nor merely scope-narrowed is newer than the newest evaluating and scope-complete pass (it may have evaluated candidates it cannot show — a size-fallback product, an unreadable pass file, a `lease_lost` or exception-path `resource_limit_blocked` pass that emptied its lists, an unknown status, or a pass whose scope keys are missing — and so may hide a breaker release that engaged after that pass), the command SHALL report those passes and exit `3` (undecidable) instead of `0`. The three triggers are anchored on **evaluating and scope-complete**, not on "decidable": a scope-narrowed pass is decidable yet answers only for its own scope (see the narrowed-scope requirement below), so it neither backs `exit 0` nor forces `exit 3`. The bounded candidate summary SHALL retain `retry_policy` `attempt`, `retry_limit`, `occurrences`, and `manual_retry_required`, including false and zero values, and SHALL retain `journal_predecessor_identity.recorded_init_state_id`. That last key is not decoration: the summary drops `state_evidence` wholesale, so without retaining it a newest pass that fell back to the bounded summary would report `recorded_init_state_id: null` for a candidate whose token the scheduler did record — and `confirm-operator-reentry`'s breaker arm requires that token, while its `recorded_init_state_id_mismatch` refusal returns only `occurrences` and `quarantine_rerun_count` and no other operator surface prints the live token. Scanning with `--passes 1` does not recover it: the token is exactly what the newest pass dropped. Because the reader takes every value field from `last_seen_pass`, retention at the writer — not a fallback to an older pass — is what makes the token correct rather than stale. The `recovery_runbook` slug returned by the display API's manual-action 409 SHALL name an existing file under `docs/runbooks/`.

#### Scenario: Summarized pass still lists a budget-exhausted candidate
- **WHEN** the latest pass evidence was bounded-summarized and contains a blocked candidate whose decision is `blocked_strict_warm_start_init_state_mismatch`
- **THEN** `list-operator-actions` SHALL list it with `attempt` and `retry_limit` taken from the retained bounded keys
- **AND** the command SHALL exit `1`

#### Scenario: Breaker-released cycle is listed from source-cycle evidence
- **WHEN** a pass released a breaker-engaged cycle from the backfill slot so no candidate entry exists for it
- **THEN** `list-operator-actions` SHALL list each model of that not-selected entry with decision `blocked_journal_predecessor_identity_quarantine`

#### Scenario: Dropped candidate lists are undecidable
- **WHEN** no action is found and a scanned pass marked its candidate lists as dropped
- **THEN** the command SHALL exit `3` and name that pass

#### Scenario: A window without an evaluating pass is undecidable
- **WHEN** every scanned pass is unreadable or non-evaluating (for example lock-contended or preflight-blocked) while an older evaluated pass outside the window holds a blocked candidate
- **THEN** the command SHALL list those passes under `unreadable_passes` or `non_evaluating_passes` and exit `3`, never `0`

#### Scenario: A window of size-fallback passes is undecidable
- **WHEN** every scanned pass is a size-fallback product whose original payload had a breaker-released not-selected source cycle and no other listed decision
- **THEN** the command SHALL report those passes under `non_evaluating_passes` and exit `3`, never `0`
- **AND WHEN** such a pass still carries a summarized blocked candidate of a listed decision
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`

#### Scenario: A hidden pass newer than every evaluating and scope-complete pass is undecidable
- **WHEN** no action is listed, an older scanned pass is evaluating and scope-complete, and a newer scanned pass is a size-fallback product, unreadable, `lease_lost`, or of an unknown status — whether or not a still newer transparent pass follows it
- **THEN** the command SHALL exit `3`, never `0`
- **AND WHEN** instead only transparent passes are newer than the newest evaluating and scope-complete pass
- **THEN** the command SHALL exit `0`

#### Scenario: A pass file deleted mid-scan is reported and vetoes exit 0
- **WHEN** no action is listed, every scanned pass is evaluating and scope-complete, and one pass file is deleted between the directory scan and its `stat`
- **THEN** the command SHALL still scan and report the remaining passes, SHALL name the deleted file under `unreadable_passes`, and SHALL exit `3`
- **AND** it SHALL NOT exit `2`, which names the evidence root and would send the operator after a root that is in fact correct

#### Scenario: A candidate seen in several passes reports the newest pass's values
- **WHEN** the same candidate is listed by three scanned passes carrying different `recorded_init_state_id` values
- **THEN** it SHALL be listed once with `first_seen_pass` naming the oldest of them, `last_seen_pass` naming the newest, and `recorded_init_state_id` (and every other value field) taken from that newest pass

#### Scenario: The re-entry token survives bounded summarization
- **WHEN** the same breaker-quarantined candidate appears in an older full pass and again in a newer pass that fell back to the bounded candidate summary
- **THEN** the merged entry SHALL name the newer pass as `last_seen_pass` and SHALL carry that newer pass's `recorded_init_state_id` rather than `null`
- **AND** the token SHALL come from the newer pass's retained summary key, never from a fallback to the older pass, so the operator is given a current token and not a stale one

#### Scenario: The listed decision set covers the refused re-entry sink
- **WHEN** a scanned pass is evaluating and scope-complete and the only blocked candidate of a listed decision it carries is one whose decision is `blocked_operator_reentry_restart_stage_refused`
- **THEN** that candidate SHALL be listed and the command SHALL exit `1`, never `0`

#### Scenario: No operator actions
- **WHEN** the scanned passes contain only blocked candidates of other decisions, at least one scanned pass is evaluating and scope-complete, no pass dropped its candidate lists, no pass file vanished mid-scan, and only transparent or scope-narrowed passes are newer than the newest evaluating and scope-complete pass
- **THEN** the command SHALL print an empty `operator_actions` list and exit `0`

### Requirement: A narrowed-scope pass SHALL NOT be read as evidence that nothing is pending

`list-operator-actions` SHALL decide `exit 0` ("nothing needs an operator") only from passes that actually looked everywhere. A pass may be narrowed in four ways that the pass file records: backfill disabled, operator filters that select a subset of models, basins, or an expression, a `sources` list naming a subset of the production source set, and a backfill discovery window collapsed to zero width. A narrowed pass that lists no action has not established that no action exists — it has established that none exists *inside its own scope* — and the breaker-released entries that make up one of the listed decisions are produced only on the backfill leg.

**The set of narrowing dimensions SHALL be closed against the writer, not maintained by hand.** Every top-level key that a pass in an **evaluating** status publishes SHALL carry one of three dispositions — judged by the scope test, documented as a known boundary, or recorded as not a scope dimension with its reason — and a published key that has none SHALL fail the build. Wherever a disposition is given at subkey level, the closure SHALL descend to that key's subkeys and apply the same rule, so that a knob added one level down fails the build too.

**The authority for that key set SHALL be obtained by running the writer, never by reading it.** The closure test SHALL execute a real pass through the existing db-free doubles and take the published mapping's own keys; it SHALL NOT parse writer source, and it SHALL NOT compare against a key list typed into the test. The check runs both ways: a published key with no disposition fails, and a dispositioned key absent from the published mapping fails, so that a stale entry cannot survive the writer dropping its key.

Scoping the authority to evaluating passes is exact rather than convenient: the scope test runs only on evaluating passes, and every early-exit branch of the writer rewrites `status` out of the evaluating set before returning, so a key that only an early-exit branch publishes can never reach the scope test.

**The doubles' reachable surface is itself a copy, and SHALL be closed too.** Running the writer only moves the enumeration from a hand-typed list to whichever branches the fixture happens to walk; a key that an evaluating pass can publish down a branch no fixture leg opens is as invisible as one nobody typed. The closure fixture SHALL therefore drive every configuration switch that changes which keys an evaluating pass publishes — at minimum a leg with Slurm execution enabled alongside the planning-only leg, since `slurm_preflight` is published on the main evaluating path and is absent from a planning-only run. There is no conditional-row escape hatch: where a double falls short of a live pass the fixture SHALL be opened, never the disposition table annotated. This is not a hypothetical courtesy to future keys — the first version of this closure was green while `slurm_preflight` had no disposition at all, in both directions, because neither fixture leg reached it.

Three separate rounds of review found a different unread narrowing dimension in this one function, and a fourth found that the disposition table itself had been built by reading two writer files and so covered a third of the keys a live pass actually publishes. Enumerating by hand is what allowed every one of them — including the attempt to fix it — which is why the enumeration is derived from a run and the dispositions are the thing reviewed.

A scanned pass SHALL therefore be treated as **scope-complete** if and only if all of the following hold, read from that pass's own file:

- its `backfill.enabled` is exactly `True`; and
- its `operator_filters` selects nothing away — `basin_ids` empty, `model_ids` empty, and `expression` null; and
- its top-level `sources` list **covers** the whole production source set (`("gfs", "IFS")`, `scheduler.py`'s `DEFAULT_PRODUCTION_SOURCES`, which `cli.py` repeats as the `resolved_sources` fallback); and
- its `cycle_window.lookback_hours` is greater than zero; and
- its `runtime_config.allowed_cycle_hours_utc` **covers** the default cycle-hour set (`scheduler.py`'s `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`); and
- its `counts.selected_model_count` is greater than zero.

`sources` SHALL NOT be judged by emptiness: `cli.py` resolves it to the full set when the operator passes no `--source`, so it is never empty, and `scheduler_evidence.py:268` writes it unconditionally as `list(config.sources)`. A pass run with `--source gfs` therefore never looked at IFS and is scope-narrowed exactly as a basin-narrowed pass is. The test is **coverage, not equality**: a pass naming a superset did look at every production source, and the spelling of the names needs no normalising here because `ProductionSchedulerConfig.__post_init__` already routes every `--source` value through `normalize_source_id`, which upper-cases into a closed table and raises on an unknown name — a case variant cannot reach the evidence file. Case-folding at the reader would be worse than useless: the db-free adapter's manifest paths (`raw/{source_id}/…`) are case-sensitive, so a reader that treated `GFS` as `gfs` would hand a key to a false `exit 0` that the config layer currently locks shut.

`cycle_window.lookback_hours` is the **time** dimension, and only its degenerate value is judged. A zero-width discovery window (`--lookback-hours 0` without `--cycle-time`, which the CLI accepts because it validates no lower bound — `max_cycles_per_source < 1` raises, `lookback_hours` does not, and the config layer's `max(int(...), 0)` clamps negatives to zero rather than rejecting them) leaves the backfill leg blind to every cycle older than the window edge, and the breaker-released cycles that make up `blocked_journal_predecessor_identity_quarantine` sit by construction on exactly that oldest side. Such a pass SHALL be `scope_narrowed`. It is read from the top-level `cycle_window` block rather than from `backfill.lookback_hours`: both carry the same `config.lookback_hours`, but `cycle_window` is written unconditionally by `base_evidence` while the `backfill` copy exists only on the `enabled: True` leg, so judging the latter would couple this presence check to the `backfill.enabled` verdict and drift the moment anyone gives the other leg that key.

`runtime_config.allowed_cycle_hours_utc` is the **cycle-hour** dimension, and it is one of the two narrowing knobs the evidence publishes exactly once — the other being `backfill.enabled`, which has no second copy either. The remaining knobs appear two or three times over (`sources` also at `runtime_config.sources`, `lookback_hours` also at `runtime_config.lookback_hours` and, on one leg, at `backfill.lookback_hours`, the operator filters also at `filters` and inside `model_discovery`). Single publication matters here only as a caution against judging a mirror instead of the source; it is not what makes the knob worth judging. Cycle discovery drops every cycle whose hour is outside this set, so an operator who narrows it below what production runs leaves whole cycle times unevaluated while the pass still reports no action. It SHALL be judged as **coverage against the code-side default**, `DEFAULT_ALLOWED_CYCLE_HOURS_UTC`, imported from the same module the reader imports `DEFAULT_PRODUCTION_SOURCES` from and never restated as a literal in the reader. The default is itself a narrow subset of the twenty-four possible hours, and that is deliberate: the question this rule answers is whether the operator narrowed *below the production baseline*, not whether the baseline covers the day. Judging against the full day would classify every production pass as narrowed and make `exit 0` unreachable. It is read from the top-level `runtime_config` block, which `base_evidence` writes unconditionally and no later branch overwrites, rather than from the nested runtime mirror.

`counts.selected_model_count` is not a narrowing dimension but an **emptiness** one, and it is judged for a different reason than the others. A pass whose model registry resolved to no runnable models discovers cycles, writes evidence, and terminates in an evaluating status having evaluated nothing at all; under the rule above it would carry empty operator filters, the full source set, a positive window and the default cycle hours, and so would be scope-complete and would clear the hidden-pass flag. Such a pass SHALL instead be reported under `non_evaluating_passes` with its own reason, distinct from both `scope_narrowed` and `scope_unknown`, and SHALL **arm** the hidden-pass flag. The distinction that decides this is the one the rest of this requirement already uses: a field that is present and narrowing is the operator's own instruction and leaves the flag alone, whereas a pass that made no observations is exactly the case arming exists for. **This verdict SHALL take precedence over `scope_narrowed`** when a pass is both narrowed and empty. An operator who narrows to a model set that resolves to nothing has not merely limited what was looked at — nothing was looked at — and the "leave the flag as you found it" rule exists for a pass that did observe its own smaller scope. Leaving the flag alone there would let a pass that observed nothing inherit a `cleared` flag from an older pass and endorse `exit 0`. The writer-side cause — that `backfill.enabled` records the configured intent rather than the leg discovery actually took when the model set is empty — is a separate defect on the writer and is out of scope here; this requirement governs only what the reader may conclude from `exit 0`.

**A non-degenerate window is a documented boundary, not a defect.** For `lookback_hours > 0` and for any `cycle_lag_hours`, `exit 0` asserts only that nothing is pending *within that pass's own window* `[start_time_utc, end_time_utc]`. Production runs `lookback=96h` with `cycle_lag=16h`, so the most recent 16 hours of cycles are outside every window. There is no in-repo authority for a "complete" window against which a narrower one could be judged — the production setting and the code default disagree — so no threshold is invented here; the boundary is stated instead. (The production value is 96 and the code default is 24.)

**The inactive-model boundary.** `exit 0` SHALL NOT be read as asserting anything about models the registry manifest marks inactive. The registry's own `model_count` counts every model row the manifest declares, while `active_model_count` counts what `list_models(active=True, …)` returned, and that filter runs before model discovery ever sees a row — so the difference between the two is structurally absent from the `exclusions` array, which can only explain drops that happen after discovery receives the row. This difference SHALL be documented rather than judged: requiring the two counts to be equal would amount to forbidding the manifest from ever retiring a model, and there is no in-repo authority for how many models a manifest ought to declare, so no threshold is invented here for the same reason none is invented for the discovery window. Measured at the live node-22 evidence root on 2026-09-16, the two counts are equal on every retained pass, so the boundary is presently empty in production while remaining structurally real.

**The single-slot boundary.** The backfill leg evaluates only the **oldest** incomplete cycle per source per pass (`scheduler_discovery.py`), recording newer gaps as `backfill_deferred_waiting_for_prior_cycle`; those newer cycles are evaluated only after the prior one yields usable state. This is narrower than it sounds, and the mitigation SHALL be documented with it: an unresolved action keeps its own cycle a gap, so that cycle is re-evaluated and re-listed on every subsequent pass **as long as that cycle stays inside the later passes' discovery range** — what is deferred is an action on a *newer* cycle, which cannot even be created until the older one clears. `blocked_journal_predecessor_identity_quarantine` is exempt **from this boundary**: breaker release runs before the single-slot split and releases every consecutive breaker-engaged cycle from the oldest, so that decision's visibility is complete with respect to slot contention. This boundary SHALL NOT be attributed to `max_cycles_per_source`, which is inert for any pass whose `backfill.enabled` is `True`.

**The discovery-retraction boundary.** That mitigation depends on re-discovery, and re-discovery is not guaranteed across a configuration change. `allowed_cycle_hours_utc` filters cycles in `scheduler_discovery.py` **before** the single-slot gap selection runs, and candidates are built only from the cycles a pass discovered, so a cycle whose hour leaves the allowed set is not merely deferred — it stops being discovered, stops producing candidates, and its already-listed unresolved action stops appearing in `blocked_candidates` and `source_cycles` alike. The same holds for a `lookback_hours` reduction that moves a cycle outside the window. `exit 0` SHALL therefore NOT be read as asserting anything about actions on cycles that an earlier, wider configuration could see and the current one cannot. This is documented rather than judged, for the reason the other boundaries are: the reader sees only the current pass's own configuration and has no in-repo authority for what an earlier one was. The knob is environment-only (no CLI flag), so reaching this state takes a deliberate widen-then-retract of the service environment, not an ordinary run. **Unlike the single-slot boundary, this one grants `blocked_journal_predecessor_identity_quarantine` no exemption**: breaker release is computed inside `_select_backfill_source_cycles` from the *already filtered* `discoveries`, so a cycle the hour filter removed is invisible to the breaker release exactly as it is to everything else. The exemption above protects that decision from losing its slot, not from losing its cycle.

`operator_filters` is a mapping that a normal production pass always writes, carrying those keys at their empty defaults. **Scope-completeness SHALL be decided from the filter *values*, never from the presence or size of the `operator_filters` mapping itself**: a pass that carries the mapping with every filter empty is scope-complete. Measured at the live node-22 evidence root on 2026-09-16, every retained pass carries `operator_filters` as a four-key mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null, with `backfill.enabled` true — so a rule keyed on mapping-emptiness would classify every production pass as narrowed and could never reach `exit 0`. The same measurement found, on every retained pass without exception: `sources` at the full production set; `cycle_window` carrying all five of its keys (`lookback_hours` 96, `cycle_lag_hours` 16, `max_cycles_per_source` 1); `runtime_config.allowed_cycle_hours_utc` exactly equal to the code-side default; and `counts.selected_model_count` at 76. No rule in this requirement can therefore turn a production pass undecidable or narrowed. (The retained-pass count is deliberately not quoted here: it drifts with the retention timer, and four measurements taken for this change across two days returned four different totals.)

A scope-complete pass behaves as today. A **scope-narrowed** pass SHALL still have its own actions listed, SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`, and SHALL leave the hidden-pass flag exactly as it found it — it neither clears it, because it did not look everywhere, nor arms it, because the narrowing was the operator's own instruction and hides nothing unexpectedly.

**The scope test SHALL be applied only to a pass that is otherwise evaluating.** A pass whose status already makes it non-evaluating — a transparent `lock_contended` or `preflight_blocked` pass, a size-fallback product, an unreadable file — keeps the classification its status gives it and is never reclassified as scope-narrowed. This ordering is not a convenience: a pass written before candidate construction structurally carries no `backfill` key at all, because the scheduler writes that key only once candidates exist, so a scope test applied ahead of the status test would declare every such pass undecidable and contradict the rule above that lets a transparent pass sit newer than a decidable one without forcing `exit 3`.

Within that restriction, when a pass that is otherwise evaluating is missing any field the scope test reads — the `backfill` key, the `operator_filters` key, `backfill.enabled`, any of `operator_filters.basin_ids`, `operator_filters.model_ids`, `operator_filters.expression`, the top-level `sources` list (absent, or not a list of strings), the top-level `cycle_window` block and its `lookback_hours` (absent, or not an integer), the top-level `runtime_config` block and its `allowed_cycle_hours_utc` (absent, or not a sequence of integers), or the top-level `counts` block and its `selected_model_count` (absent, or not an integer) — its scope cannot be determined and it SHALL NOT be assumed scope-complete. The dividing line is presence, not value: a field that is present and narrowing is the operator's own instruction (`scope_narrowed`), while a field that is absent is unreadable (`scope_unknown`). Every one of those fields is written unconditionally by a normal pass — `scheduler_evidence.py` emits the four-key `operator_filters` mapping as a dict literal, emits `sources` as `list(config.sources)`, and emits `cycle_window` and `runtime_config` each carrying all of their keys, `scheduler_runtime.py` emits `counts` as a dict literal on the main branch, and both legs of the `if/else` in `scheduler_runtime.py` write `backfill` carrying `enabled` — so an absent field is not a narrowing the pass chose to record but a shape the writer cannot produce. The presence check on each block is not optional bookkeeping: judging a block's value while assuming its absence means "complete" would reintroduce, one field over, exactly the missing-field-read-as-complete defect this paragraph exists to forbid — which is how the window dimension came to be missed in the first place. Such a pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL **arm** the hidden-pass flag, exactly as any other pass that may have evaluated candidates it cannot show. Arming is positional: a scope-complete decidable pass newer than it clears the flag again, so a missing-key pass older than such a pass does not by itself force `exit 3`, while one newer than every scope-complete pass does. A missing-key pass SHALL NOT be treated as a global veto in the way a dropped candidate list is.

A scope-narrowed pass is reported under `non_evaluating_passes` and therefore does not count toward the window's evaluating-pass total, while still being a pass that was read and understood. "Decidable" throughout this capability carries the single definition given in the requirement above — readable, terminal status in the closed allowlist, not a size-fallback product — and "evaluating" means a pass that counts toward the window having looked at anything. Scope-narrowed, `scope_unknown` and `no_models_evaluated` are the **three** kinds that are decidable but not evaluating, which is why a window containing nothing but those cannot reach `exit 0`. They differ only in what they do to the hidden-pass flag: a narrowed pass leaves it as it found it, while an unknown-scope pass and a pass that evaluated no models both arm it.

#### Scenario: A window of only narrowed passes cannot produce exit 0
- **WHEN** every scanned pass lists no action, none of them is scope-complete, and the newest carries `operator_filters.model_ids` naming one model
- **THEN** each narrowed pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A window of only backfill-disabled passes cannot produce exit 0
- **WHEN** every scanned pass lists no action, none of them is scope-complete, and the newest carries `backfill.enabled` false
- **THEN** each narrowed pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed`
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A narrowed pass does not re-arm a flag an earlier scope-complete pass cleared
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the only newer pass is scope-narrowed
- **THEN** the narrowed pass SHALL leave the hidden-pass flag as it found it — neither clearing nor arming it
- **AND** the command SHALL exit `0`

#### Scenario: A narrowed pass does not clear a flag a hidden pass armed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and decidable, a size-fallback pass is newer than it, and a scope-narrowed pass is newer still
- **THEN** the hidden-pass flag armed by the size-fallback pass SHALL survive the narrowed pass
- **AND** the command SHALL exit `3`

#### Scenario: A source-narrowed pass does not clear a flag a hidden pass armed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries a `sources` list naming only `gfs`
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`

#### Scenario: A zero-width backfill window is scope-narrowed, not scope-complete
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries `backfill.enabled` true, empty operator filters, the full `sources` set, and `cycle_window.lookback_hours` of `0`
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`, never `0`

#### Scenario: A pass missing the cycle-window block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, and no `cycle_window` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`
- **AND** the same SHALL hold when `cycle_window` is present but carries no `lookback_hours`

#### Scenario: A sources list naming a superset is scope-complete
- **WHEN** a scanned pass lists no action and carries `backfill.enabled` true, empty operator filters, a positive `cycle_window.lookback_hours`, and a `sources` list naming every production source plus one further source
- **THEN** that pass SHALL be treated as scope-complete, because it did look at every production source
- **AND** when no newer pass hides anything, the command SHALL exit `0`

#### Scenario: A cycle-hour set narrower than the default is scope-narrowed
- **WHEN** no action is listed, the oldest scanned pass is scope-complete and evaluating, a size-fallback pass is newer than it, and the newest pass carries `backfill.enabled` true, empty operator filters, the full `sources` set, a positive `cycle_window.lookback_hours`, a positive `counts.selected_model_count`, and a `runtime_config.allowed_cycle_hours_utc` naming only one of the default cycle hours
- **THEN** that newest pass SHALL be reported under `non_evaluating_passes` with reason `scope_narrowed` and SHALL leave the armed hidden-pass flag armed
- **AND** the command SHALL exit `3`, never `0`
- **AND** a pass whose `allowed_cycle_hours_utc` names every default cycle hour plus a further hour SHALL be treated as scope-complete on this dimension

#### Scenario: A pass missing the runtime-config block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, a positive `cycle_window.lookback_hours`, and no `runtime_config` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL arm the hidden-pass flag
- **AND** the command SHALL exit `3`
- **AND** the same SHALL hold when `runtime_config` is present but carries no `allowed_cycle_hours_utc`, and when that value is present but is not a sequence of integers

#### Scenario: A pass that selected no models arms the flag rather than clearing it
- **WHEN** a scanned pass lists no action, carries an evaluating status, `backfill.enabled` true, empty operator filters, the full `sources` set, a positive `cycle_window.lookback_hours`, the default `runtime_config.allowed_cycle_hours_utc`, and `counts.selected_model_count` of `0`
- **THEN** that pass SHALL NOT be treated as scope-complete and SHALL NOT clear the hidden-pass flag
- **AND** it SHALL be reported under `non_evaluating_passes` with a reason distinct from both `scope_narrowed` and `scope_unknown`
- **AND** it SHALL arm the hidden-pass flag, so that when it is the newest scanned pass the command SHALL exit `3`
- **AND** when a scope-complete evaluating pass is newer than it, the flag SHALL be cleared again and the command SHALL exit `0`
- **AND** a pass that is both narrowed and empty — `counts.selected_model_count` of `0` together with `operator_filters.model_ids` naming one model — SHALL carry the empty-pass reason rather than `scope_narrowed`, and SHALL arm rather than leave the flag

#### Scenario: A pass missing the counts block has unknown scope
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a full `operator_filters` mapping, `backfill.enabled` true, the full `sources` set, a positive `cycle_window.lookback_hours`, the default `runtime_config.allowed_cycle_hours_utc`, and no `counts` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown` and SHALL arm the hidden-pass flag
- **AND** the same SHALL hold when `counts` is present but carries no `selected_model_count`, and when that value is present but is not an integer

#### Scenario: A transparent pass is never reclassified as narrowed
- **WHEN** a scanned pass carries status `lock_contended` and, as such a pass structurally does, no `backfill` key
- **THEN** it SHALL keep its transparent classification and SHALL NOT be reported with reason `scope_narrowed`
- **AND** when it is newer than a scope-complete decidable pass that listed no action, the command SHALL exit `0`

#### Scenario: An unnarrowed pass carrying the empty filter mapping is scope-complete
- **WHEN** a scanned pass carries `backfill.enabled` true and an `operator_filters` mapping whose `basin_ids` and `model_ids` are empty and whose `expression` is null
- **THEN** that pass SHALL be treated as scope-complete
- **AND** when it lists no action and no newer pass hides anything, the command SHALL exit `0`

#### Scenario: A narrowed pass that does list a blocked action still reports it
- **WHEN** a scope-narrowed pass carries a blocked candidate of a listed decision
- **THEN** that candidate SHALL appear in `operator_actions`
- **AND** the command SHALL exit `1`

#### Scenario: An evaluating pass missing the backfill key arms the hidden-pass flag
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status and an `operator_filters` mapping but no `backfill` key
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`

#### Scenario: A partially written scope block is unreadable, not scope-complete
- **WHEN** no action is listed, an older scanned pass is scope-complete and decidable, and the newest pass carries an evaluating status, a `backfill` mapping with `enabled` true, and an `operator_filters` mapping that is empty
- **THEN** that pass SHALL be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `3`, never `0`
- **AND** the same SHALL hold when the newest pass carries a full `operator_filters` mapping and a `backfill` mapping with no `enabled` key

#### Scenario: A missing-key pass older than a scope-complete pass does not force exit 3
- **WHEN** no action is listed, the oldest scanned pass carries an evaluating status and a `backfill` mapping but no `operator_filters` key, and a scope-complete decidable pass is newer than it
- **THEN** that older pass SHALL still be reported under `non_evaluating_passes` with reason `scope_unknown`
- **AND** the command SHALL exit `0`, because the newer scope-complete pass looked everywhere after it

#### Scenario: A key published only when Slurm execution is enabled still carries a disposition
- **WHEN** the closure test runs its evaluating passes through the db-free doubles
- **THEN** it SHALL run at least one leg with Slurm execution enabled in addition to the planning-only leg
- **AND** the union of the keys those legs publish SHALL be the authority both directions of the closure are checked against
- **AND** `slurm_preflight`, which only the Slurm-enabled leg publishes, SHALL carry a disposition like every other published key

### Requirement: Completed-type terminal skips SHALL yield to a newer failure truth

The candidate decision SHALL classify a candidate as `terminal_pipeline_success` or `terminal_completed_cycle` only when the success truth backing that classification is at least as new as the candidate's latest failure truth. This is the same rule `terminal_hydro_success` already applies. Latest failure truth is the newest failed pipeline job or failure event in the candidate's decision state, excluding repaired-stage evidence and manual-retry markers as the hydro leg does. A permanence re-label SHALL NOT count as a new failure: the `permanently_failed` mark event is ignored, and a `permanently_failed` job row contributes only its submission-side time (`submitted_at`, then `created_at`), because the mark rewrites both `updated_at` and `finished_at`. When the latest failure truth is strictly newer, the candidate SHALL NOT receive either completed-type skip. It SHALL instead reach the existing failure path (`retry_failed_candidate`, subject to the missing-forcing block and the stage-scoped retry budget). As a result, no skip→forced-retry rewrite (journal-predecessor identity quarantine, `terminal_run_manifest_missing`, strict warm-start mismatch) is fed by a success that a newer failure has superseded. Equal timestamps SHALL keep the terminal classification. A success truth without a timestamp SHALL keep the pre-change classification. For the same reason, a durable-SHUD downstream resume that would restart `forecast` itself SHALL NOT be emitted when the `hydro_run` truth it would reuse is strictly older than the latest failure truth; the candidate SHALL take the failure path instead.

#### Scenario: A failed quarantine rerun is not re-read as terminal
- **GIVEN** a completed cycle with a stale recorded init-state token, no operator confirmation, and no completed quarantine-stamped master
- **WHEN** the scheduler emits `retry_journal_predecessor_identity_mismatch`, the rerun fails at `forecast`, and the scheduler runs more passes than the retry limit
- **THEN** no later pass emits a quarantine retry derived from a completed-type skip; the candidate state decides the budgeted failure path (`retry_failed_candidate`, or `permanent_failure` once the inline retry service has declined at the retry limit)
- **AND** the total number of forecast submissions does not exceed one plus the retry limit; with the production inline retry service wired, the candidate ends blocked rather than still submitting

#### Scenario: An older failure does not demote a newer success
- **WHEN** a candidate's newest terminal-success completion-stage row is newer than or equal in time to its latest failure row
- **THEN** the decision remains `terminal_pipeline_success` (or `terminal_completed_cycle`) exactly as before this change

#### Scenario: Sibling rewrite legs stop re-firing on a newer failure
- **WHEN** a failure truth is newer than the success that would have produced `terminal_run_manifest_missing` or a strict warm-start terminal mismatch retry
- **THEN** neither forced retry is emitted from the completed skip and the candidate follows the failure path

#### Scenario: A permanence re-label of an older failure does not demote a newer success
- **WHEN** a failure older than the candidate's success is later marked `permanently_failed` by a declined retry
- **THEN** the candidate remains terminal

### Requirement: §8.7 identity authority SHALL prefer the newer of the completed hydro_run and the terminal-success candidate master

The file-journal completed-pipeline init-state identity SHALL resolve as follows. Among the accepted-submit cohort MASTER rows whose identity map names the model, the scheduler SHALL consider only those that are terminal-success for that model and carry exactly one self-bound identity, and SHALL take the one with the newest `created_at`. When that master's `created_at` is strictly newer than the matching completed `hydro_run` row's `created_at`, the master's identity SHALL win. Otherwise the completed `hydro_run` row SHALL keep priority. `created_at` is the comparison key on both sides because no post-hoc write (status updates, reconciliation, permanence marks) refreshes it, while `updated_at` is rewritten by `update_hydro_run_status`. A newer running or failed rerun SHALL NOT hide an older converged success. The `hydro_run` write path SHALL remain retriable-only. The breaker occurrence count SHALL keep counting completed quarantine-stamped masters only. Every consumer of the accessor — the journal-predecessor identity quarantine and the discovery-side §8.7 scoring — SHALL observe the same resolved identity.

#### Scenario: A same-run_id corrective rerun converges
- **GIVEN** a run completed through the real hydro-run and pipeline terminal write path recording token X
- **WHEN** a same-run_id rerun with quarantine provenance completes and records the correct token Y
- **THEN** the resolved identity is Y, the journal-predecessor identity quarantine returns no decision, and the breaker does not engage

#### Scenario: A rerun that records the stale token still fail-stops
- **WHEN** the same-run_id rerun completes and records the stale token X again
- **THEN** the breaker engages exactly as before this change

#### Scenario: A later failed rerun does not undo a converged identity
- **WHEN** a corrective rerun records Y and completes, and a later rerun for the same model fails
- **THEN** the resolved identity remains Y

#### Scenario: Legacy hydro_run-only identity is unchanged
- **WHEN** no accepted-submit candidate master exists and only the completed `hydro_run` row carries an identity
- **THEN** the resolved identity equals that row's identity

