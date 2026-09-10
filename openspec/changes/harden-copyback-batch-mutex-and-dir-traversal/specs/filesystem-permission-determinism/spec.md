## ADDED Requirements

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

`packages/common/safe_fs.py` itself remains unchanged: it still passes an
explicit `0o755` base mode to `os.mkdir` and still never `chmod`s a directory
after creating it.

#### Scenario: a restrictive umask no longer hides the canonical mirror

- **WHEN** a publisher-side copyback runs with the process umask set to `0o027`
  and creates the copyback root, `canonical/`, `canonical/<source>/`,
  `canonical/<source>/<cycle>/` and `canonical/<source>/grid/`
- **THEN** each of those levels, the root included, lands with mode `0o755`
- **AND** the consuming account can traverse the whole chain and read the bytes
  of a mirrored file.

#### Scenario: the ordinary umasks are unchanged

- **WHEN** the same copyback runs under umask `0o022` or `0o002`
- **THEN** each created level lands with mode `0o755`
- **AND** no created level carries a group- or other-write bit.

#### Scenario: an already-existing intermediate directory keeps its mode

- **WHEN** an intermediate directory beneath the copyback root already exists with
  a restrictive mode when the copyback caller probes for it
- **THEN** the copyback caller MUST leave its mode unchanged
- **AND** the sole exception is the accepted race in the requirement above — a
  level created by someone else between this call's probe and its `mkdir` — which
  MUST stay documented as an accepted limit rather than being presented as
  compliance.

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

## MODIFIED Requirements

### Requirement: the directory-mode invariant is scoped to safe_fs

The invariant above SHALL be read as a property of directories created by
`packages/common/safe_fs.py`, not as a property of any directory tree. Other
production directory producers — an explicit wide `os.mkdir(..., 0o777)`, a bare
`Path.mkdir`, or `os.makedirs` — are outside this capability and keep their
current behavior.

Where a directory must be shared across uids, the sharing SHALL be established by
the caller after creation — an explicit `chmod`, or a permissive parent mode from
which the child's own mode is irrelevant. An inherited POSIX ACL on the parent is
**not** sufficient on its own: an explicit mode passed to `mkdir` clamps the
inherited ACL mask, so a named-user grant degrades to `#effective:r-x` on any
directory safe_fs creates.

An ACL restores cross-uid write only when combined with a post-creation `chmod`
that widens the mask again.

That clamp is performed by the `mkdir` call itself, not by anything a caller does
afterwards, and a later `chmod` re-derives the mask from the **group** bits of the
mode it is given. Consequently a caller-side `chmod` whose group bits are `r-x` —
`0o755`, as the copyback traversal widening performs — SHALL be understood as
mask-neutral on a safe_fs-created directory: the mask is already `r-x` and stays
`r-x`. Such a caller is therefore not required to probe for an inherited ACL
before widening.

This SHALL NOT be read as a claim about `chmod` in general. A `chmod` with wider
group bits does move the mask: `0o775` restores `mask::rwx`, which is exactly how
the state copyback parent re-establishes its cross-uid grant, and a narrower mode
clamps it further. A caller that needs cross-uid **write** through an inherited
ACL still requires such a widening `chmod` and SHALL NOT have it narrowed to
`0o755`.

#### Scenario: a non-safe_fs producer is unchanged

- **WHEN** a production code path creates a directory with an explicit wide mode
  or a bare `Path.mkdir` rather than through safe_fs
- **THEN** its landed mode is unchanged by this capability

#### Scenario: an `r-x`-group widening under an inherited default ACL changes nothing

- **WHEN** safe_fs creates a directory beneath a parent carrying
  `default:user:<consumer>:rwx` and `default:mask::rwx`
- **THEN** the new directory's mask is already `r-x` and the grant already shows
  `#effective:r-x`
- **AND** a subsequent caller-side `chmod 0o755` leaves that mask at `r-x`,
  neither restoring nor further restricting it.

#### Scenario: a wider group mode still restores the mask

- **WHEN** a caller widens a level it created with `chmod 0o775`, as the state
  copyback parent does
- **THEN** the inherited ACL mask becomes `rwx` again and the named-user grant is
  effective for write
- **AND** that behavior is unchanged by this capability; it MUST NOT be narrowed
  to `0o755` on the grounds that the copyback traversal widening uses `0o755`.
