## ADDED Requirements

### Requirement: A pinned containment root SHALL change only where `stat_no_follow` gets its root handle

`pinned_containment_root(root)` SHALL open the containment root once, through
the same no-follow chain every containment-rooted primitive uses, and keep
that handle for the duration of the block. Inside the block,
`stat_no_follow(path, containment_root=root)` SHALL start its walk from a
duplicate of that handle and SHALL otherwise run the checks it runs outside a
block: the under-root containment check, a no-follow open of every component
between the root and the parent, both handle/path identity checks, and the
refusal of a symlink leaf. For every tree state that does not change between the entry of the block and
the call, its result, or the class and `kind` of its exception, SHALL equal
what the same call returns or raises outside a block.

The pin SHALL apply only to calls naming the same root. No other primitive
SHALL consult it. Entering a block SHALL NOT raise because the root cannot be
opened; in that case nothing is pinned and calls inside behave as outside.
The handle SHALL be closed and the pin removed on every exit path. The pin
SHALL NOT be visible to a thread that does not copy the entering thread's
context, and a copied context SHALL NOT find a usable pin after the block
has exited.

A component above the root replaced, during a block, by a symlink that
resolves to the same directory is outside what the block detects; it SHALL
be refused by the first call of the next block. The root itself replaced by
such a symlink SHALL be refused by the next call of the block whose parent
directory is the root.

#### Scenario: same outcome inside and outside a block

- **WHEN** `stat_no_follow(path, containment_root=root)` is called for a
  regular entry, an absent leaf, an absent root, a symlink leaf, a symlinked
  or non-directory component under the root, a symlinked root, and a path
  outside the root
- **THEN** each call inside a `pinned_containment_root(root)` block returns
  the same stat identity, or raises the same exception class and `kind`, as
  the call outside a block

#### Scenario: the root chain is opened once per block

- **WHEN** several paths under one root are stat'ed inside one block
- **THEN** the root's no-follow chain is opened once, and a call that names
  another root or no root opens its own

#### Scenario: nothing outlives the block

- **WHEN** a block exits, normally or by exception
- **THEN** the pinned handle is closed, no pin remains for the thread, and
  the number of open descriptors is what it was before the block

#### Scenario: a swap under the root is seen by the next call

- **WHEN** a directory under the root is replaced by a symlink between two
  calls of one block
- **THEN** the second call raises `SafeFilesystemError`

#### Scenario: the root replaced by another directory is refused

- **WHEN** the root is replaced by a different directory between two calls
  of one block
- **THEN** the second call raises `SafeFilesystemError` because its handle
  and its path no longer name the same directory

#### Scenario: a component above the root replaced by a symlink is refused by the next block

- **WHEN** a directory above the root is replaced, during a block, by a
  symlink that resolves to the same directory
- **THEN** later calls of that block return the real entries' stats, and the
  first call of the next block raises `SafeFilesystemError`

#### Scenario: the root replaced by a symlink is refused at the root's own children

- **WHEN** the root is replaced, during a block, by a symlink that resolves
  to the same directory
- **THEN** the next call of that block for a path directly under the root
  raises `SafeFilesystemError`

#### Scenario: the root renamed away is refused

- **WHEN** the root is renamed away during a block and nothing is put at its
  path
- **THEN** the next call of that block raises `SafeFilesystemError`

#### Scenario: a copied context finds no pin after the block

- **WHEN** a context is copied inside a block and a stat runs in it after
  the block has exited
- **THEN** that stat opens the root chain itself
