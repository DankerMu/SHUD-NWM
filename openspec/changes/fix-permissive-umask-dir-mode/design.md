# Design

## Context

The bug is a two-party interaction, not a defect in either party alone:

| Party | File:line | Behavior |
|---|---|---|
| Producer | `packages/common/safe_fs.py:68` | `os.mkdir(part, dir_fd=fd)` — no mode, implicit base `0o777` |
| Consumer | `packages/common/provider_atomic.py:209` | rejects a lock parent with any `0o022` bit |

Neither is wrong in isolation. The producer is *underspecified*: it lets the
environment decide a permission that a security gate later depends on.

## Decisions

### D1: Pin the base mode, do not add a `mode` parameter

`os.mkdir(part, 0o755, dir_fd=fd)`. No new keyword argument on
`ensure_directory_no_follow`.

Rationale: the only production caller that wants a *different* directory mode —
`packages/common/state_manager.py:2145-2153`, `_ensure_copyback_state_parent` —
already handles it by chmod-ing to `0o775` after creation, and deliberately only
for components **it** created in that call. No caller needs a parameter, so
adding one would be speculative surface on a shared helper with 119 call sites.

### D2: Explicit mode, and **no** `fchmod`

The issue text proposes `os.mkdir(part, 0o755, ...)` *plus* an `os.fchmod` on
newly created directories "to squash the umask influence". The second half is
rejected as a **loosening regression**.

Measured (`os.mkdir` with and without an explicit mode, three umasks):

| umask | mode-less | explicit `0o755` | gate (`& 0o022`) |
|---|---|---|---|
| `0002` | `0o775` | `0o755` | unsafe -> safe |
| `0022` | `0o755` | `0o755` | safe, unchanged |
| `0077` | `0o700` | `0o700` | safe, unchanged |

The kernel applies the umask to an explicit mode exactly as it does to the
implicit `0o777`, and a umask can only *clear* bits. So the explicit mode alone
is already sufficient for the gate property (`0o755 & 0o022 == 0`) under every
umask, while `fchmod` would additionally turn the umask-`0077` case from `0o700`
into `0o755` — silently widening private directories on the strictest hosts.

Note the widening would have landed **silently**: no test in the repository pins
a *directory* mode under `0o077`. Re-counted on the merged head there are **ten**
pre-existing `os.umask(0o077)` sites across four files —
`tests/test_scheduler_file_provider_refresh.py:853`, `:967`;
`tests/test_state_manager.py:3688`, `:3897`, `:3922`, `:3964`;
`tests/test_run_tree_copyback.py:213`, `:1359`;
`tests/test_scheduler_state_index_copyback_replay.py:1177`, `:1210` — and every
one of them asserts a *file* mode or no mode at all. (An earlier revision of this
section said "three"; that count predated the `origin/master f087f08d` merge,
which added the `#1609`/`#1610` sites. The conclusion is unchanged and was
re-verified across all ten.) That absence is the reason task 2.2 exists — it is
new coverage, not a guard that already existed.

Governing rule to encode: **the umask may further restrict a safe_fs directory;
it may never loosen it.**

### D3: The gate stays fail-closed on pre-existing unsafe parents

`provider_atomic.py:209` is unchanged. A lock parent that is already
group/world-writable — whoever created it — still raises
`provider_lock_parent_unsafe` in `precommit`.

Rejected alternative: relax the gate to "uid match is enough". That reopens the
window the gate exists to close: on a shared directory, any member of the group
can pre-create or swap the `.lock` inode between the check and the open. The
cost of keeping it closed is that callers must own their parent's permissions —
which is exactly what D1 makes true for safe_fs-created parents.

Corollary, and the reason `(b)` exists: safe_fs is not the only creator of lock
parents. Anything that pre-creates a directory and then hands it to a provider
must satisfy the gate itself.

### D4: `(b)` fixes helper chokepoints, not 1398 call sites

`tests/` contains ~1398 mode-less `mkdir`/`makedirs` calls. Rewriting them all
would be scope inflation with no risk reduction: only a directory that ends up
as the **direct parent** of a provider lock matters.

