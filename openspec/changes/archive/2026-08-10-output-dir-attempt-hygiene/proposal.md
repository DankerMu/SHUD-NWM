# Proposal: Attempt-start output-dir hygiene (final_ic authorship)

## Why

Issue #1330 (PR #1328 round-1 PLAUSIBLE-DEFER, recorded as the
"Same-named cross-attempt residue" known limit in the archived
`state-save-source-freshness-gate` design D6). The #1325 writer's
`final_ic` two-exact-paths rule (`workers/shud_runtime/runtime.py:3122-3144`)
proves EXISTENCE, not authorship: `execute()` only
`_ensure_directory`s the output dir (`runtime.py:389-393`) and never
clears it, so attempts of the same deterministic `run_id` share one
tree. When checkpoint config drifts between attempts (non-empty →
empty hours; `chain_manifests.py:483-486`/`:640-643` set
`state_checkpoint_hours` and `Update_IC_STEP` together, so a
zero-hour solve normally writes no final IC of its own), a killed
attempt's stale `<project>.cfg.ic.update` is blessed with the new
attempt's provenance + checksum and published as T_{N+1} — wrong
state, self-consistent evidence chain. The repo already names and
governs this hazard class for recovery scratch
(`_clear_recovery_scratch_root`, `runtime.py:2199-2249`: "byte-
indistinguishable from a fresh result … silently poisoning the
warm-start lineage"); the main output dir has no equivalent
discipline.

## Ruling: quarantine via rename-aside (the issue's open call)

The issue left "清空 vs quarantine" to the fixture. Ruling:
**quarantine by renaming the whole pre-existing output tree aside**,
bounded to ONE retained residue tree:

1. At `execute()` attempt start, if `runs/<run_id>/output` exists
   NON-EMPTY, rename it to `runs/<run_id>/output_residue/previous`
   and recreate `output` fresh; a pre-existing EMPTY `output` is
   reused as-is — no quarantine, no eviction of retained residue
   (design D1 step 1; run-2 r1 P2-2 — this qualification is
   normative, not a parenthetical); an absent `output` is simply
   created fresh. Rename moves symlinks/subdirs/devices
   wholesale WITHOUT following or inspecting them — no per-file scan
   can half-clean, satisfying the intent of the issue's AC-3
   (no-follow, no partial state) with a strictly smaller trust
   surface than a scan-and-unlink.
2. Before quarantining, any existing `output_residue` content is
   removed with a NEW `remove_tree_allow_symlinks` primitive
   (fixture round-1 P1-1: the existing `rmtree_no_follow` REFUSES
   symlink entries, `safe_fs.py:424-425`/`:450-451`, so a residue
   tree containing a symlink would permanently lock the run_id at
   the hygiene hook; unlinking the link itself via dir-fd is
   inherently no-follow and safe for quarantine trees whose
   contents are untrusted by construction). Retention is exactly
   one tree; the retained tree is the one an operator debugging the
   CURRENT retry wants (issue AC: locatable on node-22, not
   silently discarded).
3. The quarantine name is the LITERAL `previous` (fixture round-1
   P2-1: naming with the CURRENT attempt's Slurm identity would
   mis-attribute the PREVIOUS attempt's tree; the residue's own
   witness-manifest provenance is the authoritative producer
   record). No clocks, no pid.
4. Rename needs a new safe_fs primitive (none exists — the only
   rename in `safe_fs.py` is the same-directory `os.replace` inside
   `atomic_write_bytes_no_follow`): a dir-fd `renameat`-based
   `rename_entry_no_follow(parent_dir, name, dest_parent_dir,
   dest_name)` where both parents are opened `O_DIRECTORY|O_NOFOLLOW`
   under containment. `renameat` operates on the link itself, never
   following a final-component symlink.
5. Failure semantics: the hook runs INSIDE `execute()`'s existing
   `try` (fixture round-1 P1-2 — a hygiene failure must produce the
   failure log, the task-outcome receipt (the array classifier's
   only channel; call `:421`, def `:1958`), and `mark_failed`,
   exactly like any other attempt failure), with the run-2
   ERROR-CODE SPLIT BY FAILURE SHAPE (corrected contract, closing
   run-1 defect 4; refined run-2 r1 from step-index to shape —
   design D1): TAMPER-SHAPED failures — the non-directory guard,
   plus any `SafeFilesystemError` `kind="unsafe"` refusal from any
   hook step (e.g. a symlinked `output_residue` sibling) — raise
   `WORKSPACE_PATH_UNSAFE` (permanent — retry cannot fix tampered
   geometry), while I/O-SHAPED failures ANYWHERE in the hook —
   step-1 probe/listing included, not just clear/rename/recreate —
   raise `STORAGE_WRITE_FAILED` (already in
   `TRANSIENT_ERROR_CODES`, `retry.py:23-35` — a transient NFS
   glitch at attempt start keeps automatic retry, zero orchestrator
   change). Never proceeds into a dirty tree.

Consequence: after the hook, everything inside `output` at
`write_manifest` time was written during THIS attempt (by the solve
or by this attempt's recovery installs) — the witness contract's
"existence ⟹ this attempt's authorship" becomes an established
invariant, and `_final_ic_entry` itself needs NO change (the issue's
rejected alternative — mtime/clock authorship predicates — stays
rejected for the same clock-adjacency reasons as in #1325).

## What Changes

1. `workers/shud_runtime/runtime.py` `execute()`: attempt-start
   hygiene hook (quarantine-then-recreate) as the FIRST step inside
   the existing `try`, with the `:392-393` directory loop split so
   `input_dir`/`log_dir` precede it and `output` creation belongs
   to the hook — scoped to `execute()` only, NOT `run_shud`
   (explorer + round-1 verification: no lane pre-seeds `output`
   before the solve; the one residue-seeding direct-`run_shud` test
   stays valid).
2. `packages/common/safe_fs.py`: two new primitives —
   `rename_entry_no_follow` (dir-fd `renameat`, both parents
   no-follow, containment enforced) and
   `remove_tree_allow_symlinks` (quarantine-tree removal that
   unlinks symlink entries instead of refusing; `rmtree_no_follow`
   untouched).
3. Tests per design anchors (execute-level retry geometry, symlink
   safety, bounded retention, upload/gate non-interference,
   no-regression pins).
4. Spec delta: ADDED requirement "Run output tree is attempt-fresh
   at solve start" under `cross-cycle-warm-start-chaining`.
5. Archived design D6 entry is NOT edited (archives are immutable
   history); closure is recorded on issue #1330 and in this change.

## Behavior deltas (disclosed)

1. A retried `execute()` no longer runs the solve into a tree
   containing prior-attempt bytes; prior residue is at
   `output_residue/previous` (exactly one retained; a pre-existing
   EMPTY `output` is reused without quarantine so an early-failing
   retry cannot evict real residue with an empty husk).
2. Older residue trees (two-plus retries ago) are deleted, not
   retained — recorded trade-off (bounded disk on NFS beats full
   forensic history; task-outcome receipts persist independently,
   while per-attempt logs are truncated by the next attempt —
   design D5). Residue of ever-retried runs persists indefinitely
   (one tree per run, explicit accepted cost, design D5).
3. A pre-existing `output` that is itself a symlink already dies
   TYPED on master (`ensure_directory_no_follow`'s `O_NOFOLLOW`
   final-component open maps to `WORKSPACE_PATH_UNSAFE`; fixture
   round-1 P2-3 correction) — this change makes the guard explicit
   in the hook, ordered BEFORE any quarantine mutation, and
   anchored for the first time (A4).
3b. NEW accounting + retry-classification change on output-setup
   failures (run-2 corrected disclosure — run-1's
   "benign-to-positive" claim was INVERTED and terminal): on
   master, output-setup failures die at the `:392-393` dir loop
   OUTSIDE the `try` — no receipt — and the array reader back-fills
   `DEFAULT_TASK_ERROR_CODE = NODE_FAILURE`
   (`chain_array_accounting.py:38`/`:880-890`), which IS transient:
   master auto-retries these. With the hook inside the `try`, the
   receipt carries an explicit code and the classifier
   (`is_transient_error` membership in `TRANSIENT_ERROR_CODES`,
   `retry.py:123-124`, reached via `is_retryable_failure`
   `:187-188` — run-2 r1 P3-3 cite fix) decides: tamper-shaped
   failures → `WORKSPACE_PATH_UNSAFE`, permanent — a CORRECTION
   (master's auto-retry of a tampered geometry was a
   pseudo-transient misclassification that could never succeed);
   I/O-shaped failures → `STORAGE_WRITE_FAILED`, transient —
   auto-retry PRESERVED. Net: no failure class silently loses
   retryability; the tampered class gains an honest terminal
   verdict plus full accounting.
4. The multi-root shadow geometry of #1329 is UNCHANGED (workspace
   manifest of a total-miss attempt still shadows the object-store
   root — that is #1329's contract question, out of scope here).

## Non-goals

1. No publish-side gate changes (`packages/common/state_cli.py`) —
   the gate trusts the witness by contract; this change fixes the
   witness's production end.
2. No `state_checkpoint_hours`/`Update_IC_STEP` alignment guard
   (#1317).
3. No mtime/clock authorship predicate (rejected in #1325, stays
   rejected).
4. No quarantine GC/retention policy beyond keep-exactly-one; no
   object-store side cleanup.
5. No change to checkpoint capture, recovery logic, or
   `_final_ic_entry`'s selection rule.

## Risk triage

- Level: expanded (S-sized diff, but it mutates the solve entry path
  of every production forecast/analysis run and deletes data —
  wrong-delete or wrong-refuse both have production blast radius).
- Must-preserve: every existing `execute()` success/failure lane
  (18 + 2 e2e test sites), direct-`run_shud` writer anchors from #1325
  (A7 family), witness contract semantics, upload scope
  (`_upload_directory` strictly under `output`), gate probe surface
  (literal `runs/<run_id>/output` only), `_clear_recovery_scratch_root`
  discipline untouched.
- Seams under test (upstream-declared, consumed): `execute()` attempt
  start; `safe_fs` primitive boundary; quarantine sibling placement
  (outside `output`, inside `runs/<run_id>`).
- Risk packs selected: fail-closed error-path pack (rename/clear
  failure semantics), filesystem-safety pack (no-follow, containment,
  NFS rename atomicity within one directory tree). Not selected:
  DB/scheduler packs (no orchestrator surface), concurrency pack
  beyond single-attempt discipline (Slurm runs one attempt of a
  run_id at a time — same exclusion recorded in #1325 D6).
- Evidence mapping: anchors A1-A7 in design.md → issue ACs 1-6;
  evidence floor = `uv run pytest -q tests/test_shud_runtime.py
  tests/test_warm_start_chaining.py tests/test_state_manager.py` +
  `uv run ruff check .` + `openspec validate --strict`.
