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

Roots that resolve to the same absolute path SHALL be swept exactly once, so a
single-root deployment sees no plan drift and no double-counted freed bytes.

The retention receipt SHALL make every entry attributable: its schema version is
raised to `nhms.production_scheduler.retention.v2`, each planned, deleted,
skipped, and failed entry carries the absolute root it belongs to, and the
receipt carries a block naming the additional-root switch state, window, cutoff,
and the resolved additional roots. Entry keys remain root-relative, so the root
field is what disambiguates identically named runs across roots.

The additional-root block SHALL survive scheduler evidence size compaction, so a
pass large enough to have its per-entry retention detail stripped still discloses
which window governed the additional roots. Deletion failures on an additional
root SHALL be recorded per entry and SHALL NOT abort the sweep or the pass,
including failures raised as safe-filesystem errors rather than OS errors.

An additional root SHALL be an explicitly configured absolute path. A root that
is unset, empty, or blank SHALL be discarded before any path resolution, and a
root whose configured value is relative SHALL be discarded with a recorded
reason rather than resolved — so that no additional root can resolve against the
process working directory. A root SHALL NOT be forwarded when its value comes
from a built-in default rather than from explicit configuration, even where that
default has already been anchored to an absolute path upstream.

Deletion on an additional root SHALL stay inside that root. An additional root
whose `runs/` entry is a symbolic link SHALL be skipped with a recorded reason
rather than followed, and removal of a selected run workspace SHALL NOT follow
symbolic links out of the resolved root.

The adjudication order, the pipeline-frontier exemption, the protected-prefix and
static-segment protections, the published-artifact protection, and the contract
that cleanup never aborts scheduling SHALL all apply to additional roots
unchanged.

#### Scenario: additional root reclaims its aged run workspaces

- **WHEN** additional-root coverage is enabled and the scheduler workspace root
  differs from the object-store root
- **AND** the workspace root holds `runs/<canonical_run_id>` whose cycle is older
  than the additional-root cutoff and older than the active lower bound
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

#### Scenario: coincident roots are swept once

- **WHEN** additional-root coverage is enabled and every configured root resolves
  to the same absolute path
- **THEN** each target appears exactly once in the plan
- **AND** the freed byte total counts each reclaimed directory exactly once.

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
- **THEN** both are among the resolved additional roots recorded in the receipt
- **AND** an aged run workspace under either of them is selected, attributed to
  the root it belongs to.

#### Scenario: an additional root defaulted rather than configured is not swept

- **WHEN** the scheduler workspace root is not explicitly configured, so its
  value comes from the built-in default
- **THEN** it is not forwarded as an additional root, and nothing beneath it is
  selected for deletion
- **AND** the resolved additional roots recorded in the receipt do not include it.

#### Scenario: a relative additional root is discarded, not resolved

- **WHEN** an additional root is configured with a relative value
- **THEN** it is discarded with a recorded reason and never resolved
- **AND** no directory under the process working directory is selected for
  deletion.

#### Scenario: an unset or blank additional root is discarded, not resolved

- **WHEN** an additional root is configured as unset, empty, or blank
- **THEN** it is discarded before any path resolution and never appears among the
  resolved additional roots
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

#### Scenario: a missing additional root is a silent no-op

- **WHEN** additional-root coverage is enabled and a configured additional root
  does not exist, or exists without a `runs/` directory
- **THEN** retention records no targets for it and raises nothing
- **AND** the scheduling pass completes normally.