The fix set is therefore **empirically bounded**: fix the helpers the umask-`002`
run implicates, re-run, repeat until zero. Two chokepoints are already
identified — `_scheduler_env_roots` (creates the seven scheduler roots) and
`_set_db_free_scheduler_env` (creates `<root>/object-store/db-free`, the measured
trip point) — plus per-test sites such as the one in
`test_nfs_raw_ready_candidate_stages_raw_before_convert_submit`.

`Path.mkdir(mode=..., parents=True)` applies `mode` to the **leaf only**;
ancestors created by `parents=True` still take `0o777 & ~umask`. The shared test
helper must therefore set the mode per created component, not rely on the `mode`
kwarg. `state_manager._ensure_copyback_state_parent` is the in-repo idiom to
follow: create, and chmod only what this call created.

### D5: Helper placement avoids the CI selector carve-out

The new test helper must **not** live in `tests/conftest.py` or
`tests/integration_helpers.py`. Both are an explicit issue-#1487 carve-out in
`scripts/select_ci_tests.py:232-234`, mapped at `:936` to the selector meta-guard
suite.

Precision, because the naive version of this rationale is wrong: the mapping is
*additive*, and collapse requires the final selection to be **exactly** the
meta-guard (`scripts/select_ci_tests.py:1048`). This PR also changes
`packages/common/safe_fs.py` and `tests/test_production_scheduler.py`, both of
which select real suites, so **this** PR would not collapse either way. The rule
is kept because it costs nothing and because a later helper-only follow-up PR
*would* collapse — at which point the helper's location is no longer changeable
cheaply.

### D6: Permissive-side coverage is the durable half

The reason this survived on master is coverage shape, not code: every umask test
in the repository pins `0o077`. The new test pins the `0o002` side — that
safe_fs-created directories carry no `0o022` bit, and that a provider lock
acquires successfully with such a parent. Without it, the next mode-less `mkdir`
on this path reintroduces the bug invisibly.

### D7: Explicit mode clamps an inherited POSIX ACL mask — a real boundary, enumerated clean

Measured on node-27 (`/home/ghdc` — the NFS server for the node-22 handoff), in a
scratch directory carrying `default:user:<other>:rwx`:

| creation call | landed mode | ACL mask | named-user entry |
|---|---|---|---|
| `os.mkdir(p)` | `0o770` | `mask::rwx` | `user:other:rwx` |
| `os.mkdir(p, 0o755)` | `0o750` | `mask::r-x` | `user:other:rwx` **`#effective:r-x`** |
| `os.mkdir(p, 0o775)` | `0o770` | `mask::rwx` | `user:other:rwx` |
| `os.mkdir(p, 0o755)` then `os.chmod(p, 0o775)` | `0o775` | `mask::rwx` | `user:other:rwx` (restored) |

Two consequences that are not obvious and must be recorded:

1. When a default ACL is present the **umask is ignored entirely** — the bare
   `mkdir` above landed `0o770`, not `0o775`, under umask `0002`.
2. `mkdir`'s mode argument **clamps the ACL mask**. So a named-user grant that
   makes cross-uid sharing work is silently reduced to `#effective:r-x` by an
   explicit `0o755`.

There is therefore **no mode-bit strategy that both strips the `0o022` bits and
preserves an ACL mask** — the mask *is* the group bits. A "tightening-only"
`fchmod(mode & ~0o022)` variant fails identically. The governing invariant and
ACL-mask sharing are incompatible **on the same directory**; the resolution is to
keep them on different directories, not to find a cleverer mode.

Note this also means `provider_atomic`'s gate already refuses any lock whose
parent sits in an ACL-shared tree (`0o770 & 0o022 != 0`), independently of umask.
Locks and ACL-shared data cannot coexist in one directory today, and this change
does not alter that.

**Production enumeration (node-27 `/home/ghdc/nwm/object-store`, 2026-08-20).**
The shared NFS root is written by **two uids** — node-22's `frd_muziyao` (1103)
and node-27's `nwm` (1005) — so "single-writer, single-uid" is false for this
tree and must not be used as the safety argument. The real picture, measured:

