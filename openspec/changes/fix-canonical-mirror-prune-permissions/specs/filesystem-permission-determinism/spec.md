# Spec Delta: filesystem-permission-determinism

## MODIFIED Requirements

### Requirement: copyback intermediate directories are made traversable by their creator

The object-store copyback callers SHALL make every intermediate directory they
create on the path to a copyback target traversable by the consuming account,
because `safe_fs` deliberately lets the ambient umask restrict what it creates
and never widens it afterwards. "On the path to" is deliberate and includes
levels *above* the copyback root: the callers that prepare the root pass no
containment root, so a missing ancestor of the root is created by the same call,
and an ancestor left at `0o750` defeats traversal exactly as a level below the
root would.

The widening SHALL apply only to directory levels the creating call itself
created, and SHALL NOT change the mode of a level that already existed at the
time the call probed for it. "Created by this call" is decided by that probe,
not by the `mkdir` return: `safe_fs.ensure_directory_no_follow` absorbs
`FileExistsError`, so a level that a concurrent creator wins between the probe
and the `mkdir` is still widened. That residue is accepted rather than fixed —
closing it would mean either changing `safe_fs` (out of scope, see below) or
re-probing after creation, which only narrows the window instead of removing it.
It is bounded in practice: every directory-tree writer beneath the root holds
the copyback batch mutex, and the only mutex-exempt writers work in disjoint
subtrees, so the levels genuinely open to the race are the root and its
ancestors, where `0o755` is the intended mode anyway. The
copyback root itself is one such level whenever the call creates it: a root left
at `0o750` defeats traversal no matter what the levels below it carry. It is
applied unconditionally to those levels: an inherited POSIX default ACL does not
change the outcome, because the explicit mode `safe_fs` passes to `os.mkdir`
already fixed the new level's ACL mask before the caller runs.

The trees a copyback lane *copies* (the temp tree it promotes with one
`os.replace`, and the clone the commit phase keeps for rollback) are a separate
surface from the traversal-widened levels above them: their directories are set
by the lane's tree chmod, whose directory mode is lane-scoped. The `runs/` and
`forcing/` lanes keep `0o755`, so the ACL-mask neutrality described below still
holds for them. The canonical precipitation mirror lane SHALL set its copied
trees (`canonical/<storage_source>/<cycle>/prcp_rate_or_amount/` and
`canonical/<storage_source>/grid/<grid_id>/`) and their commit clones to an
explicit `0o2775`, and SHALL additionally assert `0o2775` on
`canonical/<storage_source>/<cycle>/` — a traversal-widened level — through a
descriptor-bound `fchmod` before the temp tree is created and whether or not
the call created that level. `0o2775` is written explicitly on every one of
those directories; nothing relies on a setgid bit surviving a chmod. The setgid
bit on `<cycle>/` is what makes the temp tree inherit the shared group at
`mkdir`, so a cycle tree created under a `2775`, shared-group
`canonical/<storage_source>/` is deletable by the node-27 retention account
(#2100). Files in every lane stay `0o644`.

`packages/common/safe_fs.py` itself remains unchanged: it still passes an
explicit `0o755` base mode to `os.mkdir` and still never `chmod`s a directory
after creating it.

#### Scenario: a restrictive umask no longer hides the canonical mirror

- **WHEN** a publisher-side copyback runs with the process umask set to `0o027`
  and creates the copyback root, `canonical/`, `canonical/<source>/`,
  `canonical/<source>/<cycle>/` and `canonical/<source>/grid/`
- **THEN** the root, `canonical/`, `canonical/<source>/` and
  `canonical/<source>/grid/` — the traversal-widened levels the lane does not
  own — land with mode `0o755`
- **AND** `canonical/<source>/<cycle>/` (asserted by the lane),
  `canonical/<source>/<cycle>/prcp_rate_or_amount/` and
  `canonical/<source>/grid/<grid_id>/` (the copied trees) land with mode
  `0o2775`, and no level carries an other-write bit
- **AND** the consuming account can traverse the whole chain and read the bytes
  of a mirrored file.

#### Scenario: the ordinary umasks are unchanged

- **WHEN** the same copyback runs under umask `0o022` or `0o002`
- **THEN** each traversal-widened level the lane does not own lands with mode
  `0o755` and carries no group- or other-write bit
- **AND** `<cycle>/` and the canonical precipitation copied trees land with
  mode `0o2775` exactly as under `0o027`, because both modes are explicit and
  not derived from the umask.

#### Scenario: the canonical precipitation trees take the shared group of their source root

- **WHEN** `canonical/<source>/` is `0o2775` with a shared group before the
  copyback runs
- **THEN** the promoted `canonical/<source>/<cycle>/` and its
  `prcp_rate_or_amount/` carry that group and mode `0o2775`
- **AND** the mirrored files carry that group and mode `0o644`.

#### Scenario: a pre-existing cycle directory is converged when its tree is copied

- **WHEN** `canonical/<source>/<cycle>/` already exists at `0o755` and the
  destination bytes differ from the source
- **THEN** after the mirror `<cycle>/` is `0o2775` and the promoted tree is
  `0o2775`
- **AND** when the destination is identical (the tree is `skipped`) the
  `0o755` `<cycle>/` is left untouched.

#### Scenario: a rolled-back canonical tree is restored at the lane mode

- **WHEN** the commit phase of a canonical precipitation batch fails after
  taking its rollback clone and the lane rolls the batch back
- **THEN** the restored `prcp_rate_or_amount/` is `0o2775`.

#### Scenario: sibling lanes keep the narrower mode

- **WHEN** the `runs/` or `forcing/` copyback lane promotes a tree
- **THEN** its directories land with mode `0o755` and its files `0o644`,
  unchanged by the canonical precipitation lane's mode.

#### Scenario: an already-existing intermediate directory keeps its mode

- **WHEN** an intermediate directory beneath the copyback root already exists with
  a restrictive mode when the copyback caller probes for it
- **THEN** the copyback caller MUST leave its mode unchanged, the one
  exception being `canonical/<storage_source>/<cycle>/`, which the canonical
  precipitation lane owns and asserts as described above
- **AND** the sole further exception is the accepted race in the requirement
  above — a level created by someone else between this call's probe and its
  `mkdir` — which MUST stay documented as an accepted limit rather than being
  presented as compliance.

#### Scenario: the run-tree interior stays ACL-mask-preserving

- **WHEN** `run_tree_copyback` copies a run tree's interior directories with a
  mode-less `mkdir`
- **THEN** that behavior is unchanged by this requirement
- **AND** those interiors still inherit `mask::rwx` from the parent's default ACL.
- **AND** this is a property of that writer's path, not of `runs/` interiors in
  general: the q_down product lane reaches the same `runs/<run_id>/` key through
  `_copyback_collected_object_tree`, which creates every interior with an
  explicit mode, so the clamp happens at the `mkdir` — as stated below — and
  interiors that lane creates carry `mask::r-x` instead. Both are
  readable by the consuming account — the clamp costs write, and that lane's
  consumer only reads — so the divergence is recorded, not reconciled here.
