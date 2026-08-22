# Design

## Context

`_iter_pipeline_job_records()` (`services/orchestrator/file_orchestration_journal.py:4839`)
replays the whole journal tree. Six entrypoints use it for single-row lookups.
The `/proc` measurement in the issue proves the volume but **cannot attribute it
to an entrypoint**, so the attribution below is static call-graph evidence.

## D1. Which entrypoints are narrowed, and why

| entrypoint | per-cohort per-pass magnitude | derivable? | ruling |
|---|---|---|---|
| `query_pipeline_jobs_by_cycle` (`:1154`) | see below | yes — `cycle_id` is `{source_lower}_{cycle}` | **narrow** |
| `query_pipeline_jobs_by_run` (`:1166`) | ~11 combined | yes — `run_id` regexes | **narrow** |
| `query_candidate_state` (`:1053`) | ~4 | yes — key is `run_id:stage` | **narrow** |
| `_candidate_job_for_idempotency_unlocked` (`:1061`) | same order | yes — same key | **narrow** |
| `_pipeline_job_for_id_unlocked` (`:1099`) | most call sites | **no** — `job_id` carries no cycle | **leave** |
| `query_pipeline_job_by_slurm_id` (`:1176`) | **zero production callers** | no | **leave** |

Evidence for the two hot ones:

- `query_pipeline_jobs_by_cycle` / `by_run` are reached ~11 times per cohort per
  pass: once from `_active_orchestration_conflicts`
  (`chain_runtime_utils.py:103`/`:123`, the guard ahead of `_run_cycle_chain`),
  once at the top of `_run_cycle_chain`
  (`chain_forecast_execution.py:144`), and twice per stage inside the stage loop
  (`:155`, `:209`) across 5 stages.
- `query_candidate_state` is reached ~4 times per cohort per pass: the
  accepted-submit fast path is gated on `is_forecast_cohort_stage(stage)`
  (`chain_forecast_orchestrator_cycle.py:590-592`), so the four non-forecast
  stages (convert / forcing / state_save_qc / parse) fall to
  `_pipeline_job_conflicts_unlocked:6467-6471`, which calls it unconditionally.

Evidence for the two left alone:

- `_pipeline_job_for_id_unlocked` (`:1075-1102`) already tries
  `_direct_pipeline_job_record()` first and only replays when that misses. The
  flat `pipeline-jobs/<job_id>.json` file is written for every row that is not
  an accepted-submit candidate (`:6421` else branch), so the fast path covers
  the common case. Its argument also carries no cycle: narrowing it would
  require a new persisted index.
- `query_pipeline_job_by_slurm_id` has **no production call sites at all** —
  only its own definition (`:1176`), the abstract declarations
  (`chain.py:469`, `chain_repository.py:826`), and `chain_compat_static.py`
  export lists. Leaving it on the full scan costs production nothing.

**Consequence for the >=90% `rchar` criterion**: the two dominant entrypoints
are both narrowed, so the criterion is reachable. Had the dominant caller been
`query_pipeline_job_by_slurm_id`, this ruling would have been void — that is
why Task 1 gates implementation on the attribution rather than assuming it.

## D2. Forbidden implementation: routing to the direct partition alone

The by-cycle direct partition **is not a complete index of a cycle's pipeline
jobs**. `_write_pipeline_job_direct_unlocked` (`:6412-6428`) writes into
`pipeline-jobs/by-cycle/<source>/<cycle>/<job_id>.json` **only** when

```python
if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "candidate":
```

and sends every other row — every cohort **master** row, and every row from the
four non-forecast stages — to the flat `pipeline-jobs/<job_id>.json` instead
(`:6421` else branch).

Therefore: a narrowing implemented as "call
`_direct_pipeline_job_records_for_cycle_cached` instead of
`_iter_pipeline_job_records`" would **silently drop every master row and every
non-forecast-stage row**. That is the exact silent direction this change must
not take. It is named here as forbidden so a reviewer can check for it by name.

**Required wiring**: the narrowing is a *cycle-scoped input set* fed through the
**same** merge path as today — that cycle's `latest/<source>/<cycle>/**` views,
that cycle's `journal/<source>/<cycle>*.jsonl` segments, and the direct records,
applied via the same `_apply_journal_record` last-write-wins ordering, producing
rows in the same `_db_compatible_pipeline_job_order_key` order, and raising the
same blocked-row error shape. Only the set of files opened changes.

## D3. Key -> (source, cycle) derivation

Reuse the existing helpers; do **not** write a fresh parser.

- Run ids: `services/orchestrator/run_identity.py:24-32` —
  `FORECAST_RUN_ID_RE` (`fcst_{src}_{cycle}_{model_id}`),
  `CYCLE_COHORT_RUN_ID_RE` (`cycle_{src}_{cycle}[_suffix]`),
  `ANALYSIS_RUN_ID_RE` (`analysis_{src}_{start}_{end}_{model_id}`).
  Group 1 is the source segment, group 2 the cycle.
- Idempotency keys: `chain_runtime_utils.py:462-480` builds
  `f"{run_id}:{stage}"`, optionally `f"{run_id}:{stage}:{suffix}"`. Split off
  the run id, then apply the run-id regexes.
- `cycle_id` (for `query_pipeline_jobs_by_cycle`) is `{source_lower}_{cycle}`.

### The case trap

Run ids **always** spell the source lowercase (`chain_runtime_utils.py:85`
builds `f"cycle_{source_id.lower()}_{...}"`). On disk the directory casing is
whatever `normalize_source_id` (`packages/common/source_identity.py:12-18`,
`{"GFS": "gfs", "ERA5": "ERA5", "IFS": "IFS"}`) returns — so `gfs` is lowercase
but `IFS` and `ERA5` are **uppercase**. A derivation that uses the raw
lowercase run-id segment looks in `journal/ifs/`, finds nothing, and returns
`None` — a silent miss on half of production.

