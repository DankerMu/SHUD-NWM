## ADDED Requirements

### Requirement: Completion scoring SHALL decide through the candidate-state ladder without building per-event evidence

When cycle completion scoring consults candidate state, it SHALL obtain each in-scope model's decision from the same
candidate-state decision ladder the submitting path uses, so the terminal decisions that ladder returns decide the
verdict exactly as before.
Because completion scoring neither reads nor publishes the per-event evidence listing, it SHALL request the decision
without that listing: the decision's evidence SHALL then omit `pipeline_events` altogether and SHALL be otherwise
identical. The submitting path SHALL keep requesting the full evidence.

#### Scenario: The decision is the same with and without the event listing

- **WHEN** the ladder is evaluated twice for the same candidate and the same candidate state, once with and once
  without the event evidence listing
- **THEN** both decisions have the same `action` and `reason`
- **AND** the evidence of the second equals the evidence of the first with the `pipeline_events` key removed

#### Scenario: Completion scoring does not sanitise events

- **WHEN** a cycle whose candidates carry pipeline events is scored for completion
- **THEN** the decision evidence completion scoring receives carries no `pipeline_events` key
- **AND** the verdict equals the verdict the full-evidence decision would produce, including for a cycle that scores
  `complete` because a recalibrated model is scoped out by its lineage cutover

#### Scenario: Submission still publishes the event listing

- **WHEN** a candidate is evaluated on the submitting path
- **THEN** its decision evidence carries `pipeline_events` as before

## MODIFIED Requirements

### Requirement: Retention covers every configured run-workspace root

The scheduler's end-of-pass retention SHALL be able to reclaim aged `runs/`
workspaces under run-workspace roots other than the object-store root — the
scheduler workspace root and the object-store copyback root — using an
independent retention window, while leaving every non-`runs/` prefix on those
additional roots untouched, except the root-level Slurm log lane on the scheduler
workspace root defined by "Retention reclaims aged root-level Slurm log directories
on the scheduler workspace root".

Additional-root coverage SHALL be gated by an explicit enablement switch whose
default is disabled, so that adopting this capability is an operational decision
separate from shipping it. When the switch is disabled, the retention plan SHALL
be identical, key for key, to the plan produced before this capability existed.

Additional roots SHALL be swept `runs/`-only, except that the scheduler workspace
root additionally carries the root-level Slurm log lane. Cycle-scoped prefixes (`raw`,
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
include `raw|canonical|forcing/<source>/<valid_cycle>/**`; the scheduler workspace
root named as the Slurm log root additionally includes `<canonical_run_id>/**` and
`(gfs|ifs|era5)_<valid_cycle>/**` directly beneath it. A root at or below one
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

In scheduler pass evidence the skipped list SHALL NOT repeat the entries whose reason is `within_retention_window`:
those are attributed through `skipped_summary`, one row `{root, reason, count}` per root and reason over all skipped
entries, whose counts sum to `counts.skipped`, and the block SHALL name the omitted reason in `skipped_omits`. Every
skipped entry with any other reason SHALL stay listed with its root. The standalone retention receipt SHALL keep
listing every entry.

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
- **THEN** retention skips that root's `runs/` lane with a recorded reason and selects no `runs/` entry
- **AND** no path outside the resolved root appears in the plan or is removed
- **AND** on the scheduler workspace root, the root-level Slurm log lane is evaluated independently.

#### Scenario: descendant links are unlinked without following

- **WHEN** a selected additional-root run workspace contains top-level and nested symbolic links to targets outside the root
- **THEN** retention unlinks the links, removes the complete run workspace in that pass, and leaves every target byte-identical
- **AND** a second pass neither plans nor fails that removed workspace.

#### Scenario: a missing additional root is a silent no-op

- **WHEN** additional-root coverage is enabled and a configured admitted
  additional root does not exist, or exists without a `runs/` directory
- **THEN** retention records no `runs/` targets for it and raises nothing; a missing root records no targets at all
- **AND** an existing scheduler workspace root without `runs/` still has its root-level Slurm log lane evaluated
- **AND** the scheduling pass completes normally.

#### Scenario: Pass evidence summarises the within-window skips

- **WHEN** a scheduler pass's retention skips many keys because they are within the retention window, some across two roots, and one because its cycle name is unparseable
- **THEN** pass evidence `retention.skipped` holds the unparseable entry with its root and no `within_retention_window` entry
- **AND** `retention.skipped_summary` holds one row per root and reason whose counts sum to `retention.counts.skipped`
- **AND** `retention.skipped_omits` is `["within_retention_window"]`
- **AND** the standalone retention receipt of the same plan lists every skipped entry
