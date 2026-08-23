# Design

## Context

`_iter_pipeline_job_records()` (`services/orchestrator/file_orchestration_journal.py:4839`)
replays the whole journal tree. Six entrypoints use it for single-row lookups.
The `/proc` measurement in the issue proves the volume but **cannot attribute it
to an entrypoint**, so the attribution below is static call-graph evidence.

## D1. Which entrypoints are narrowed, and why

| entrypoint | per-cohort per-pass magnitude | derivable? | ruling |
|---|---|---|---|
| `query_pipeline_jobs_by_cycle` (def `:1150`, replay `:1154`) | see below | yes — `cycle_id` is `{source_lower}_{cycle}` | **narrow** |
| `query_pipeline_jobs_by_run` (def `:1162`, replay `:1166`) | ~11 combined | yes — `run_id` regexes | **narrow** |
| `query_candidate_state` (def `:1051`, replay `:1053`) | ~4 | yes — key is `run_id:stage` | **narrow** |
| `_candidate_job_for_idempotency_unlocked` (def `:1060`, replay `:1061`) | same order | yes — same key | **narrow** |
| `_pipeline_job_for_id_unlocked` (def `:1075`, replay `:1099`) | most call sites | **yes** — see D1a | **narrow** |
| `query_pipeline_job_by_slurm_id` (def `:1174`, replay `:1176`) | **zero production callers** | no | **leave** |

Evidence for the two hot ones:

- `query_pipeline_jobs_by_cycle` / `by_run` are reached on the order of 11
  times per cohort per pass: once from `_active_orchestration_conflicts`
  (`chain_runtime_utils.py:103`/`:123`, the guard ahead of `_run_cycle_chain`),
  once at the top of `_run_cycle_chain` (`chain_forecast_execution.py:144`),
  and twice per stage inside the stage loop (`:155`, `:209`) across 5 stages.
  **The implementer wires the dispatch, not these call sites**: all three go
  through `_query_pipeline_jobs_for_cycle_context`
  (`chain_forecast_orchestrator_cycle.py:852`), which delegates to
  `query_pipeline_jobs_for_cycle_context` (`chain_forecast_cycle.py:478`) and
  from there to `query_pipeline_jobs_by_run` or `query_pipeline_jobs_by_cycle`.
  This is the seam to narrow.
- `query_candidate_state` is reached on the order of 4 times per cohort per
  pass: the accepted-submit fast path is gated on
  `is_forecast_cohort_stage(stage)`
  (`chain_forecast_orchestrator_cycle.py:591`), so the four non-forecast stages
  (convert / forcing / state_save_qc / parse) fall to
  `_pipeline_job_conflicts_unlocked` (def `:6454`), whose final return calls it
  unconditionally at `:6471`.

**Both magnitudes above are estimates chained across two hops of indirection,
not directly traced call counts.** They are strong enough to settle the binary
narrow/leave ruling — the two hot entrypoints are the narrowing candidates
either way — but they are NOT strong enough to satisfy tasks.md Task 1, which
gates implementation on real attribution. Task 1 is not discharged by this
section.

### D1a. Correction: `job_id` IS cycle-derivable (this reverses an earlier ruling)

An earlier draft of this table ruled `_pipeline_job_for_id_unlocked` "leave", on
the stated ground that `job_id` carries no cycle. **That was wrong**, and the
proof is in this same file: `_CANDIDATE_JOB_ID_RE` (`:195`) is

```python
_CANDIDATE_JOB_ID_RE = re.compile(r"^job_fcst_([^_]+)_(\d{10})_.+$")
```

— group 1 is the source, group 2 the cycle — and `_direct_pipeline_job_record`
(`:4794-4810`) already uses it to route a by-cycle partition read. Cohort rows
use the sibling `job_cycle_{source}_{cycle}_{stage}...` shape. So both live job
id shapes carry `(source, cycle)`.

The correction was forced by measurement, not by re-reading. Task 1(b)'s
instrumented run put only **82.2%** of replays on the narrowed path (654/796),
with **130 of the 142 surviving full-tree replays (91.5%)** coming from this
entrypoint's direct-miss fallback. That is below the >=90% criterion, so
tasks.md Task 1(c)'s pre-declared fallback ruling fires: the "leave" decision is
void and is revisited **inside this change**. It is hereby reversed.

