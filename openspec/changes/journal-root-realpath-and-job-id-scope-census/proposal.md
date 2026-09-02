# Verify the journal root at construction and census legacy job-id scope divergence read-only

## Why

PR #1939 (change `journal-cache-fingerprint-and-identity-followups`, merged as
`27855616`) shipped the #1760 job-id scope gate and, in its cross-review,
routed two pre-existing operational gaps as follow-ups. Both sit on the
db-free scheduler's file journal. Line cites are against `origin/master`
`6ca2e0b3`; symbol names are authoritative.

- **#1943 — `NHMS_SCHEDULER_JOURNAL_ROOT` has an undocumented realpath
  precondition, and the diagnostic never names it.** The repository stores
  the configured root raw (`file_orchestration_journal.py:1076`,
  `self.root = Path(journal_root)`); the db-free factory hands the config
  value straight through (`scheduler_core.py:49`). Every journal read walks
  from the filesystem anchor with `O_NOFOLLOW` per component
  (`packages/common/safe_fs.py:849` `_open_directory_no_follow`), so a
  symlink in *any* ancestor of the root — macOS `/var` → `/private/var`, an
  ops-level `/data -> /volume/data`, a "move the directory and symlink it
  back" firefight — turns every read into a blocked row. The db-free
  preflight `_db_free_path_check` (`scheduler_config/db_free.py:221`) only
  rejects a symlinked leaf or direct parent, so the configuration passes
  preflight and fails at first read with `Path component is not a directory`
  (darwin, ENOTDIR) or the ELOOP text (Linux) — neither mentions a symlink or
  the remedy. Reproduced locally (red proof in tasks.md §0): alias root →
  `preflight blocker: None`, `list_stage_statuses(model_id=None)` →
  `file_journal_unsafe_scanned_entry`, `model_id="model_a"` →
  `file_journal_unreadable`; the same tree under its realpath → legal rows.
  Sibling lanes already disagree: `operator_reserved_demotion.py:74` asserts
  the root with `verify_directory_no_follow` and raises
  `FILE_JOURNAL_INVALID_ROOT`; `file_orchestration_migration.py:738` hands a
  `.resolve()`d root to its subprocess; the scheduler main lane does neither.
  node-22's live root (`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`,
  six real directory components, `readlink -f` identical) does not trip it:
  this is a deployment trap, not an outage.
