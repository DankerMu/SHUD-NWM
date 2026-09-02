# Close four file-journal follow-ups on one hardening pass

## Why

Four open issues sit on the same file
(`services/orchestrator/file_orchestration_journal.py`; line cites are against
`origin/master` `4f0ff53f`, 14,713 lines), were each filed by a
prior change as an explicit, pre-authorised follow-up, and each leave one
judgement the journal makes about a cycle's on-disk state weaker than the
discipline the rest of the file already enforces:

- **#1567** — `_cycle_segment_signatures` (`:10220`) fingerprints the cycle-rows
  cache with the bare `_stat_signature` (`:13420`), which follows symlinked
  *parent* components. A real empty directory and a `symlink -> empty decoy`
  therefore fingerprint identically, so a long-lived instance (the scheduler,
  `scheduler_core.py:110`) that cached a legal `[]` before a tamper keeps
  serving `[]` after it, while a cold instance on the same tree reports
  `file_journal_unreadable`. The owner fast path (`:5711-5732`) is a second,
  narrower bypass that skips the fingerprint entirely. PR #1566 declared this
  in the spec as a tracked residual.
- **#1658** — `_locked_cycle_write` (`:10069`) clears the whole cycle-rows cache
  on window exit (`:10089`). Every cohort's write window evicts every other
  cohort's freshly computed rows. PR #1667's design D2 ruled the *entry* clear a
  correctness precondition and pre-authorised narrowing only the exit clear.
- **#1761** — `_merge_cycle_source_discovery` (`:13504`, string check `:13514`) and the
  `source_segment_overrides` branch of `_cycle_read_source_segments` (`:13581`, string check `:13595`)
  still dedupe source-directory spellings by string. On a case-insensitive
  volume (macOS default) the mixed pair `("IFS", "ifs")` the merge produces is
  fed into the overrides branch, which cannot collapse it, so every record is
  read twice and local budget/containment assertions pass or fail for the wrong
  reason. PR #1759 fixed only the primary branch by `(st_dev, st_ino)` identity.
- **#1760** — PR #1759's D2a made the flat direct file *name* authoritative for
  cycle scoping, but `_write_pipeline_job_direct_unlocked` (`:9133`) never
  checks that a row's `job_id` agrees with the row's own `source_id`/`cycle_time`.
  The spec declares the residual instead of closing it. Measured 0/4309
  divergent rows in production; no current writer can mint one — the gate turns
  an emergent property into an enforced invariant before a new caller can.

## What changes

One PR, one OpenSpec change, serial implementation in the order
#1567 → #1658 → #1761 → #1760 (the first two share the cycle-rows cache; the
last two are independent of them and of each other):

1. **Containment-aware cache fingerprint (#1567).** Every stat that feeds the
   cycle-rows fingerprint resolves through the same no-follow containment probe
   as the hardened readers. A containment fault yields a fingerprint that can
   neither hit nor be stored, so the forced recompute reaches the existing
   probe fault in `_read_cycle_segments` and raises `file_journal_unreadable`
   exactly as a cold instance does. The owner fast path keeps skipping the
   source-file fingerprint but runs the same cheap directory probe, so the
   window owner is no longer a tamper hole either.
2. **Scoped exit clear (#1658).** The window-exit clear evicts only the
   window's own `(source_id, cycle_segment)` prefix (base key included). The
   window-entry clear stays global, untouched.
3. **Identity dedup everywhere (#1761).** Both remaining string-dedup sites
   use the `_names_same_directory` inode identity the primary branch already
   uses; case-sensitive volumes keep reading both real directories.
4. **Write-boundary scope gate (#1760).** When a row's `job_id` resolves to a
   `(source_id, cycle)`, a write whose row disagrees is rejected with a new
   `file_journal_job_id_scope_mismatch` before any byte — journal record or
   direct file — reaches disk. Unparseable ids keep fall-open semantics; the
   read-side validator is left alone. The historical import keeps its existing
   abort-at-row semantics for a divergent row (no new prefilter).

## Capabilities

- `pipeline-job-persistence` (modified): three existing requirements are
  MODIFIED (containment probes, cache fast path / window wipes, cycle-scoped
  lookups) and one requirement is ADDED (identity-based source-directory
  dedup).

## Non-goals

- The other `_stat_signature` callers outside the cycle-rows fingerprint family
  (authority-root walks `:1982-2142`, `_direct_jobs_cycle_cache` `:5928-5929`,
  the #1734 memo `:6675-6690`, latest-view watchers `:8333-8359`, `:8497`,
  `:8547`, sequence floor `:14262`, strict authority walk `:14456-14474`) —
  different contracts, not the cache the issues name.
- `_direct_jobs_cycle_cache` and the #1734 memo — their invalidation is already
  cycle-scoped.
- Read-side `job_id` decomposition in `_validate_pipeline_job_identity`
  (#1760's rejected alternative) and any backfill/health-check of historical
  rows.
- Disk-side read amplification (#1758) and archive口径 (#1757).
- Any DB path, any node-27 receipt: all four issues are `db-free` / `local-only`;
  the oracle is local + CI pytest on two filesystem semantics.

## Fixture

`design.md` is required: fixture level `expanded`, repair intensity `high`
(symlink/path safety, shared cache state, and a writer boundary in one shared
helper root).