(An earlier revision of this section, and commit `11faf4ac`'s message, cited
83.4% against a denominator of 784. The denominator was 796; the share was
82.2%. The ruling is unaffected — both figures are below 90% — but the numbers
here are the correct ones.)

### D1a result

After the reversal and D2a, the same instrumented driver reports:

| entrypoint | entrypoint calls | cycle-scoped | full-tree |
|---|---|---|---|
| `query_pipeline_jobs_by_run` | 408 | 408 | 0 |
| `_pipeline_job_for_id_unlocked` | 947 | 123 | 7 |
| `query_candidate_state` | 116 | 110 | 6 |
| `_candidate_job_for_idempotency_unlocked` | 114 | 108 | 6 |
| `query_pipeline_jobs_by_cycle` | 28 | 28 | 0 |
| `query_pipeline_job_by_slurm_id` | 0 | 0 | 0 |
| **total** | | **777** | **19** |

**Reading the table**: the last two columns count *iterator* calls, and they sum
to 796. The first column counts *entrypoint* calls, which is the same number on
every row except `_pipeline_job_for_id_unlocked`: 947 calls reach that
entrypoint, but 817 of them are satisfied by a direct-record hit before the
iterator is ever entered, leaving 123 + 7 = 130 iterator calls. So the total row
is deliberately blank in the first column — 947 and 130 are different quantities
and must not be added to the others. The 97.6% below is computed from the
iterator columns only.

**777/796 = 97.6% narrowed**, up from 82.2%. The 19 residual full scans are all
deliberate D4 fall-opens: 7 unparseable job-id shapes and 12 per-candidate
idempotency keys of the shape `{source}:{cycle_id}:{basin}:{stage}`, which carry
no run id. Replay count remains a proxy and the test call mix is not the
production call mix — Task 1(c)'s node-22 measurement is still the decider.

`_pipeline_job_for_id_unlocked` therefore derives `(source, cycle)` from the job
id, narrows its fallback replay, and falls open per D4 on any id shape that does
not parse. Its `include_direct=False` semantics are unchanged (D6), and the
narrowed variant must be covered by the parity test, not only the shared
iterator.

Evidence for the one left alone:

- `query_pipeline_job_by_slurm_id` has **no production call sites at all** —
  only its own definition (`:1176`), the abstract declarations
  (`chain.py:469`, `chain_repository.py:826`), and `chain_compat_static.py`
  export lists. Leaving it on the full scan costs production nothing.

**Consequence for the >=90% `rchar` criterion**: after the D1a correction all
five entrypoints with production callers are narrowed, and the only remaining
full-scan surface is `query_pipeline_job_by_slurm_id`, which production never
calls. Task 1(b)'s 82.2% is what forced D1a — the gate did its job.

## D2. Forbidden implementation: routing to the direct partition alone

The by-cycle direct partition **is not a complete index of a cycle's pipeline
jobs**. `_write_pipeline_job_direct_unlocked` (`:6412-6431`) writes into
`pipeline-jobs/by-cycle/<source>/<cycle>/<job_id>.json` **only** when

```python
if accepted_submit_contract_is_current(row) and accepted_submit_row_kind(row) == "candidate":
```

and sends every other row — every cohort **master** row, and every row from the
four non-forecast stages — to the flat `pipeline-jobs/<job_id>.json` instead
(`:6429` else branch).

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

## D2a. The flat `pipeline-jobs/` surface is filtered by filename, with fall-open

The cycle-scoped iterator must also read direct records. Those live in two
places, and only one is partitioned:

- `pipeline-jobs/by-cycle/<source>/<cycle>/` — accepted-submit candidate rows,
  already cycle-partitioned, trivially scoped.
- `pipeline-jobs/<job_id>.json` — **flat**, holding every cohort master row and
  every non-forecast-stage row, for **all retained history**.

Reading the flat directory unrestricted would leave the growth law half
repaired. Measured on node-22 (2026-08-22):

```
pipeline-jobs/*.json          4,303 files   12,889,597 B = 12.29 MiB
pipeline-jobs/by-cycle/**     7,225 files
pipeline-jobs/ total                          40 MiB
```

12.29 MiB per lookup would **dominate** the 1.79 MiB cycle slice (D8) and grows
with `cycles x models x stages`, unbounded — and 4,303 files on its own already
exceeds the 4,096-entry cache cap. That is the very law this change exists to
repair.