- **#1944 — a legacy row whose `job_id` contradicts its own `(source, cycle)`
  has no observation surface and no recovery path once the gate is live.**
  `_require_job_id_cycle_scope` (`:9558`) rejects such a row on every write
  lane with `file_journal_job_id_scope_mismatch`. The gate is correct and stays.
  But (a) the reconcile scan's non-strict repair branch
  (`_iter_reconcile_inventory_records` `:7020` →
  `_restore_derived_master_direct_unlocked`) has no `try`, so one
  "anchor present + flat direct missing" divergent row aborts the whole scan
  before later anchors are yielded and before the offending anchor is pruned;
  `scheduler_runtime.py:1555-1595` swallows it into `evidence["status"]="error"`
  and restores zero cohorts, every pass, with no receipt naming the `job_id`.
  And (b) the row cannot be moved: `update_pipeline_job_status`,
  `upsert_pipeline_job` and `permit_pipeline_job_retry` all carry the persisted
  `job_id` into the outgoing record, so the gate recurs — for non-terminal rows
  toward any target, and (per the verifier's 36-cell matrix recorded on #1944)
  for terminal rows toward `partially_failed` / `permanently_failed` too,
  which is exactly the target `FileJournalRetryService.mark_permanently_failed`
  uses on auto-retry's decline path. The only evidence that no such row
  exists is the migration-input measurement `0/4309`; the live journal has
  never been censused, and PR #1939's own fixture proved the public
  `reserve_pipeline_job` lane could mint the shape before the gate existed.

## What changes

1. **#1943 — one root-verification seam, used by the scheduler factory and
   the demotion CLI** (design D1). A new module
   `services/orchestrator/journal_root_authority.py` exposes
   `verify_journal_root_authority(journal_root) -> Path`, which walks the
   configured root through `verify_directory_no_follow` and converts any
   `OSError` / `SafeFilesystemError` into
   `OrchestratorError("FILE_JOURNAL_INVALID_ROOT", …)` whose message names the
   cause class and the remedy (every component of the configured journal root
   must be a real directory; set it to the `readlink -f` result) and whose
   details name the setting the caller reads (`NHMS_SCHEDULER_JOURNAL_ROOT`
   for the scheduler, `--journal-root` for the demotion CLI). `_db_free_orchestration_repository_from_config`
   (`scheduler_core.py:44`) calls it before constructing the repository, so a
   symlinked ancestor now fails at scheduler start with a typed `code: message`
   on stderr (the `plan-production` catch, exit 1) instead of at first read
   with a misleading token. `operator_reserved_demotion.py:74` switches to the
   same helper; its exact-string test is updated to the new message
   (recorded deviation, a strengthening). `_db_free_path_check` is untouched
   (#1627 owns that lane's realpath/ENOENT ruling); the migration lane's
   `.resolve()` stays and is recorded as consistent with the constraint.
2. **#1943 — the constraint is documented where the value is set** (design
   D1): a comment line at `infra/env/compute.scheduler-dbfree.env.example:42`,
   `infra/env/compute.example:58`, `infra/README.two-node-docker.md:115`; a
   sentence in `docs/runbooks/current-production-ops.md` §3.1 next to the
   db-free scheduler description and a new §8 subsection with the symptom
   (blocked rows, a diagnostic that does not say "symlink", preflight PASS)
   and the remedy; one cross-reference line each in
   `docs/runbooks/qhh-22-business-bringup.md` and
   `docs/runbooks/failed-basin-retry.md` at the cited sites.
3. **#1944 — a read-only census subcommand** (design D2). New module
   `services/orchestrator/journal_scope_census.py` with
   `census_job_id_scope(journal_root, …)` and the CLI command
   `census-job-id-scope` (Click and argparse, registered from `cli.py` with
   import/register/dispatch lines only — `cli.py` is 965 lines under the
   1000-line guard). It classifies every pipeline-job row on every surface the
   reconcile scan and the canonical lookup read — flat `pipeline-jobs/`,
   `pipeline-jobs/by-cycle/**`, the journal replay (latest views + segments),
   `reconcile-inventory/` anchors, and the legacy `active-reconcile/`
   directory when present — by calling the gate's own predicate
   `_require_job_id_cycle_scope` per row, never a second comparison. It
   cross-matches each divergent `job_id` against the anchor set and the flat
   direct listing and flags the "anchor present + flat direct missing"
   combination as a reconcile-abort trigger. Anchors are counted on their own
   surface and never listed among a row's surfaces; an anchor with no row is
   still a divergent id and a trigger. It emits one JSON receipt and
   exits 0 (none), 2 (divergent rows found) or 1 (typed error). It enumerates
   anchors itself through `list_directory_no_follow_limited` and the pure
   anchor validator — never through `_iter_reconcile_inventory_records`, which
   takes the write lock and cycle flocks, migrates the inventory, restores
   directs and prunes anchors — and reports `.tmp` residue instead of removing
   it. Zero bytes are written to the journal tree; the tests prove it on files
   and directories and pin that no repository write/lock path is entered.
4. **#1944 — the runbook records identification, consequences and recovery**
   (design D3): a new §8 subsection in `current-production-ops.md` giving the
   census command in the node-22 interpreter form, consequences (a) and (b)
   including the terminal-row correction from the #1944 comment ("neither
   terminal nor non-terminal divergent rows can be retired through the API"),
   and the manual recovery — stop the timer, back up, delete the divergent
   row's direct files **and its anchor together**, and the fate of a
   segment-resident row (inert history; retirement is out of scope).
5. **#1944 — node-22 live census receipt** (design D4): run before any
   post-#1939 checkout lands on node-22 (its active checkout `3acea778`
   predates the gate, which is exactly the census window), from a detached
   worktree of this branch with the active interpreter
   `/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli …`,
   never `uv run` / `uv sync`. The receipt (root, realpath equality, worktree
   SHA, interpreter, UTC time, per-surface counts, divergent count, abort
   triggers, residue, exit code, timer/service state) lands in
   `.workplans/pr-journal-root-and-scope-census/` and the PR. **Census 0 →**
   the runbook section is the recovery path and no repair command is built.
   **Census non-zero →** a guarded repair command is added under the same
   fixture (dry-run default, `--apply`, the one action "delete the divergent
   row's direct files and its anchor"), with its own tests and runbook step.
6. **Spec**: `runtime-evidence-and-operations` gains an ADDED requirement for
   root verification at construction with the named remedy;
   `pipeline-job-persistence` gains an ADDED requirement for the read-only
   census (predicate reuse, surface coverage, abort-trigger cross-match, zero
   writes).

## Deviations recorded up front

- `tests/test_orchestrator_demote_cli_security.py:838` asserts the exact
  stderr line `FILE_JOURNAL_INVALID_ROOT: journal root failed safe filesystem
  verification`. The line changes to the new message; the traceback/module
  leak assertions stay. This is a strengthening of the operator-facing
  diagnostic (#1943's point), recorded here and in the PR 偏离记录.
- The root helper is strict about a missing root (as the demotion lane already
  is): under `db_free_required` the runtime preflight
  (`scheduler_core.py:176`) already blocks a missing root before the factory
  runs, so the from_env path sees no new behaviour for absence.
- #1943 lists `docs/runbooks/two-node-deployment-overview.md` as **not** a
  site; it is not touched.

## Capabilities

- `runtime-evidence-and-operations` (modified): one ADDED requirement.
- `pipeline-job-persistence` (modified): one ADDED requirement.

## Non-goals

- Changing `safe_fs` containment semantics (per-component no-follow is
  #1167/#1566's design) or `_db_free_path_check` (#1627's ruling).
- Any change to `_require_job_id_cycle_scope`, the reconcile scan's exception
  propagation (`:7020`, `:1895`, `:1990`) or `import_historical_scheduler_state`
  — fail-closed stays; skip-and-keep-anchor was rejected in PR #1939's review.
- The #1820 per-row isolation/skip-list shape — a different lane, not copied.
- Rewriting journal segments to retire a segment-resident divergent row.
- Pulling node-22 to master (ops / #1831), or any `uv` invocation there.

## Fixture

`design.md` is required: fixture level `expanded`, repair intensity `high`
(a new CLI over the live journal root; symlink / path safety at the
construction seam; legacy on-disk compatibility of the census; a read-only
invariant over a tree the live scheduler writes).
