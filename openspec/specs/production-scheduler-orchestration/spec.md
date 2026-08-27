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

Scheduler candidate evidence SHALL treat cycle terminal `reconciling` and stage/candidate statuses `submit_result_ambiguous` and `reconcile_unverified` as incomplete reconciliation outcomes. Such evidence SHALL be partial and non-successful, but SHALL NOT manufacture a failed candidate.

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

#### Scenario: Pending status without confirmed identity remains non-submitted

- **WHEN** a scheduler candidate has a reconciliation-pending status but the current stage loop has never observed a confirmed Slurm identity
- **THEN** model-run evidence SHALL keep `submitted=false`
- **THEN** no producer SHALL turn the pending token itself into a positive submission proof

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

- **WHEN** the submit-attempt commit writer receives an accepted transition carrying `operator_verified_absence`, the cohort defer or cohort task-projection writer receives the raw decision token, or ordinary pipeline-job upsert receives the token while creating or upgrading a current-contract row
- **THEN** each current-contract writer rejects it with the typed-authority error before row construction, lock acquisition, durable mutation, or event, the journal stays byte-identical, and existing legitimate decisions and non-token legacy upgrades still apply unchanged; legacy transition/reconciliation writer compatibility is tracked separately in #1805

#### Scenario: Committed reclaim never strands a pre-sbatch live reservation

- **WHEN** the public old-ID operator recovery path commits the reclaim authority append and a derived direct or inventory projection write then fails before any submission
- **THEN** the failure SHALL NOT be reported as an uncommitted failure that leaves a live `reserved` row: the flow either completes the single submission path or transitions the row to a non-live retryable authority state under the lock, and the next public pass does not fail with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: The receipt locator uses the one safe journal-root authority

- **WHEN** `--journal-root` is a symlink loop or a literal unexpanded tilde path
- **THEN** the loop root fails through the typed operational error path before the authority append with no traceback and zero journal bytes, and the tilde root's success receipt locator equals the expanded authority root actually used by repository reads and writes