**Ruling: filter the flat directory by filename, and fall open on any name that
does not parse.** Skip a file only when its name parses as a *different*
`(source, cycle)`; read it whenever the name does not parse. This keeps the D4
fall-open shape at the filename level: an unrecognised name is read, never
skipped.

This is effective, and measured rather than assumed. Both live shapes carry the
pair — `job_fcst_{source}_{cycle}_...` and `job_cycle_{source}_{cycle}_{stage}...`
— so on node-22:

```
total 4,303   parseable 4,301 (100.0%)   unparseable 2
distinct (source, cycle) keys                        230
worst single cycle                             233 files
unparseable bytes                             2,941 B
```

The two unparseable names (`cycle_gfs_..._retry_active`, `cycle_gfs_..._retry_2`
— missing the `job_` prefix) are read under fall-open, costing 2.9 KB. So the
flat surface goes from 4,303 files to at most 233 + 2, and stops growing with
retained history.

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
`_cycle_read_source_segments` (`:9872-9896`), which already handles the legacy
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
`tests/test_production_scheduler.py:9766`
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

## D9. Round 2: the D2a prefilter was applied to one of two near-identical readers

**Ruling: `_iter_direct_pipeline_job_records_for_cycle` (`:4857`) must delegate
its flat leg to `_iter_flat_direct_pipeline_job_records_for_cycle` (`:4801`)
rather than carry a third copy of the filter.**

There are two readers of the flat `pipeline-jobs/` directory, serving different
callers:

|reader|flat leg|callers|
|---|---|---|
|`_iter_flat_direct_pipeline_job_records_for_cycle` (`:4801`)|**filename-prefiltered (D2a)**|`_iter_pipeline_job_records_for_cycle` (`:4943`) — the narrowed path this change built|
|`_iter_direct_pipeline_job_records_for_cycle` (`:4857`)|**unfiltered** — `self._read_optional_json(path)` at `:4883` runs on every file in the directory before any content check|`_direct_pipeline_job_records_for_cycle_cached` (`:4348`) → `_cycle_rows` (`:4216`, `:4283`)|

D2a's fix landed on the first and not the second. The second's docstring-free
flat leg reads the whole directory's *content* — 13.18 MB across 4,375 files on
node-22 today — on every cache miss.

This is the same defect class as the two P1s in #1775: two near-identical code
paths deciding the same thing, one corrected and one not. It is closed the same
way — by **reference, not by a second fix**. A third copy of the filename filter
would recreate the class. Delegating also preserves the fail-open-on-unparseable
-name property by construction rather than by re-argument.

The by-cycle leg (`pipeline-jobs/by-cycle/<source>/<cycle>/`) is already
partitioned and needs nothing.

## D10. The D7 memo contingency has fired — and its invalidation scope is the whole decision

D7 recorded: *"a memo is only justified if the post-change node-22 measurement
misses the `>=90%` target. Recorded so the implementer does not add one
speculatively."* The 2026-08-23 receipt missed it (71.3% vs `>=90%`). **The
contingency has fired; the memo is now in scope.**

**Ruling: memoize `_iter_pipeline_job_records_for_cycle` (`:4943`) keyed on
`(source_id, cycle)`, with the invalidation signature scoped to that cycle's own
files — never to a shared directory's stat.**

The scoping rule is not a refinement, it is the whole point, and
`_direct_jobs_cycle_cache` (`:4348`) is the cautionary example sitting in this
same file. Its signature's first component is
`_stat_signature(self.root / "pipeline-jobs")` (`:4363`) — the **shared,
unpartitioned** flat directory. Any write to any cycle's flat row bumps that
directory's `(mtime_ns, size, inode)` and therefore invalidates **every**
`(source, cycle)` entry, not just the written one. That cache is *correct*
(the signature is conservative, so it never serves stale rows) and it *thrashes*
(a write-heavy pass re-scans the whole flat directory repeatedly). Correct-but-
thrashing is defect B in this change's attribution. A memo that keys on a shared
directory stat reproduces it exactly and buys nothing: the measured pass carried
`syscw` 4,961.

`_iter_pipeline_job_records_for_cycle`'s three legs make precise scoping
available:

