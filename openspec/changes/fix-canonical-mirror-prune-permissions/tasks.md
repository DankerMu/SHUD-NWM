# Tasks: fix-canonical-mirror-prune-permissions (#2100)

Fixture level: expanded, repair intensity high (design.md carries the triage,
decisions D1–D7, the Invariant Matrix, seams and pack decisions).
Precondition: change `fix-raw-retention-lane-root-locality` (#2104) is merged
first.

## 1. Producers (code)

- [x] 1.1 `services/tile_publisher/publisher.py`: add
  `CANONICAL_MIRROR_DIRECTORY_MODE = 0o2775`; `_chmod_tree_readable(root, *,
  containment_root, directory_mode=0o755)` applies `directory_mode` to
  directories and `0o644` to files; thread `directory_mode` through the
  temp-tree chain (`_copyback_collected_object_tree`, `_copyback_object_tree`,
  `_copyback_object_tree_with_rollback`) and the commit-clone chain
  (`_commit_qdown_copyback_batch`, `_clone_copyback_backup_tree_no_follow`),
  default `0o755` everywhere; `_replace_directory_tree_for_qdown_batch` only
  renames and gets no parameter.
- [x] 1.2 `_copyback_canonical_precip`: at the top of phase 2 — after the
  `trees_already_mirrored` early return, once per call, before the first
  tree is copied — ensure
  `<cycle>/` exists via `ensure_traversable_copyback_directory(..., containment_root=copyback_root)`
  and assert `CANONICAL_MIRROR_DIRECTORY_MODE` on it through an fd opened
  with `_COPYBACK_DIR_FLAGS` (`os.fchmod`), whether or not this call created
  it; pass the constant as `directory_mode` for every tree it copies (cycle
  and grid) and to `_commit_qdown_copyback_batch`. Comment states the lane scoping, why
  `<cycle>/` is asserted before the temp tree exists (gid inheritance), and
  why `forcing/` keeps `0o755` (default ACL mask).
- [x] 1.3 `scripts/canonical_precip_copyback_backfill.py`: `DIR_MODE =
  0o2775`; module docstring sentence "chmod'ed 0o755" updated.
- [x] 1.4 `scripts/node27_raw_retention.py`: rewrite the rmtree-handler
  comment ("Known limit (measured on node-27, 2026-09-06) …") — a
  `PermissionError` on the canonical lane now means the producer mode or the
  ops sweep regressed; point at `docs/runbooks/current-production-ops.md`
  §5.3 (#2100).
- [x] 1.5 `infra/env/node27-raw-retention.example`: rewrite the "HOW TO READ
  THIS UNIT UNTIL #2100 LANDS" block to the post-#2100 steady state
  (`failed[]` empty, rc 0, unit not in `--failed`); clause 3 of the `jq`
  becomes `([.failed[]] | length == 0)`; the exit-code table's "expected
  steady state" and `PermissionError` lines updated; keep #2104's clause 4
  and readings intact.

## 2. Tests

- [x] 2.1 `tests/test_tile_publisher.py`: update
  `test_canonical_copyback_leaves_every_level_it_created_traversable` per
  design Required evidence (traversal levels `0o755`, `<cycle>/` and copied
  trees `0o2775`, no other-write, files `0o644`).
- [x] 2.2 `tests/test_tile_publisher.py`: new
  `test_canonical_copyback_trees_take_the_source_roots_group` (second gid
  from `os.getgroups()`; skip when unavailable or `chown` is refused;
  `<cycle>/` and `prcp_rate_or_amount/` `0o2775` + gid; `.nc` gid + `0o644`).
- [x] 2.3 `tests/test_tile_publisher.py`: new
  `test_canonical_copyback_converges_a_pre_existing_cycle_directory` (a
  `0o755` `<cycle>/` becomes `0o2775` on a copy; stays `0o755` on a skip).
- [x] 2.4 `tests/test_tile_publisher.py`: new
  `test_copyback_canonical_precip_rollback_restores_the_commit_clone_at_the_lane_mode`
  — fail `_commit_qdown_copyback_batch` after its clone (monkeypatch the
  `rmtree_no_follow` it calls to raise for `entry.backup_dir`) so the
  `.copyback-rollback.<uuid>` clone is what `_rollback_qdown_copyback_batch`
  restores; assert `rolled_back`, primed bytes, restored
  `prcp_rate_or_amount/` `0o2775`, `<cycle>/` `0o2775`.
- [x] 2.5 `tests/test_tile_publisher.py`: confirm the sibling-lane tests
  (`…qdown…under_umask_027`, `…run_products…under_umask_027`,
  `test_publish_qdown_copybacks_complete_run_products_to_shared_object_store`,
  `test_a_pre_existing_copyback_level_keeps_its_restrictive_mode`) pass
  unchanged and say so.
- [x] 2.6 `tests/test_canonical_precip_copyback_backfill.py`: extend
  `test_backfill_created_directories_stay_readable_under_a_restrictive_umask`
  (every created directory `0o2775`); new
  `test_backfill_created_cycle_directory_takes_the_source_roots_group`.
- [x] 2.7 `tests/test_node27_raw_retention.py`: the documented-`jq` test gains
  case (iii) — a `production_execute` summary with one canonical
  `PermissionError` failure exits `1`; the docstring of
  `test_an_undeletable_canonical_target_fails_without_stopping_the_other_lanes`
  no longer calls that the production shape.
- [x] 2.8 Red proof: 2.1 (mode assertions), 2.2 (gid/mode on `<cycle>/`) and
  2.6 fail on pre-change source (batched red run, output kept in the PR
  evidence).

## 3. Docs

- [x] 3.1 `docs/runbooks/current-production-ops.md` §5.3: new subsection
  "canonical 降水镜像的跨账号删除权限（#2100）" covering design D1–D7:
  model (gid 1107, `2775`, why `canonical/` is left alone), exact sweep
  commands + account + host, verification one-liners, new-source
  precondition ("sweep `canonical/<S>` before adding `<S>` to
  `NODE27_RAW_RETENTION_SOURCES`"), post-deploy re-sweep (load-bearing for
  gap cycles), reversal with `chmod 00755`, blast radius.
- [x] 3.2 Supersession pointer (原文保留、追加指针) in
  `openspec/changes/display-v2-national-timeline-precip-overlay/specs/canonical-precip-copyback/spec.md`
  after the sentence stating created directories are `0o755`.
- [x] 3.3 `docs/runbooks/receipts/2026-09-13-issue-2100-canonical-mirror-prune-node27.md`
  (orchestrator-authored from live output; §sweep, §live tick, §post-merge
  persistence placeholder replaced by the follow-up).

## 4. Evidence Floor

- [x] 4.1 `uv run ruff check .` clean.
- [x] 4.2 `uv run pytest -q tests/test_tile_publisher.py
  tests/test_canonical_precip_copyback_backfill.py
  tests/test_node27_raw_retention.py` green locally.
- [x] 4.3 `openspec validate fix-canonical-mirror-prune-permissions --strict
  --no-interactive` valid.
- [x] 4.4 node-27 disposable worktree: the same three pytest files green
  (`TMPDIR=/home/nwm/tmp`; active tree untouched).
- [x] 4.5 node-22 sweep receipt: before `stat` sample; commands; after
  `find canonical/gfs canonical/IFS -type d ! -perm -2775 | wc -l` = 0,
  `find canonical/gfs canonical/IFS ! -group 1107 | wc -l` = 0,
  `canonical/` still `755` gid 1078.
- [x] 4.6 node-27 live tick receipt via the unit: canonical `deleted` > 0,
  `freed_bytes` > 0, `failed == []`, raw/cache lanes no new `failed`,
  `Result=success` / `ExecMainStatus=0`, `du`/`df` before and after, the
  documented `jq` exits `0`. Covers D2 and the retention path only — not
  D3/D4 (every existing cycle is `2775` after the sweep regardless of the
  producer change).
- [x] 4.7 PR body: ordering gate (#2104 merged first), blast-radius
  disclosure, deviation record, and the post-merge persistence receipt plan
  (node-22 `git pull --ff-only`, first mirrored cycle `stat` at both levels,
  gap re-sweep) as the single declared post-merge evidence item and the only
  live oracle for D3.

## Known limits (recorded)

- Persistence on a freshly mirrored cycle (D3/D4) can only be observed after
  the merged producer runs on node-22; it is delivered as a post-merge
  receipt on the issue (4.7) and folded into 3.3 by the accountability
  follow-up. Pre-merge, D3 is proven by unit tests on Linux (node-27
  worktree) and macOS.
- Cycles mirrored between the sweep and node-22's deploy land `<cycle>/`
  `0755` gid 1107 / `prcp_rate_or_amount/` `0755` gid 1078 (clean denial,
  zero bytes) and are not healed by a producer that skips an identical
  destination; the documented re-sweep is load-bearing for them.
- node-27's active checkout stays at `a8db554d` (#2280); the live tick runs
  the installed unit and script, whose deletion path is unchanged by this
  change and by #2104.
- A storage source added later fails closed (clean `PermissionError`, zero
  bytes) until the documented per-source sweep runs — deliberate (D2).
