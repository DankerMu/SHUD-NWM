# Design: fix-canonical-mirror-prune-permissions (#2100)

Fixture level: expanded; repair intensity: **high** (cross-uid write grant on
production shared NFS + delete/publish/rollback paths + a shared helper serving
three copyback lanes) — Invariant Matrix and boundary-surface checklist below.
Project profile: NHMS (`openspec/project-profile.md`)
Upstream suggested level: absent (hand-written issue).
Ordering gate: #2104 (`fix-raw-retention-lane-root-locality`) merges before
this change's mode change lands; a hard precondition recorded in the issue.

## Measured facts the design rests on (node-22 / node-27, 2026-09-13)

- The NFS server for `/ghdc/data/nwm` (node-22 mount, NFSv4.2) is node-27
  itself: `/home/ghdc/nwm/object-store` is local ext4 there.
- `nwm` on node-27: uid 1005, groups `nwm sudo docker nwmuser`; `sudo` needs
  a password, so root on node-27 is not available to this workflow.
- gid 1078 is `nfsdata` (empty) on node-27 and `huser` (23 members,
  `frd_muziyao`'s primary group) on node-22. gid 1107 is `nwmuser` on both
  hosts; members on node-27: `nwm, frd_muziyao`; on node-22: seven humans
  including `frd_muziyao`.
- `nwm`'s running `systemd --user` manager (pid 2047) already carries gid
  1107, and `systemd-run --user … id -Gn` prints `nwmuser`: no `usermod`, no
  session restart, so the #2104 "0770 before the group is effective" hazard
  is unreachable by construction.
- Owner-side probe (node-22): `mkdir; chgrp nwmuser; chmod 2775` on a probe
  root, then `mkdir child` → the child inherited gid 1107 and the setgid bit
  at `mkdir` (Linux semantics). A later GNU `chmod 0755 child` from the shell
  left `2755`: that is `chmod(1)`'s documented directory behaviour (numeric
  modes keep setgid unless a 5-digit mode is given), **not** `chmod(2)` —
  `os.chmod(dir, 0o755)` clears the bit. The design never relies on a bit
  surviving a chmod; every bit it needs is set explicitly.
- node-27 probe inside `systemd-run --user`: a `2775 frd_muziyao nwmuser`
  tree with a `0644` file was removed by `shutil.rmtree` as `nwm`. Probe
  directory removed afterwards.
- `getfacl`: the canonical tree has no extended ACL (issue comment
  2026-09-06); `setfacl` over the NFSv4 client is unsupported and root is
  unavailable, so the ACL route (D) is not executable here.
- Current tree: `canonical/{gfs,IFS}` 42 cycle dirs each + `grid/`, every
  level `755 frd_muziyao` gid 1078, files `644`; `du` 2.6 G + 2.5 G.

## How the mirror is laid out today (the code facts that shape D3)

- The copied tree's key is `canonical/<S>/<cycle>/prcp_rate_or_amount`
  (four segments, `_is_canonical_precip_tree_key`), so in
  `_copyback_collected_object_tree` `target_dir` **is**
  `prcp_rate_or_amount/` and `target_dir.parent` is `<cycle>/`.
- `<cycle>/` (and `canonical/`, `canonical/<S>/`, `canonical/<S>/grid/`) are
  created by `ensure_traversable_copyback_directory`, which `os.chmod`s each
  level it created to `0o755` right after the `mkdir` — clearing any setgid
  bit the level inherited.
- The temp tree `<cycle>/prcp_rate_or_amount.copyback.<uuid>/` is a sibling
  under `<cycle>/` (`_copyback_temp_tree_key`); its directories are created
  by the same helper (`0o755`), then `_chmod_tree_readable(temp_dir)` sets
  the temp root and everything beneath (directories `0o755`, files `0o644`)
  and the tree is promoted with one `os.replace`. The tree has no
  sub-directories: `prcp_rate_or_amount/` holds files only; each
  `grid/<grid_id>/` holds `grid.json` only.
