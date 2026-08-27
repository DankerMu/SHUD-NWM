Fixture level: expanded
Project profile: NHMS
Repair intensity: high
Issues: #1616, #1615, #1617
Upstream suggested level: absent
Minimal mergeable slice: all three contracts at the shared retention root-admission/deletion seam.

## 1. Root Admission and Overlap

- [x] 1.1 Extract shared pre-resolution root hygiene; freeze reasons as `primary_root_blank`, `primary_root_not_absolute`, and existing `extra_root_not_absolute`. Capture the constructor-time raw `object_store_root` before `ProductionSchedulerConfig.__post_init__` normalizes it, use only that raw value for pass retention, and keep normalized config behavior everywhere else.
- [x] 1.2 Reject unequal roots only when their potential canonical run/cycle deletion trees intersect; preserve parent/child layouts with disjoint lanes, primary-first then configuration-order precedence, silent equal-root dedup, and loser evidence `reason=root_overlap` with `conflicting_root=<winner>`.
- [x] 1.3 Keep the v2 `(root, key)` receipt identity, extra-root list, runs-only behavior, windows, frontier, and protections unchanged.

## 2. Contained Additional-Root Deletion

- [x] 2.1 Replace the additional-root descendant-symlink refusal path with `remove_tree_allow_symlinks(..., containment_root=<root>, missing_ok=False)` while retaining the symlinked-`runs/`-root refusal.
- [x] 2.2 Preserve per-entry failure isolation and freed-byte accounting for actual deletion errors.

## 3. Requirement-Driven Tests

- [x] 3.1 Direct input `{None, "", "   ", "relative/store"}` + CWD containing `raw/gfs/<old-cycle>` and `runs/<old-run>` -> `None` is a quiet no-op; explicit blank/relative values yield `primary_root_blank` / `primary_root_not_absolute`, `planned=[]`, `deleted=[]`, and both physical trees survive.
- [x] 3.2 Scheduler-pass constructor/env raw `OBJECT_STORE_ROOT` and cleanup-CLI env input `{ "", "   ", "relative/store" }` + old targets under CWD and the scheduler workspace-derived normalized location -> no scan/delete, bytes survive, and `skipped` carries the exact primary reason token; an absolute configured primary remains functional.
- [x] 3.3 True target intersection: primary A ↔ additional B and additional A ↔ additional B in both orders, including `B=A/runs/<canonical_run_id>/nested`, plus B under primary `raw|canonical|forcing/<source>/<valid_cycle>`, -> deterministic winner only, `root_overlap` + `conflicting_root`, no duplicate target/bytes; equal aliases remain silent dedup.
- [x] 3.3b Disjoint ancestry: parent workspace + child object-store, and parent/child additional roots outside canonical target lanes in both orders, each with its own aged run -> both roots admitted/planned/deleted once, no overlap skip, independent physical byte total.
- [x] 3.3c Classifier branch coverage: exact-at canonical run and primary cycle roots reject; exact `runs/`, noncanonical run ancestry, and additional-root cycle-shaped ancestry admit; mutation proof bites run/cycle fenceposts, parser loss, and primary-role loss at the public `run_retention` seam.
- [x] 3.4 Additional-root old run containing one top-level and one nested symlink to byte fixtures outside the root -> first pass deletes the whole run and preserves both targets byte-identical; second pass has no planned/failed entry for that run.
- [x] 3.5 Symlinked additional `runs/` root -> existing `runs_root_symlink_skipped` and untouched target; forcing prefixes/frontier/protected paths/v2 receipt/ordinary absolute primary -> existing outputs stay green.
- [x] 3.6 Inject `SafeFilesystemError(kind=unsafe|io)` from the new removal call -> entry lands in `failed`, later entry deletes, function returns, and `freed_bytes` counts only the latter.
- [x] 3.7 Produce batched red proof for new behavior tests against pre-change source and leave no `red-proof` stash entry.

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_retention.py tests/test_cli_cleanup_frontier.py tests/test_safe_fs.py`
- [x] 4.2 `uv run ruff check packages/common/safe_fs.py services/orchestrator/retention.py services/orchestrator/scheduler_config.py services/orchestrator/scheduler_runtime.py tests/test_cli_cleanup_frontier.py tests/test_retention.py` (tracked change surface clean; bare `ruff check .` is blocked by a pre-existing untracked `skills/subagent-workflow/scripts/review_gate.py:453` E501 outside this PR and intentionally untouched)
- [x] 4.3 `openspec validate harden-retention-root-boundaries --strict --no-interactive`
- [x] 4.4 Read-only completion audit maps every issue acceptance criterion and Invariant Matrix row to passing evidence on the final fixed head.
- [x] 4.5 Exact large-file guard exclusions for the two mandatory legacy files parse as valid JSON; the guard's 44-case test suite passes; positive/negative controls prove only those files are unblocked.

## 5. Risk-Pack Mapping and Non-Goals

- [x] 5.1 Selected: Public API/CLI, Config, File IO/path safety, Schema/receipt, Concurrency/ordering, Resource limits, Legacy compatibility, Error rollback/partial output, Documentation/migration, Hydro-met forcing-window preservation, Published artifact/display identity.
- [x] 5.1b Round-1 verified finding `cand-01` is closed across the complete potential-target surface inventory, not only the documented workspace/object-store example.
- [x] 5.1c Round-2 verified coverage finding `cand-02` is closed with independent public-seam boundary and mutation-bite evidence.
- [x] 5.2 Not selected: Auth/secrets, Release/dependencies, Geospatial, numerical runtime, PostGIS/TimescaleDB, Slurm lifecycle, external providers, manifest/QC; no touched surface maps to them.
- [x] 5.3 Non-goals remain: primary containment primitive, prefix/window/frontier changes, receipt v3, following a symlinked `runs/` root, and unrelated safe-filesystem callers.
- [x] 5.4 Known limit routed: exact guard exclusions permit future edits to two already-large files; split both below threshold and remove the exclusions via follow-up #1872.
