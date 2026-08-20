## ADDED Requirements

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

## MODIFIED Requirements

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
