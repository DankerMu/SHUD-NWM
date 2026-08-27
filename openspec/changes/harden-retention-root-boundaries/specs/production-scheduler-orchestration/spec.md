## MODIFIED Requirements

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

The admitted resolved root set SHALL contain no duplicate or ancestor/descendant
pair. The primary root SHALL take precedence over an overlapping additional root;
among additional roots, the first accepted configured root SHALL take precedence.
Equal aliases SHALL retain the existing silent single-sweep deduplication behavior,
while an unequal overlapping root SHALL be rejected as `root_overlap` with a
`conflicting_root` field naming the accepted winner. No rejected root SHALL
contribute a plan entry or freed-byte count.

The retention receipt SHALL make every entry attributable: its schema version
remains `nhms.production_scheduler.retention.v2`, each planned, deleted, skipped,
and failed entry carries the root it belongs to, and the receipt carries a block
naming the additional-root switch state, window, cutoff, and admitted resolved
additional roots. Entry keys remain root-relative, so the root field disambiguates
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
- **THEN** retention selects that run workspace for deletion, reclaiming its contents
- **AND** the receipt entry names the workspace root as its root.

#### Scenario: cycle-scoped prefixes on an additional root are never selected

- **WHEN** an additional root holds aged `raw/`, `canonical/`, or `forcing/` trees
- **THEN** retention selects none of them in plan or deletion
- **AND** the node-27 display's disk-resident forcing history is unaffected.

#### Scenario: the two retention windows are independent

- **WHEN** the same cycle exists under the primary and an additional root and is expired only under the primary window
- **THEN** retention deletes the primary copy
- **AND** records the additional copy as retained inside its window.

#### Scenario: disabled switch preserves the previous plan exactly

- **WHEN** additional-root coverage is disabled
- **THEN** no additional root is scanned and the previous primary-root plan is preserved key for key.

#### Scenario: invalid primary root never becomes a derived deletion root

- **WHEN** the direct API, scheduler-pass constructor/environment, or cleanup-CLI environment supplies `OBJECT_STORE_ROOT` as `""`, whitespace, or `"relative/store"`
- **AND** aged retention-shaped trees exist under CWD and under the location scheduler normalization would derive beneath the workspace
- **THEN** blank values record `primary_root_blank` and the relative value records `primary_root_not_absolute`
- **AND** neither location is scanned, planned, or removed and the physical trees remain intact.

#### Scenario: coincident roots are swept once

- **WHEN** configured roots resolve to the same absolute path
- **THEN** each target appears exactly once
- **AND** freed bytes count each completed removal once.

#### Scenario: unequal overlapping roots are rejected deterministically

- **WHEN** primary A and additional B, or two additional roots A and B in either configuration order, have an ancestor/descendant relationship, including `B=A/runs/<canonical_run_id>/nested`
- **THEN** the primary wins, or the first accepted additional wins when no primary is involved
- **AND** the loser is omitted from scanning and records `root_overlap` with `conflicting_root` naming the winner
- **AND** no loser target or duplicate freed-byte contribution is produced.

#### Scenario: window attribution survives evidence compaction

- **WHEN** size compaction strips per-entry retention details
- **THEN** the compacted block still carries the additional-root and frontier blocks.

#### Scenario: the pass forwards its configured additional roots

- **WHEN** a scheduler pass has additional-root coverage enabled and explicit workspace and copyback roots
- **THEN** every non-overlapping admitted root appears in the receipt and its aged runs are attributable to it.

#### Scenario: an additional root defaulted rather than configured is not swept

- **WHEN** a workspace root comes only from a built-in default
- **THEN** it is not forwarded or scanned as an additional root.

#### Scenario: a relative additional root is discarded, not resolved

- **WHEN** an additional root is relative
- **THEN** it is discarded with a recorded reason
- **AND** no working-directory path is selected.

#### Scenario: an unset or blank additional root is discarded, not resolved

- **WHEN** an additional root is unset, empty, or blank
- **THEN** it is discarded before resolution and omitted from admitted roots.

#### Scenario: a failed removal on an additional root is isolated

- **WHEN** removing one selected additional-root workspace raises an OS or safe-filesystem error
- **THEN** it is recorded as failed while remaining entries continue
- **AND** freed bytes count only completed removals and the pass does not abort.

#### Scenario: a symlinked runs directory does not extend the deletion surface

- **WHEN** an additional root's `runs/` entry is a symbolic link
- **THEN** the root is skipped with a recorded reason and no target outside it is planned or removed.

#### Scenario: descendant links are unlinked without following

- **WHEN** a selected additional-root run workspace contains top-level and nested symbolic links to targets outside the root
- **THEN** retention unlinks the links, removes the complete run workspace in that pass, and leaves every target byte-identical
- **AND** a second pass neither plans nor fails that removed workspace.

#### Scenario: a missing additional root is a silent no-op

- **WHEN** an admitted additional root is absent or lacks `runs/`
- **THEN** it produces no targets or exception and the pass completes normally.