|leg|partitioned?|signature component|
|---|---|---|
|`latest/<segment>/<cycle>/`|yes, by source+cycle|directory stat is already cycle-scoped|
|`journal/<segment>/<cycle>*.jsonl`|directory shared across the source's cycles, **files** are cycle-named|stat the matched file set, not the directory|
|flat `pipeline-jobs/`|directory shared globally, **already filename-prefiltered to this cycle** (D2a, and D9 above)|stat the prefiltered file set, not the directory|

Per-file `lstat` over an already-narrowed set is O(files-for-this-cycle) metadata
calls — the set the pass would open anyway — and contributes nothing to `rchar`.
Where a leg cannot be scoped, that is recorded as a stated limitation of the
memo, never hidden behind a directory stat.

**Discriminating test, required:** a write to a *different* cycle MUST NOT evict
this cycle's memo entry. That single assertion is what separates this memo from
the `_direct_jobs_cycle_cache` pattern; without it the two are indistinguishable
from their code.

The concurrency requirement is unchanged and binding: spec
`pipeline-job-persistence` "Journal read caches are safe under concurrent
orchestration threads sharing one repository instance"
(`openspec/specs/pipeline-job-persistence/spec.md:550`). Single lock order
preserved; no cache-mutex -> write-mutex nesting.

## D11. Attribution must become traced, and the counter ships in the repo

The 2026-08-23 receipt leaves three candidate mechanisms and traces none:

- **A** — full-tree replay via `_iter_pipeline_job_records()` (`:4894`) reached
  by the narrowed entrypoints' fall-open derivation. ~96 MB per call. Ranked
  highest by fit to both the 12.75 GB total and the 16.6 KB average read size
  (12.75 GB / 768,170 `syscr`), and **not sized**: D1a states plainly that the
  local driver's 19/796 fall-open ratio is not the production ratio.
- **B** — the flat-directory re-read of D9 + the shared-stat invalidation of
  D10. Bounded above at ~175 full rescans / ~2.3 GB by the pass's own `syscr`
  budget (768,170 / 4,375 files).
- **C** — the absent memo of D10. Order 1-4 GB, on a call count (777) that is
  again a local-driver number.

**Ruling: add a permanent, always-on per-entrypoint read counter and merge its
totals into pass evidence.**

Three properties are required and each has a reason:

1. **Always-on, shipped through the repo.** node-22 pulls from GitHub; there is
   no local-patch path. A monkeypatched probe would also have to run a real
   submitting pass to be representative — a plan-only pass performs no writes,
   so it triggers no invalidation and would understate B specifically.
2. **Tag -> bytes, keyed by entrypoint (A/B/C).** The output is a dozen
   `(tag, calls, bytes)` triples, negligible against the 5 MB evidence limit.
3. **Self-attributing, so no pre-fix baseline is needed.** After D9+D10 land,
   the same counters *verify* them: C's tag near zero proves the memo holds
   across the pass, and B's tag at roughly one 13 MB pass per cycle rather than
   many proves prefilter-plus-scoped-invalidation. The counter is therefore both
   the measuring instrument and the regression pin.

Thread-safety is bound by the same spec:550 requirement as D10.

## D12. Pre-declared outcome of this round: the primary criterion is expected to MISS again

The criterion allows `0.32 GB x 14 = 4.48 GB`. The receipt measured 12.75 GB, so
**8.27 GB must be removed**. Against the only published sizes: B is capped at
~2.3 GB by the `syscr` budget and C is estimated at 1-4 GB. Their sum is at most
~6.3 GB, which leaves the criterion missed even if both land perfectly and even
at the top of C's range.

**This is recorded before implementation, not discovered after it.** This change
has twice mistaken an estimate for a measurement (the pre-D1a table, and the
now-stale fallback ruling), and a third "we expected this to pass" would be the
same failure a third time. The deliverable of this round is therefore explicitly
three things, not one:

1. D9 — a parity defect fixed on its own merits, independent of attribution.
2. D10 — D7's own pre-registered contingency, now fired.
3. D11 — the traced attribution that **sizes A**, which is the only candidate
   large enough to close the remaining gap and the only one this change has
   never measured.

A receipt showing the primary criterion still missed, together with a traced
A/B/C split, is this round **succeeding**. The criterion closes in the round
that follows, against a measured target.
