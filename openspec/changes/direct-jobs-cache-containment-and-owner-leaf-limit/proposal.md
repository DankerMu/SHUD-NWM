# Close the direct-jobs cache containment hole and rule the owner leaf-swap residual

## Why

PR #1939 (change `journal-cache-fingerprint-and-identity-followups`, merged as
`27855616`) put the cycle-rows cache fingerprint under the containment
discipline and, in doing so, measured two residuals it deliberately left open
and routed as follow-ups. Both live in
`services/orchestrator/file_orchestration_journal.py` (line cites against
`origin/master` `9785e52d`, 14,999 lines; symbol names are authoritative):

- **#1941 — the direct-jobs cycle cache still fingerprints with bare stats.**
  `_direct_pipeline_job_records_for_cycle_cached` (`:5972`) signs its listing
  with `_stat_signature` on `pipeline-jobs` and
  `pipeline-jobs/by-cycle/<source>/<cycle>` (`:5988-5996`) and stores the
  entry unconditionally (`:6014-6019`). Bare `_stat_signature` reports a
  missing path as `None` without asking whether a parent component is a
  symlink. Hard variant, reproduced in PR #1939's verification (cand-02):
  swap `pipeline-jobs/by-cycle/gfs` for a symlink to a decoy directory where
  neither side holds the `<cycle>` child — the signature is `(…, None)`
  before and after, so a warm instance keeps serving `[]` from
  `_direct_jobs_cycle_cache` while a cold instance raises
  `file_journal_unsafe_scanned_entry`. The owner fast path shares the hole:
  its directory probe forces a `_cycle_rows` recompute, but that recompute
  consults the same warm direct cache and gets the same `[]`.
- **#1942 — a leaf file swapped for a symlink inside a cycle write window is
  invisible to the owner fast path.** `_cycle_directories_probe_faulted`
  (`:9701`) probes directories only; the owner skips the source-file
  fingerprint by design (spec `pipeline-job-persistence` `:796-798`). PR #1939
  named this a stated limit and asked for a ruling: extend the probe to
  leaves (option A) or accept the limit permanently (option B).

The user ruled **B** for #1942. The PR therefore changes code for #1941 only.

## What changes

1. **#1941 — containment-aware signature on both legs of the direct-jobs
   cache, never storing a faulted signature** (design D1). Both stats route
   through `_containment_stat_signature` (`:9671`); a signature carrying
   `_FINGERPRINT_CONTAINMENT_FAULT` is neither stored nor treated as a hit.
   The recompute already reads the tampered path, so the warm instance now
   reaches the same `file_journal_unsafe_scanned_entry` a cold one does, in
   every lane (`model_a`, `model_id=None`, and the write-window owner) — all
   of which consume the cache through the `_cycle_rows` miss path (`:5835`);
   the retention inspection lane reaches the same point and keeps reporting a
   blocked row rather than raising. An untouched empty by-cycle directory
   still signs as absence and still hits the cache on a second read.
2. **#1941 sibling copy — conclusion recorded, no code change** (design D2).
   `_cycle_job_records_cache` (memo `_cycle_job_records_memoized` `:6823`,
   signature `_cycle_job_records_signature` `:6707`) was probed on every leg;
   its enumerators run under containment and raise inside the signature
   computation, so there is no warm/cold split to close.
3. **#1942 — ruled a permanent stated limit** (design D3; option C, reusing the owner probe's directory tuples, priced and rejected under the same ruling). No probe
   extension. The existing pin
   `test_cycle_write_window_owner_hit_does_not_see_a_leaf_swap_stated_limit`
   stays; its docstring stops promising a flip and cites the ruling.
4. **Spec**: the `pipeline-job-persistence` containment-probes requirement is
   MODIFIED so the direct-jobs cycle cache is named among the caches the
   discipline governs and the warm/cold-agreement scenario loses the "only
   where the recompute reads the tampered path" bound that existed solely to
   exclude the hard variant. The owner fast-path requirement already carries
   the stated-limit sentence; a second MODIFIED block widens it from "a leaf swapped for a symlink" to any leaf-level change beneath the probed directories (the probe is fault-only), which is what ruling B accepts.

## Deviations recorded up front

- #1941 acceptance item 4 asks for the archived change's `design.md:390-391`
  and `:80-93` (the by-cycle hard-variant "stated limit" rows) to be
  rewritten as closed. Archived changes are immutable in this repo; the
  closure is recorded by this change (design D1, matrix rows 2-4) and by the
  MODIFIED spec requirement, which is the normative record. The archived text
  stays as the history of what PR #1939 knew at merge time.
- The direct-cache caller at `:5907` (`_cycle_rows_by_model_unlocked`,
  `include_direct_jobs=True` branch) has no live caller today — every call
  site passes `include_direct_jobs=False` — so it is covered by the shared
  helper change but carries no test row of its own.

## Capabilities

- `pipeline-job-persistence` (modified): two existing requirements MODIFIED
  (journal existence probes / cache fingerprints under containment; the
  cycle-write-window ownership fast path's stated-limit sentence).

## Non-goals

- Leaf-level probing on the owner fast path (#1942 option A, and option C) — rejected on
  cost, see design D3.
- Any other `_stat_signature` caller outside the two fingerprint families
  (authority-root walks, latest-view watchers, sequence floor, strict authority
  walk) — different contracts, unchanged.
- Changing the direct cache's eviction, key shape or its three callers'
  semantics beyond the fail-closed raise that the containment signature now
  lets through.
- Any DB path. The oracle is local pytest on two filesystem semantics plus the
  node-27 run that #1941's acceptance names.

## Fixture

`design.md` is required: fixture level `expanded`, repair intensity `high`
(fail-closed cache fingerprint in the shared journal helper; symlink / path
safety; shared cache state under a lock).