| subtree | mode | sharing mechanism | `nwm`-owned entries | directory creator | effect of D1 |
|---|---|---|---|---|---|
| `forcing/` | `775` + `default:user:nwm:rwx` | named-user ACL | **0** | safe_fs via `LocalObjectStore` -> `atomic_write_bytes_no_follow` -> `_open_parent_dir(create=True)` (`services/tile_publisher/forcing_copyback_backfill.py:198`, `:210`; `services/tile_publisher/publisher.py:780`, `:937`), **no repairing chmod** | ACL mask clamped on newly created dirs |
| `runs/` | `775` + ACL | named-user ACL | **0** | safe_fs at `services/orchestrator/run_tree_copyback.py:49`, `:375`, `:396`, **no repairing chmod**; interiors by bare `Path.mkdir` at `:427` | mask clamped on the safe_fs-created parents; `:427` interiors unchanged |
| `states/` | `775` + ACL | named-user ACL | **0** | `state_manager._ensure_copyback_state_parent` — safe_fs then `chmod 0o775` on components it created | **none** — the chmod restores `mask::rwx` |
| `raw/` | **`777`**, no ACL | world-writable parent; each uid owns its own children | **3430** | mixed | **none** — see below |
| `models/direct_grid_variants/` | **`1777`** sticky, no ACL | same | 1848 | mixed | **none** — see below |
| `models/`, `scheduler/` | `755`, no ACL | node-22 only | 0 | node-22 | none; umask `0022` already lands `0o755` |

Two independent reasons the two-uid sharing survives D1:

1. **The ACL'd subtrees' grant is unused.** `find` reports **zero** `nwm`-owned
   entries under `forcing/`, `runs/`, or `states/`. node-22 is their sole writer;
   node-27 only reads (display serves station series out of `forcing/`,
   `scripts/node27_autopipeline.py` reads `runs/`). A clamped mask still leaves
   `r-x`, which is exactly what node-27 exercises.
2. **The actually-shared subtrees do not share via group bits at all.** `raw/`
   and `models/direct_grid_variants/` are shared through their **parent** being
   `777` / `1777`-sticky; each uid creates and owns its own children. Those
   children's groups are `nwm` (1005) and `nfsdata` (1078), and **both groups are
   empty** (`getent group nwm` -> `nwm:x:1005:`). The only group containing both
   accounts is `nwmuser` (1107), which none of these directories use. So a child
   at `0o775` grants the *other* account exactly what `0o755` grants it — `r-x`
   via the other-bits. Tightening the group bit takes nothing from anyone.

The `raw/` and `models/direct_grid_variants/` rows name the creator only as
"mixed" because the empty-group argument is **creator-independent** — it holds
whoever creates the child, and it also covers node-22 writing *inside* a
node-27-created child, not merely alongside it. Those two rows therefore carry no
creator evidence, and would need re-deriving if `nwm` (1005) or `nfsdata` (1078)
ever gained members. `getent group nwm` is the check.

Independent in-repo corroboration that the copyback tree already tolerates
mask-clamped directories: `services/tile_publisher/publisher.py:1373` calls
`_chmod_tree_readable`, which chmods **every directory in a copied tree to
`0o755`** and every file to `0o644` before the tree is swapped into the copyback
store (`publisher.py:2394-2409`). So `forcing/` leaf directories on the shared
root already run at a clamped mask in production today, and node-27 reads them
fine. D1's delta is confined to the *ancestors* those copies are created under
(`ensure_directory_no_follow(target_dir.parent, ...)` at `publisher.py:1358`,
which `_chmod_tree_readable` does not reach) — a delta toward a state the tree
already demonstrably tolerates.