The derivation therefore **must** pass the parsed segment through
`normalize_source_id` / `_normalize_file_source_id`
(`file_orchestration_journal.py:9817-9828`) and then through
`_cycle_read_source_segments` (`:9853-9876`), which already handles the legacy
lower/upper directory aliases on the read side. A case-mismatch test is a
required negative pin (tasks.md).

## D4. Fall open, never fall closed

Every narrowed entrypoint keeps today's full scan as its fallback and takes it
whenever the derivation does not yield a `(source, cycle)` with certainty:
unrecognised run-id shape, an unparseable cycle token, an unknown source, or a
`cycle_id` that does not split.

Rationale — direction asymmetry, same as #1736: the fix moves *toward* reading
less. A narrowed lookup that misses a row is silent and harmful (a missed dedup
hit double-submits a cohort; a missed reconcile row mints a wrong retry). A
fallback to the full scan is merely slow, which is the status quo. Uncertainty
resolves to slow-but-correct.

`parse_run_cycle`'s own docstring already states this discipline for its own
caller ("There is no fallback to another token"); the fall-open here is its
counterpart on the consumer side.

## D5. Cross-cycle lookups: none exist, and one test pins their exclusion

No production call site looks up a job from a cycle other than the one being
processed: `_active_orchestration_conflicts` and
`query_pipeline_jobs_for_cycle_context` both scope to the current
`cycle_id`/`run_id`. The direction is in fact already the opposite —
`tests/test_production_scheduler.py:9764-9789`
(`test_foreign_cycle_marker_never_pins_even_with_its_own_stage_record`) asserts
a cohort-master row stamped with a *different* cycle is **excluded** from the
current candidate's decision state.

So the narrowing tightens an invariant the suite already asserts rather than
contradicting one. This is a search-based negative, not an exhaustive audit, so
the equivalence property test (tasks.md) is what actually pins it: it compares
the narrowed answer against the full-scan answer for every key, and would fail
loudly on any cross-cycle expectation anywhere in the suite.

## D6. `include_direct=False` parity

`_pipeline_job_for_id_unlocked` passes `include_direct=False` so the fallback
does not double-count the direct record it already tried. That entrypoint is not
being narrowed (D1), but the cycle-scoped iteration must still carry the flag
with identical meaning, because it shares the merge path. A test that would show
duplication if the flag were dropped is required (tasks.md).

## D7. Concurrency

The repository is a cross-thread shared singleton (`scheduler_core.py:46-48`)
fanned out over the cohort submit pool. Any new per-cycle memo follows the
discipline `_direct_pipeline_job_records_for_cycle_cached` (`:4331-4374`)
already establishes: population/lookup/eviction are mutually exclusive under
`self._cache_lock`, keyed on a stat-signature fingerprint, invalidated from the
write path, and **the cache mutex never acquires the journal write mutex inside
it** (single lock order). This is the standing requirement "Journal read caches
are safe under concurrent orchestration threads sharing one repository
instance" (`openspec/specs/pipeline-job-persistence/spec.md:550`); the change
must not weaken it.

Simplest option, and the default: add **no new cache**. Narrowing already cuts
the input set by ~two orders of magnitude; a memo is only justified if the
post-change node-22 measurement misses the >=90% target. Recorded so the
implementer does not add one speculatively.

## D8. Retention ruling

**Ruling: do not prune, and do not treat "no pruning" as an unbounded working
set once the lookups are narrowed.**

Rationale. The unbounded quantity that hurt was
`queries x whole-tree size`. After D1 the hot queries read one cycle's slice.
Measured on node-22 (2026-08-22, read-only):

```
latest/gfs/2026082112   17 files   1,585,910 B = 1.51 MiB
journal/gfs/2026082112.jsonl         292,800 B = 0.28 MiB
per-cycle slice                                  1.79 MiB
```

against a 566.1 MiB whole tree — a **316x** reduction per lookup. The slice
grows with `models-per-cycle` only (these cycles predate the 34 -> 48 registry
growth; at 48 models the view side scales to roughly 4.3 MiB), and **not at all**
with retained history. Retention therefore only bounds *disk*, which is not the
failing resource.

The same listing confirms the D3 case trap in production: the two source
directories under `latest/` are literally `gfs` and `IFS`.

Pruning is also the riskier move: `latest/` and `journal/` hold live rows,
including #1748's wedged reservation rows, and a retention sweep that deletes a
row a later reconcile needs converts a performance issue into a correctness one.

Therefore: the working-set bound this change delivers is **per-lookup**, not
per-tree; a disk-side archive口径 for `latest/`/`journal/` is filed as a
follow-up issue and is explicitly **not** implemented here (tasks.md §3).

## Invariant Matrix

| # | Invariant | Where pinned |
|---|---|---|
| I1 | Narrowed result is list-equal to full-scan-filtered result, incl. order and error shape | equivalence property test |
| I2 | A narrowed lookup opens no file outside its cycle's directories | read-path containment test |
| I3 | Underivable key -> full scan, never `None` | fall-open negative pin |
| I4 | Source-case mismatch (`ifs` vs `IFS`) still resolves | case negative pin |
| I5 | The direct partition is never used as the sole record source | D2, reviewer checks by name |
| I6 | `include_direct=False` still excludes direct records | parity test |
| I7 | Single lock order preserved; no new cache unless measurement demands one | D7 + existing concurrency test |
| I8 | `query_pipeline_job_by_slurm_id` semantics byte-identical (untouched) | existing tests |