- On a replace inside a batch (`_replace_directory_tree_for_qdown_batch`),
  the previous tree is only **renamed** to a sibling
  `.<name>.copyback-backup.<uuid>` — its mode is untouched — and a rollback
  from that entry renames it back. The tree that *is* chmod'ed is the clone
  the commit phase takes: `_commit_qdown_copyback_batch` calls
  `_clone_copyback_backup_tree_no_follow` for every entry that has a backup,
  producing `.<name>.copyback-rollback.<uuid>` and `_chmod_tree_readable`'ing
  it (`0o755`); if the commit then fails (its `rmtree_no_follow` of the
  backup raises), that clone becomes the rollback source and
  `_rollback_qdown_copyback_batch` restores it into place at `0o755`. The
  canonical lane's only commit call is in `_copyback_canonical_precip`.
- Consequence for a naive "widen the copied tree" fix: a new cycle on
  node-22 would land `<cycle>/` `0755` gid 1107 (inherited gid, bit cleared)
  and `prcp_rate_or_amount/` gid 1078 (created under a no-longer-setgid
  parent, so it takes the process egid). Neither level is writable by `nwm`.

## Decisions

- D1 **Identity: shared group, not ACL, not `chgrp` to a per-host group.**
  Write for `nwm` comes from gid 1107 (`nwmuser`), the only group present on
  both hosts that already contains both accounts. Rejected: C (`usermod -aG
  nfsdata nwm`) — widest blast radius and needs a user-manager restart that
  would bounce the display API; D (ACL) — not executable without root on
  node-27; `0777` — `raw/` precedent but grants every local account delete.