This refutes the intuitive failure scenario ("a node-27-created prefix lands
`0o755` and a later node-22 write fails `EACCES`"): at `0o775` that write already
fails today, for the same reason. The change is a no-op for cross-uid access on
this tree.

Two scoping statements, so the enumeration is not read as broader than it is:

- It covers the **object-store** root only. The sibling shared root
  `/ghdc/data/nwm/published` (`NHMS_SCHEDULER_ALLOWED_ROOTS` in
  `infra/env/compute.scheduler-dbfree.env.example:71`) also has safe_fs-created
  directories, from `services/orchestrator/chain_workspace.py:171`, `:210`. It is
  out of scope and inert for an independent reason: no service under `apps/`
  references `NHMS_PUBLISHED_ARTIFACT_ROOT`, so node-27 consumes nothing through
  that path, and node-22 stays the owner with `user::rwx` regardless of any mask.
- The `scheduler/` row states the subtree, not every child:
  `scheduler/direct-grid-candidates` is `1777` with no code reference (operator
  residue, measured in issue #1513). It is not a delta — this change never chmods
  an existing directory — but it is a known exception to that row.

Bounding the whole discussion: `ensure_directory_no_follow` never chmods an
**existing** directory (`safe_fs.py:53-91` has no chmod path), so every prefix in
production today is untouched. The exposure is limited to newly created
directories.

Residual risk, recorded not designed for: if node-27 ever needs to **write** into
`forcing/`, `runs/`, or `states/`, newly created directories would no longer honor
the ACL grant. Tracked as a follow-up, not this change's scope.

### D8: `0o755`, not `0o700`

Issue #1513 asks for an explicit ruling between the two. `0o755` is chosen.

`0o700` would strip group **and other execute**, which breaks directory traversal
for any second account. That is a strictly larger regression than the group-write
question this change exists to solve: the shared NFS root
`/ghdc/data/nwm/object-store` is `drwxrwxr-x` precisely so the other host can
traverse and read it, and node-27's display serves station series by walking
`forcing/<source>/<cycle>/...`. `0o755` removes only the write bits the gate
objects to (`0o022`) and preserves the read/traverse path that the two-node
topology depends on.

`0o755` is also the mode the repository already uses for the analogous explicit
case, `services/orchestrator/scheduler_lease.py:598`
(`os.mkdir(component, 0o755, dir_fd=parent_fd)`).

## Invariant Matrix

Governing invariant: a directory created by `safe_fs` never carries a group- or
other-write bit, under any ambient umask; the umask may only further restrict it.

Source-of-truth identity/contract: the landed `stat.S_IMODE` of a
safe_fs-created directory, and the `0o022` predicate at `provider_atomic.py:209`.

Surfaces:

- Producers: `packages/common/safe_fs.py:53-92` (`ensure_directory_no_follow`),
  reached from `atomic_write_bytes_no_follow` / `_open_parent_dir` with
  `create=True`.
- Validators/preflight: `packages/common/provider_atomic.py:209` (unchanged).
- Storage/cache/query: `packages/common/object_store.py` (`ensure_directory_no_follow(self.root)`).
- Public routes/entrypoints: none — internal helper only.
- Frontend/downstream consumers: none.
- Failure paths/rollback/stale state: `provider_atomic` `precommit` raising
  `provider_lock_parent_unsafe`; `state_manager.py:2145-2153`
  `_ensure_copyback_state_parent`, which re-widens to `0o775` after creation.
- Evidence/audit/readiness: `services/production_closure/*` evidence and lane
  roots (`ops_validation`, `met_validation`, `scale_validation`,
  `readiness_shared_artifacts`, `two_node_e2e_final_aggregation`),
  `services/m24_live/receipt.py`.

Regression rows:

- safe_fs creates a directory under umask `0002` -> mode `0o755`, gate passes.
- safe_fs creates a directory under umask `0077` -> mode `0o700`, byte-identical
  to today; no widening.
- safe_fs creates a directory under umask `0022` -> mode `0o755`, unchanged.
- Provider lock parent that already exists as `0o775` -> still
  `provider_lock_parent_unsafe` in `precommit` (fail-closed preserved).
- `_ensure_copyback_state_parent` under umask `0022` -> copied checkpoint parent
  is still `0o775` and the file still `0o664`
  (`tests/test_run_tree_copyback.py:302-303` stays green). Note this test asserts
  the **leaf** only; its ancestors `states/gfs/model_a/` are created by
  `LocalObjectStore.write_bytes_atomic` (safe_fs) at
  `tests/test_run_tree_copyback.py:245-247` and are *not* covered by the chmod —
  which is why D7 enumerates the tree rather than relying on this test.
- `test_provider_atomic_publishes_shared_mode_under_private_umask` (umask
  `0o077`) -> destination file still `0o644`; unchanged sibling.
- Six `provider_atomic` importers -> no source change, no behavior change.
- safe_fs creates a directory under a parent carrying `default:user:X:rwx` ->
  the ACL mask is clamped to `r-x` and `user:X` becomes `#effective:r-x`. This is
  a **documented boundary**, not a tested one: it is kernel/ACL semantics, it is
  not reproducible on the macOS dev host, and the production enumeration in D7
  shows no live consumer. Pinned instead by the D7 table and by source comments.
- `_ensure_copyback_state_parent` creates under an ACL'd parent and chmods
  `0o775` -> mask restored to `rwx`, cross-uid grant intact.

## Boundary-Surface Checklist

- **Shared helper root**: `packages/common/safe_fs.py`, 119 production call
  sites. The mode change is global to all of them by construction — that is the
  point, and the reason repair intensity is `high`.
- **Write/overwrite surfaces**: evidence/lane roots under
  `services/production_closure/`, m24 receipts, and the Slurm/SHUD workspaces
  (`services/slurm_gateway/real_backend.py:655`, `:665`;
  `workers/shud_runtime/runtime.py:438`, `:533`, `:553`) are same-uid, same-host.
  The shared NFS object-store root is **not** — it has two writing uids; see D7
  for the per-subtree enumeration and why D1 is a no-op there.
- **Deliberately shared surface**: the node-22 <-> node-27 NFS object-store root.
  It uses **two different sharing mechanisms**, and neither is group-write:
  `forcing/`, `runs/`, `states/` use a named-user POSIX ACL
  (`default:user:nwm:rwx`) — the `rwx` group bits `ls` shows there are the ACL
  **mask**; `raw/` and `models/direct_grid_variants/` use a `777`/`1777`-sticky
  parent with per-uid children in empty groups. Only `states/` is *structurally*
  immune (its post-create `chmod 0o775` restores the mask). `forcing/` and
  `runs/` are exposed in mechanism but inert in fact (zero `nwm`-owned entries);
  `raw/` and `models/` are unaffected because their group bits grant nobody. Full
  enumeration and evidence in D7.
- **Stale-state/idempotency**: `ensure_directory_no_follow` is idempotent and
  never chmods an **existing** directory. Directories created before this change
  keep their current mode; the fix is forward-only, with no migration and no
  silent re-permissioning of anything the process did not create.
- **Unchanged downstream consumers**: `provider_atomic`'s gate and its other
  fail-closed branches; `scheduler_lease`'s separate lock channel.
- **CI boundary**: `scripts/select_ci_tests.py` routing (see D5).

## D9 — post-approval addendum: the second gate the implementation surfaced

**This section postdates the fixture review's `approve` (r2).** It records
measured facts discovered during Phase 1; it changes no decision above.

`provider_atomic` has **two** fail-closed mode gates, and the fixture modelled
only the first:

| # | Site | Checks | Refusal |
|---|---|---|---|
| 1 | `provider_atomic.py:209` | lock's **direct parent directory** carries no `0o022` bit | `provider_lock_parent_unsafe` |
| 2 | `provider_atomic.py:362` | an **already-existing destination file** is exactly `SHARED_PROVIDER_MODE` (`0o644`) | `provider_destination_access_invalid` |

Gate 2 accounted for **all 74** `tests/test_production_scheduler.py` failures
that survived D1, plus failures in three further suites: a test that seeds a
destination stub with a bare `write_text` lands `0o666 & ~umask` = `0o664` under
umask `0o002`.

**Production is immune, and this is why gate 2 needs no production change.**
Every production destination is written by `atomic_replace_provider_bytes`
itself, which passes `mode=SHARED_PROVIDER_MODE` to
`atomic_write_bytes_no_follow`; that helper opens the temp file and then calls
`os.fchmod(file_fd, mode)` (`safe_fs.py:141-143`). `fchmod` sets the mode
absolutely — the umask applies only at *creation* — so a production destination
lands exactly `0o644` under every umask. The umask-dependence exists solely
where a **test** pre-creates the file.

Consequences, all already reflected above and in §1/§2:

- The repair for gate 2 is same-party and test-side only. Gate 2 is left
  fail-closed, exactly as D3 leaves gate 1.
- The shared helper is therefore `tests/provider_mode_helpers.py`, exposing
  `write_provider_destination` alongside `make_directory_with_explicit_mode`;
  it imports `SHARED_PROVIDER_MODE` from production so the two cannot drift.
- The capability's coverage requirement names both inspected surfaces
  (`specs/filesystem-permission-determinism/spec.md`), because coverage is the
  half this change actually adds (D6). **No** new ADDED requirement was written
  for gate 2's production behavior: that behavior is pre-existing and untouched,
  so a requirement asserting it would document the wrong layer.