- D2 **Existing tree: owner-side sweep, roots first, `canonical/` untouched.**
  On node-22 as `frd_muziyao`, for `S` in `gfs IFS`:
  `chgrp -R 1107 canonical/$S` then
  `find canonical/$S -type d -exec chmod 2775 {} +` (pre-order: each parent
  before its children — the rollout rule the issue's first comment demands).
  Files keep `0644` (unlink needs `w+x` on the directory, not the file).
  `canonical/` stays `755` gid 1078: a storage source added later lands with
  gid 1078 and `nwm` is denied at the first `unlink` — zero bytes, a clean
  `failed[]` entry — instead of the partial-delete shape. Numeric gid in the
  runbook because 1078 maps to different names on the two hosts. Reversal
  uses `chmod 00755` (five digits, so `chmod(1)` clears the bit).
- D3 **Producer: one mirror directory mode, `0o2775`, asserted by the lane on
  every directory it owns.** A single constant
  `CANONICAL_MIRROR_DIRECTORY_MODE = 0o2775` in the publisher (the same
  value the sweep writes):
  1. At the top of phase 2 — after the `trees_already_mirrored` early return,
     once per call, before the first tree is copied —
     `_copyback_canonical_precip` ensures
     `<cycle>/` exists (`ensure_traversable_copyback_directory`, unchanged)
     and then asserts the mode on it through an fd bound with
     `_COPYBACK_DIR_FLAGS` (`os.open` + `os.fchmod`; never following a
     symlink), **whether or not this call created it** — that is what makes
     a re-mirror converge a `<cycle>/` left at `0755`. Because the bit is set
     before the temp tree is created, the temp directory inherits `<cycle>/`'s
     gid at `mkdir` on Linux (macOS inherits the gid unconditionally).
  2. `_chmod_tree_readable(root, *, containment_root, directory_mode=0o755)`
     gains the keyword and applies it verbatim to directories (files stay
     `0o644`). The canonical lane passes the constant on both chains that
     chmod a tree: the temp-tree chain
     `_copyback_object_tree_with_rollback` → `_copyback_collected_object_tree`
     → `_chmod_tree_readable(temp_dir, …)`, and the commit-clone chain
     `_commit_qdown_copyback_batch(…, directory_mode=)` →
     `_clone_copyback_backup_tree_no_follow(…, directory_mode=)` →
     `_chmod_tree_readable(clone_dir, …)`, so a clone restored by
     `_rollback_qdown_copyback_batch` is at the lane mode too.
     `_replace_directory_tree_for_qdown_batch` needs no parameter (it only
     renames). All other callers keep the default.
  2b. The temp tree root is asserted too. `_copyback_collected_object_tree`
     prepares `temp_dir` with `ensure_traversable_copyback_directory`, whose
     `chmod 0o755` strips the setgid bit `mkdir` inherited from `<cycle>/`;
     files are created inside `temp_dir` before the tree chmod runs, and a
     file takes the shared group only from a setgid parent at create time.
     So the helper re-asserts `directory_mode` on `temp_dir` through the
     same fd-bound `fchmod` right after preparing it and before the first
     write. It runs for every lane: for `runs/` and `forcing/` it rewrites
     the `0o755` the same call just wrote (ACL mask unchanged). This assert
     is load-bearing on Linux — without it mirrored `.nc` files carry the
     writer's egid, which the gid test pins.
  3. Result on node-22 under a swept `canonical/<S>/`: `<cycle>/` `2775`
     gid 1107, `prcp_rate_or_amount/` `2775` gid 1107, files `0644` gid
     1107. `grid/<grid_id>/` `2775`; `grid/` itself is a traversal level
     (`0755` when the producer creates it, `2775` after the sweep) and is
     never a deletion target.
  `ensure_traversable_copyback_directory` and the `runs/` / `forcing/` lanes
  are untouched: `forcing/` on node-27 carries a default ACL (`+` in
  `ls -l`) where a wider group mode would move the mask, and the spec's
  ACL-mask-neutrality scenario keeps holding for those lanes. The setgid bit
  on the mirror directories is set explicitly (`0o2775`) so nothing depends
  on a bit surviving a chmod; the kernel silently drops it when the caller is
  not a member of the directory's group. Measured for the actual producer
  process, not just a shell: the running `plan-production` pass (pid
  1955340) and `frd_muziyao`'s `systemd --user` manager (pid 7654) on
  node-22 both carry `Groups: 118 1078 1106 1107`. The post-merge
  persistence receipt (`stat … nwmuser` on both levels) is the live oracle
  that the bit stuck.
- D4 **Backfill: same mode below `canonical/`.** `DIR_MODE = 0o2775`;
  `_ensure_target_directory` unchanged otherwise (it chmods every level it
  creates, `<cycle>/` included, so the bit is on `<cycle>/` before
  `prcp_rate_or_amount/` is created). `canonical/` itself is the one level
  neither producer nor the sweep widens (D2): the backfill prepares it at
  `0o755` under the copyback batch mutex, before the first tree is
  mirrored — only when this run created it — so the
  per-level helper never creates the mirror root and a fresh copyback root
  (the 2026-09-06 bring-up shape) lands `canonical/` `755`, everything below
  `2775`. `FILE_MODE` unchanged. The docstring's `0o755` sentence updated.
  (Review round 1: the first cut let the helper widen `canonical/` too.)
- D5 **`PermissionError` becomes an incident.** The rmtree handler comment
  in `scripts/node27_raw_retention.py` points at the runbook section; the
  env example's "UNTIL #2100 LANDS" block is rewritten to the post-#2100
  steady state and clause 3 of the documented `jq` becomes
  `([.failed[]] | length == 0)`; the exit-code table is updated. #2104's
  clause 4 and readings stay.
- D6 **Rollout order, per level.** Let `P` = `canonical/<S>/`, `C` =
  `<cycle>/`, `L` = `prcp_rate_or_amount/`. `rmtree` unlinks files (needs
  `w+x` on `L`), then `rmdir L` (needs `w+x` on `C`), then `rmdir C`
  (needs `w+x` on `P`). The partial-delete shape is "`L` writable by `nwm`
  and `C` not".
  - Sweep first, old producer (the gap): new cycle → `C` `0755` gid 1107,
    `L` `0755` gid 1078 → denied at the first `unlink`, zero bytes.
  - New producer, no sweep: `P` `755` gid 1078 without setgid → `C` `2775`
    gid 1078, `L` `2775` gid 1078 → `nwm` ∉ 1078 → denied at the first
    `unlink`, zero bytes.
  - Both: `P` `2775`/1107, `C` `2775`/1107, `L` `2775`/1107 → whole cycle
    removed.
  - Re-mirror of a gap cycle (destination differs, so the tree is copied):
    the lane asserts `C` `2775` before creating the temp tree → `C` and `L`
    both `2775` gid 1107. A gap cycle whose destination is identical is
    *skipped* by the plan phase and not healed by the producer: the
    idempotent re-sweep after node-22 runs the merged producer is
    load-bearing for those cycles and is a runbook step.
  - Rollback-restored clone: `2775` gid 1107 via the threaded mode.
  - Existing cycles: the sweep sets `P`, `C`, `L` in pre-order, so at no
    instant is `L` writable while `C` is not.
  No order or path above yields "`L` writable, `C` not".
- D7 **Reversal.** `chgrp -R 1078 canonical/{gfs,IFS}` and
  `find … -type d -exec chmod 00755 {} +`; the producer change is a plain
  `git revert`. Recorded in the runbook.

## Invariant Matrix

- Governing invariant: every directory from `canonical/<S>/` down to the
  last directory of a mirrored cycle is `2775` with group gid 1107, so a
  `shutil.rmtree` of `canonical/<S>/<cycle>/` by `nwm` either removes the
  whole cycle or is denied at the first `unlink` with zero bytes removed —
  never in between.
- Source-of-truth identity/contract: mode `0o2775` + gid 1107 on
  `canonical/<S>/` (ops), `<cycle>/` and `prcp_rate_or_amount/`
  (producers); `CANONICAL_MIRROR_DIRECTORY_MODE` / backfill `DIR_MODE` are
  the same value as the sweep's `chmod 2775`.
- Producers: `services/tile_publisher/publisher.py::_copyback_canonical_precip`
  (asserts `<cycle>/`, passes the mode to the tree chmod and the backup
  clone); `scripts/canonical_precip_copyback_backfill.py::_ensure_target_directory`;
  the owner-side sweep (existing tree, gap cycles).
- Validators/preflight: none in code by design (no pre-check in the delete
  loop; the rollout rules make the partial shape unreachable); runbook
  one-liners `find … -type d ! -perm -2775` and `find … ! -group 1107`.
- Storage/cache/query: the NFS tree (ext4 on node-27); no DB row records
  modes.
- Public routes/entrypoints: `nhms-node27-raw-retention.service` (deletes);
  display precip routes read the same tree (404 `PRECIP_CYCLE_NOT_MIRRORED`
  when `prcp_rate_or_amount/` is absent; never a wrong image).
- Frontend/downstream consumers: `services/precip` mirror discovery keys on
  the `prcp_rate_or_amount/` directory; unchanged.
- Failure paths/rollback/stale state: unswept new source → clean denial;
  gap cycle → clean denial until re-sweep; batch rollback → clone restored
  at the lane mode; `chmod` refused (EPERM on a foreign-owned `<cycle>/`) →
  the lane's existing `OSError` handling records the mirror as failed.
- Evidence/audit/readiness: unit tests (modes and gid per level), node-22
  sweep receipt, node-27 live tick receipt (`failed == []`, canonical
  `deleted` non-empty), post-merge persistence receipt (`stat` on both
  levels of a freshly mirrored cycle), the documented `jq`.
- Regression rows:
  - new producer + swept `canonical/<S>/` (`2775` gid G) → `<cycle>/`
    `2775` gid G, `prcp_rate_or_amount/` `2775` gid G, `.nc` `0644` gid G.
  - old producer + swept parent (gap) → `<cycle>/` `0755` gid 1107,
    `prcp_rate_or_amount/` `0755` gid 1078 → `nwm` denied at first
    `unlink`, zero bytes (reasoned in D6; observed on the live tree as the
    pre-fix `failed[]` shape).
  - new producer + unswept parent (`755` gid 1078) → both levels `2775`
    gid 1078 → denied at first `unlink` (mode half covered by test 2.1 on
    a fresh `0755` parent; the gid/denial half reasoned in D6).
  - re-mirror over a `<cycle>/` pre-existing at `0755` → `<cycle>/` becomes
    `2775` and the promoted tree `2775`.
  - batch rollback of the canonical lane → restored `prcp_rate_or_amount/`
    is `2775`.
  - `runs/` and `forcing/` lanes, backup clone included → `0755` / `0644`
    unchanged; `canonical/`, `canonical/<S>/`, `canonical/<S>/grid/` created
    by the traversal widening → `0755`, pre-existing ones untouched.

## Boundary-surface checklist (high)

- Shared helper roots: `_chmod_tree_readable` (new keyword, default
  unchanged), `_copyback_collected_object_tree` /
  `_copyback_object_tree` / `_copyback_object_tree_with_rollback`
  (temp-tree chain) and `_commit_qdown_copyback_batch` /
  `_clone_copyback_backup_tree_no_follow` (commit-clone chain) thread the
  keyword; `_copyback_collected_object_tree` additionally re-asserts the
  mode on the temp tree root (D3 2b); `_replace_directory_tree_for_qdown_batch` and
  `ensure_traversable_copyback_directory` are **not** change surfaces.
- Public entrypoints: `TilePublisher.copyback_canonical_precip`;
  `canonical_precip_copyback_backfill.main`; the retention unit.
- Read surfaces: display precip mirror discovery; retention `collect_targets`.
- Write surfaces: `canonical/<S>/<cycle>/**` and `canonical/<S>/grid/<id>/**`
  (modes only; bytes unchanged); the ops sweep on `canonical/{gfs,IFS}/**`.
- Stale state: gap cycles (re-sweep), rollback clones (threaded mode).
- Unchanged downstream: `runs/` and `forcing/` copyback consumers, display
  routes, retention lane logic, #2104's skip vocabulary.

## Change surface

- `services/tile_publisher/publisher.py`: `CANONICAL_MIRROR_DIRECTORY_MODE`,
  `_chmod_tree_readable` (keyword), the helper chain above,
  `_copyback_canonical_precip` (assert `<cycle>/`, pass the mode);
  `_copyback_collected_object_tree` (re-assert the temp tree root, D3 2b).
- `scripts/canonical_precip_copyback_backfill.py`: `DIR_MODE`, docstring.
- `scripts/node27_raw_retention.py`: rmtree handler comment;
  `infra/env/node27-raw-retention.example`: operator block.
- Tests: `tests/test_tile_publisher.py`,
  `tests/test_canonical_precip_copyback_backfill.py`,
  `tests/test_node27_raw_retention.py`.
- Docs: `docs/runbooks/current-production-ops.md` §5.3 (new subsection),
  `docs/runbooks/receipts/2026-09-13-issue-2100-canonical-mirror-prune-node27.md`,
  supersession pointer in the display-v2 change's
  `specs/canonical-precip-copyback/spec.md`.

## Must preserve

- `runs/` and `forcing/` copyback trees and their backup clones: directories
  `0o755`, files `0o644`
  (`test_publish_qdown_copybacks_complete_run_products_to_shared_object_store`,
  the two `…_under_umask_027` sibling tests).
- Traversal-widening levels (`canonical/`, `canonical/<S>/`,
  `canonical/<S>/grid/`, the copyback root): `0o755` when created, never
  re-chmod'ed when pre-existing
  (`test_a_pre_existing_copyback_level_keeps_its_restrictive_mode`).
- Files in the mirror: `0o644`; symlink refusal and containment checks in
  `_chmod_tree_readable` and the copy helpers unchanged; the batch mutex
  unchanged.
- Retention deletion logic (`collect_targets`, `run_retention`, exit codes,
  `SCHEMA_VERSION`) unchanged; #2104's skip vocabulary unchanged.
- Display read path: reads and traverses only; no mode check on the mirror
  (the only `0o022` refusals are `provider_atomic`'s lock-parent gate and
  `node27_pgdata_host`, neither on this tree).
- The documented operator check still exits `0` on a healthy fresh
  `production_execute` summary and `1` on every shape in its table.

## Seams under test

- `TilePublisher.copyback_canonical_precip(source, cycle)` — public lane
  entry; modes and gid of `<cycle>/`, `prcp_rate_or_amount/`, `grid/<id>/`
  and the `.nc` file observed on disk; rollback path via the existing
  rollback tests' fixtures.
- `TilePublisher._copyback_qdown_products` / `_copyback_run_products` —
  sibling lanes, must-preserve oracle (existing tests).
- `canonical_precip_copyback_backfill.main([...])` and
  `_ensure_target_directory` — backfill modes and gid.
- `node27_raw_retention.main([...])` + the example file's `jq` program —
  operator check after clause 3 changes.
- Live: `nhms-node27-raw-retention.service` on node-27 and the owner-side
  sweep on node-22 (orchestrator-run, receipt-backed).

## Selected risk packs

- Auth / permissions / secrets: cross-uid write grant scoped to one group and
  two directory trees; blast radius disclosed; no secret involved.
- File IO / path safety / overwrite: modes only; the one new chmod is
  fd-bound with `_COPYBACK_DIR_FLAGS` (no symlink follow); symlink and
  containment guards unchanged.
- Error handling / rollback / partial outputs: D6 rules out the
  partial-delete shape per level; a sweep interrupted midway leaves parents
  swept before children (pre-order) → clean denial, retryable; rollback
  clones restored at the lane mode.
- Concurrency / shared state / ordering: sweep vs. a concurrent copyback
  promotion — `chmod`/`chgrp` on a directory being renamed into place is
  harmless and the re-sweep covers a cycle promoted mid-sweep; the retention
  tick (03:35Z daily) worst case during the sweep is the clean first-`unlink`
  denial.
  Copyback vs. tick (review round 1, verified PLAUSIBLE, deferred): the
  retention runner holds no copyback batch mutex, so a force re-mirror of a
  cycle already past the cutoff can interleave with the tick while the
  gap-cycle `.copyback-backup.<uuid>` (still `0755` gid 1078) sits beside
  the promoted `2775` tree — the promoted tree is removed, the backup
  denies, the next tick finishes the job. That is the unlocked-deleter
  shape `openspec/specs/object-store-copyback-mutual-exclusion` already
  records for this runner under #2252; the mandated post-deploy re-sweep
  removes the only shape this change makes newly reachable.
- Legacy compatibility / examples: sibling lanes pinned at `0o755`; the
  spec's ACL-mask-neutrality scenario still holds for them; env example and
  runbook updated with the change.
- Documentation / migration notes: runbook subsection + receipt + example
  rewrite + supersession pointer.

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — no public signature or CLI
  flag changes (`_chmod_tree_readable` is private; keyword has a default).
- Config / project setup: not selected — no env key; the runbook adds an ops
  precondition, not a config.
- File IO / path safety / overwrite: selected.
- Schema / columns / units / field names: not selected — summary schema
  unchanged.
- Auth / permissions / secrets: selected.
- Concurrency / shared state / ordering: selected.
- Resource limits / large input / discovery: not selected — the sweep walks
  ~90 directories per source; no new enumeration in code.
- Legacy compatibility / examples: selected.
- Error handling / rollback / partial outputs: selected.
- Release / packaging / dependency compatibility: not selected — no
  dependency change; node-22 deploy is `git pull --ff-only` without `uv sync`
  (the 3.12.7 maintenance-window rule in CLAUDE.md is respected).
- Documentation / migration notes: selected.

Domain packs (NHMS profile, all eight considered):

- Geospatial / CRS / basin geometry: not selected — bytes untouched.
- Hydro-met time series / forcing windows: not selected — the retention
  watermark and window are unchanged.
- SHUD numerical runtime / conservation / NaN: not selected.
- PostGIS / TimescaleDB domain behavior: not selected — no DB access.
- Slurm production lifecycle / mock-vs-real parity: not selected — the
  producer runs in the orchestrator's convert-terminal hook, not in Slurm.
- External hydro-met providers / snapshot reproducibility: not selected —
  mirror inputs and provider snapshots unchanged.
- Run manifest / QC provenance: not selected — no manifest or QC field.
- Published NHMS artifacts / display identity: **selected** — the mirror is
  the display precip read surface; the invariant guarantees a cycle is either
  fully present or fully absent (404 `PRECIP_CYCLE_NOT_MIRRORED`), never an
  empty shell that the receipt reports as intact.

## Required evidence

- `tests/test_tile_publisher.py::test_canonical_copyback_leaves_every_level_it_created_traversable`
  (updated): under umask `0o027`, `root`, `canonical/`, `canonical/gfs/`,
  `canonical/gfs/grid/` land `0o755`; `canonical/gfs/<cycle>/`,
  `canonical/gfs/<cycle>/prcp_rate_or_amount/`, `canonical/gfs/grid/gfs_0p25/`
  land `0o2775`; no level carries other-write; mirrored files `0o644`.
- `tests/test_tile_publisher.py::test_canonical_copyback_trees_take_the_source_roots_group`
  (new): pick a gid from `os.getgroups()` different from `canonical/gfs`'s
  current gid (skip when none), `os.chown(path, -1, gid)` + `os.chmod(0o2775)`
  on `canonical/gfs` before the copyback (skip if `chown` raises `PermissionError`)
  → `<cycle>/` and `<cycle>/prcp_rate_or_amount/` have that gid and mode
  `0o2775`; the `.nc` file has that gid and mode `0o644`. Deterministic on
  Linux and macOS: the directories inherit the gid at `mkdir` on both once
  `<cycle>/` carries setgid (macOS inherits the gid unconditionally); the
  file inherits it on Linux only because the temp tree root is re-asserted
  `0o2775` before the first write (D3 2b), and every asserted bit is set
  explicitly by the lane.
- `tests/test_tile_publisher.py::test_canonical_copyback_converges_a_pre_existing_cycle_directory`
  (new): `<cycle>/` pre-created at `0o755` with different bytes at the
  destination → after the mirror `<cycle>/` is `0o2775` and the promoted tree
  `0o2775`; a run whose destination is identical (`skipped`) leaves a `0o755`
  `<cycle>/` untouched (documents the re-sweep dependency).
- `tests/test_tile_publisher.py::test_copyback_canonical_precip_rollback_restores_the_commit_clone_at_the_lane_mode`
  (new): prime a mirrored cycle, change the source bytes, then make
  `_commit_qdown_copyback_batch` fail **after** its clone — monkeypatch the
  `rmtree_no_follow` it calls so it raises for `entry.backup_dir` — so the
  `.copyback-rollback.<uuid>` clone enters `remaining_for_rollback` and is
  restored by `_rollback_qdown_copyback_batch` from the lane's `except`:
  the summary reports the tree `rolled_back`, the restored
  `prcp_rate_or_amount/` holds the primed bytes and is `0o2775`, `<cycle>/`
  is `0o2775`. (The existing
  `test_copyback_canonical_precip_rollback_restores_a_stale_tree` exercises
  the rename-back branch, whose backup keeps the mode the lane already gave
  it, and cannot discriminate the threaded parameter.)
- `tests/test_tile_publisher.py::test_qdown_copyback_leaves_every_level_it_created_traversable_under_umask_027`
  and `…run_products…` (unchanged): `runs/` and `forcing/` levels still
  `0o755` — the must-preserve oracle for the lane scoping; plus
  `test_publish_qdown_copybacks_complete_run_products_to_shared_object_store`
  (`0o755` / `0o644`).
- `tests/test_canonical_precip_copyback_backfill.py::test_backfill_created_directories_stay_readable_under_a_restrictive_umask`
  (extended): every created directory is `0o2775`; files `0o644`.
- `tests/test_canonical_precip_copyback_backfill.py::test_backfill_created_cycle_directory_takes_the_source_roots_group`
  (new): `canonical/<S>/` at the destination pre-created with a second gid
  from `os.getgroups()` and `0o2775` (skip when unavailable) → the created
  `<cycle>/` and `prcp_rate_or_amount/` carry that gid and `0o2775`.
- `tests/test_node27_raw_retention.py`: the #2104 `jq` test's healthy case
  still exits `0`, and a summary with one canonical `PermissionError` entry
  now exits `1` (new case (iii)).
- `uv run ruff check .`; `uv run pytest -q tests/test_tile_publisher.py
  tests/test_canonical_precip_copyback_backfill.py
  tests/test_node27_raw_retention.py`; `openspec validate
  fix-canonical-mirror-prune-permissions --strict --no-interactive`.
- node-27 disposable worktree (`/home/nwm/tmp/wt-2104` re-pointed or a new
  one, `TMPDIR=/home/nwm/tmp`, active tree untouched): the same three pytest
  files green on Linux (setgid inheritance path, case-sensitive FS, real
  `IFS` names, `jq` present).
- node-22 sweep receipt (orchestrator): `stat` sample before; the two
  commands per source; after: `find canonical/gfs canonical/IFS -type d
  ! -perm -2775 | wc -l` = 0 and `find canonical/gfs canonical/IFS ! -group
  1107 | wc -l` = 0; `canonical/` itself still `755` gid 1078.
- node-27 live tick receipt (orchestrator): `du -sb canonical/{gfs,IFS}` and
  `df -h /home/ghdc/nwm/object-store` before; `systemctl --user start
  nhms-node27-raw-retention.service`; the new summary's path, `counts`,
  `freed_bytes`, every canonical `deleted[]` key, `failed == []`, raw and
  cache lanes with no new `failed`; `systemctl --user show -p
  ActiveState,SubState,Result,ExecMainStatus`; `du`/`df` after; the
  documented `jq` (new clause 3) exits `0` on that summary. **This receipt
  exercises the sweep and the retention path, not D3/D4**: after the sweep
  every existing cycle is `2775` regardless of the producer change.
- Post-merge persistence receipt (issue comment, then folded into the receipt
  file by the accountability follow-up) — the only live oracle for D3:
  node-22 `git pull --ff-only` HEAD, the first cycle mirrored afterwards —
  `stat -c '%a %U %G'` on `canonical/<S>/<cycle>` and
  `…/prcp_rate_or_amount` showing `2775 … nwmuser` — and the gap re-sweep's
  before/after counts.

## Non-goals

- Retention pruning logic (#2099 is correct); the `os.access` pre-check the
  issue's first comment offers for staged rollouts (D6 makes the partial
  shape unreachable per level).
- Moving pruning to node-22; mirror write-path correctness (#2076, #2070);
  q_down dead call site (#2068).
- `ensure_traversable_copyback_directory` and the `runs/`/`forcing/` lanes.
- Asserting the mode on `canonical/<S>/grid/` (never a deletion target).
- Moving node-27's active checkout off its current commit (`a8db554d`,
  local branch `hotfix/node27-rollback-pre-2073`): #2280 blocks
  `origin/master` there; the live receipt runs the unit as installed.
- Running the branch's producer on node-22 before merge (node-22 stays on
  master; D3 is proven by unit tests pre-merge and by the persistence receipt
  post-merge).
- Deleting anything by hand: only the retention unit deletes.

## Review focus

- Lane scoping: `0o2775` reaches only the canonical-precip trees, their
  backup clones and `<cycle>/`; `runs/`, `forcing/`, and traversal-widening
  levels unchanged.
- The `<cycle>/` assertion happens before the temp tree is created and is
  fd-bound; the gid test proves inheritance on both platforms.
- D6 per level and the Invariant Matrix rows: no order or path yields
  "`prcp_rate_or_amount/` writable, `<cycle>/` not".
- The env example and runbook describe exactly what the code and the sweep
  do; the `jq` no longer whitelists `PermissionError`; reversal uses
  `chmod 00755`.
- Live receipt: `failed == []`, canonical `deleted` non-empty, bytes freed,
  unit `Result=success`, raw/cache lanes unchanged; the tasks state that it
  does not cover D3.
