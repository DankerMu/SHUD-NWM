# Design

## D1. The objection that actually fired

`query_released_identity_blocked_jobs` carries a docstring paragraph defending
the whole-tree replay:

> The cost is acceptable because this is an operator command run by hand on a
> wedge, never a scheduler pass.

That is a **wall-time** argument. What fires in production is a **fail-closed
record budget** — `_RecordBudget.consume` raises at
`file_orchestration_journal.py:729` once the count crosses `max_records`. No
amount of operator patience gets past it. I wrote that paragraph; it answered an
objection nobody raised and left the real one unexamined, which is why the query
shipped. D1 exists so the replacement docstring names the constraint that
actually binds.

## D2. Coverage is structural, not observational

The enumerate-then-scoped-read shape is only safe if a released row is
GUARANTEED to have an entry in the enumerated surface. If enumeration can miss
one, the change trades fail-closed (a loud `file_journal_record_limit_exceeded`)
for fail-open (a silent short listing) — strictly worse for an operator tool
whose entire job is "the operator can find the row".

The guarantee holds by construction, in three steps:

1. **Single write funnel.** `_write_pipeline_job_unlocked`
   (`file_orchestration_journal.py:7342`) calls
   `_write_pipeline_job_direct_unlocked` unconditionally at `:7382`. There is no
   pipeline-job write that skips the direct write.
2. **Masters always land flat.** `_write_pipeline_job_direct_unlocked` (`:7392`)
   routes to `pipeline-jobs/by-cycle/<source>/<cycle>/` **only** when the row is
   a current-contract `candidate` (`:7400`); every other row, cohort masters
   included, goes to the flat `pipeline-jobs/<job_id>.json` (`:7410`). A released
   identity-blocked row is a cohort master by the filter's own predicate.
3. **Nothing prunes it.** The only `unlink` sites that could touch a persisted row
   are `_remove_reconcile_atomic_residue_unlocked` (`:6646`, temp residue) and
   `_remove_reconcile_inventory_anchor_unlocked` (`:6925`, scoped to
   `_RECONCILE_INVENTORY_DIRECTORY`). Neither reaches `pipeline-jobs/`.

Corroborated but NOT relied upon: on node-22 production all four known
`identity_mismatch_released` rows have a flat entry (4/4).

## D3. The residue path, and why it is content-derived

Enumeration parses each flat filename with
`_accepted_submit_source_cycle_from_job_id` (`:9971`), which requires
`_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE = ^job_cycle_([^_]+)_(\d{10})_.+$` (`:400`).

That regex judges the **job_id string**. `accepted_submit_row_kind`
(`accepted_submit_identity.py:351`) judges the **row content** — a row is
`"master"` if any of eight master markers is set, `reconciliation_decision`
among them. **The two are not the same predicate**, so a name-only enumeration
would be an assumption, not a proof.

Measured on node-22: 74 of ~4 555 flat entries have unparsable job_ids, and every
one of them is `(row_kind=None, contract_is_current=False)` — zero would be
missed today. **That is a fact about today's data and this design does not lean
on it.** The residue path instead:

- unparsable name -> read that ONE flat file -> derive the scope from content via
  `_source_id_from_job` (`:9983`) / `_cycle_time_from_job` (`:9991`);
- only if content ALSO yields no cycle does the query fall open to the old
  `_iter_pipeline_job_records` full scan.

Two properties this buys. First, symmetry: `_write_pipeline_job_direct_unlocked`
derives the very same scope from the very same two functions at `:7397-7398`, so
reader and writer agree by construction. Second, the fall-open case preserves
#1734's D4 contract verbatim — an underivable key costs the old full scan, never
a false "not found". The residue set is empty in production today, so this path
is exercised only by a synthetic test row, which is precisely why task 1.3 pins
it rather than reasoning about it.

## D4. Rejected: raise the budget

`journal/` is append-only history: 232 files holding 71 213 records today, and
the count only grows. Raising `MAX_FILE_JOURNAL_RECORDS` moves the cliff instead
of removing it, and it would ship this mechanism a second time without a
production-scale oracle. The cycle-scoped path has no such property — each call
gets its own budget sized to one cycle's records, and cycle size is bounded by
the cohort, not by history.

## D5. Rejected: reuse the reconcile inventory

The inventory is the cheap enumeration surface the scheduler uses
(`_iter_reconcile_pipeline_job_records`). It cannot be used here, and the reason
is the same one that made the whole-tree replay look necessary in the first
place: `_remove_reconcile_inventory_anchor_unlocked` prunes the anchor the moment
a row stops needing restart reconcile, which is exactly what the release does. A
released row is by definition absent from the inventory. Using it would be the
fail-open failure D2 exists to prevent.

## D6. Snapshot semantics, stated rather than discovered

The flat listing is point-in-time on a live journal — two reads minutes apart
during this investigation returned 4 531 and 4 555 entries. The listing therefore
reflects the tree as of enumeration, and a row written mid-scan may or may not
appear. This is **not a regression**: the whole-tree replay had the identical
property, and the recovery API re-reads under `_locked_cycle_write` and CASes
against the values it reads there, so a stale listing can never cause a bad
write — only a repeat invocation. Recorded here so it is a stated property rather
than a review finding.

## D7. Test-oracle shape (the trap)

The obvious regression pin — construct the repository with `max_records=1` — is
**wrong** and would have been worse than no test. The cycle-scoped replay
consumes the same `_RecordBudget` per call (`:5770`), so `max_records=1` reddens
the fixed code as well; the only ways to "fix" that are to exempt the scoped path
from the budget (an oracle weakening) or to conclude the design fails.

The discriminating shape is **N cycles x M records each, with
M < max_records < N*M**. Concretely 3 cycles x 4 records with `max_records=6`:
whole-tree consumes 12 and raises (red at base); per-cycle consumes at most 4 and
returns the released row (green after the fix). The assertion must be that the
row is FOUND — not merely that no exception escaped, which a silently empty list
would also satisfy.

## D8. Evidence: equivalence over the whole retained history

Four known rows are not coverage. The production receipt (task 5.1) runs the OLD
whole-tree replay with an injected huge budget (`max_records=10**9`, diagnostic
script only — never production code, read-only) and diffs its released-row set
against the new enumerate-then-scoped result on the same journal. Identical sets
is a statement about all retained history, which "it found the four we knew
about" is not.

## D9. The per-cycle replay was measured and REJECTED

The first shape I committed to on paper — enumerate the 230 distinct
`(source, cycle)` scopes and run one `_iter_pipeline_job_records_for_cycle` per
scope — was prototyped against the real node-22 journal and **did not finish
inside 40 minutes**. It is not viable as an operator command.

Likely cause, stated as a hypothesis and not as a measured breakdown: the
per-cycle path filters the flat `pipeline-jobs/` directory by filename
(`_iter_flat_direct_pipeline_job_records_for_cycle`, `:5405`), so running it once
per cycle is O(cycles x flat-directory-size) — roughly 230 x 4 557. The whole-tree
replay pays that listing once.

This is recorded rather than quietly replaced. The paper design looked right,
satisfied every structural argument in D2/D3, and was still wrong on the axis
nobody had measured. It is the same failure the whole change is about: a
mechanism verified everywhere except at production scale.

## D10. The shape that measured well: flat scan as candidate filter

The flat `pipeline-jobs/` directory holds one journal RECORD per non-candidate
row, with the row itself under `payload`. Reading all of it and filtering on
`payload` measured, on the real node-22 journal:

```
read 4557 flat files in 1.44s
FOUND 4
  job_cycle_gfs_2026072000_forecast_cohort_bb8ef9c2fc1b_forecast_retry_87  seq 7118  attempt 88
  job_cycle_gfs_2026080712_forecast_cohort_3e066f456290_forecast           seq 298   attempt 1
  job_cycle_ifs_2026072000_forecast_cohort_1a2a03935b8b_forecast_retry_117 seq 9502  attempt 118
  job_cycle_ifs_2026080712_forecast_cohort_9c372471f1c1_forecast           seq 296   attempt 1
```

Exactly the four rows established independently as ground truth by scanning every
`journal/` record for `identity_mismatch_released`. No replay, no record budget:
`_iter_direct_pipeline_job_records` (`:5336`) is guarded by `max_files`
(100 000 vs 21 499 actual files), not by `_RecordBudget`.

**Why the flat record is current for this shape, not stale.**
`release_identity_blocked_reservation` writes through
`_write_pipeline_job_unlocked` (`:3425`), which calls
`_write_pipeline_job_direct_unlocked` unconditionally at `:7382`. The same call
that releases the row rewrites its flat file. Staleness in the direction that
matters — a released row whose flat file still says `reserved` — cannot arise
from that path.

**The design does not rely on flat being authoritative in general.** The flat scan
is the CANDIDATE filter; each candidate then gets an authoritative re-read through
the cycle-scoped path already proven by `--job-id` (which returns
`decision: "eligible"` on a real production row). Candidates number 4 out of
4 557, so confirmation costs four scoped reads. A stale flat file can therefore
produce at most a candidate that confirmation drops — never a bad write, and never
a listing entry that the recovery API would then refuse.

**Open inventory obligation, for the implementer not for me.** Row writes reach
the flat file through two families: `_write_pipeline_job_unlocked` (which pairs
both writes itself), and batch cycle writes that append a `("pipeline_job", row,
...)` payload and separately call `_write_pipeline_job_direct_unlocked` (`:2959`
paired with `:3006`; `:3084` with `:3111`; `:3311` with `:3336`; `:3931` writes a
candidate, which is routed to `by-cycle/` by design and cannot be a master). Task
3.1 requires this to be closed as an exhaustive list — every append of a
`pipeline_job` payload either has a paired direct write, or provably cannot carry
a current-contract master row. Eyeballing four pairs is not that proof.

## D11. The write inventory, closed (task 3.1) — D10's list was incomplete

Every durable pipeline-job row is written by exactly one of five paths, and all
five pair a `_write_pipeline_job_direct_unlocked` call:

| # | site | payload append | paired flat write | verdict |
|---|---|---|---|---|
| 1 | `_write_pipeline_job_unlocked` (`:7342`), 17 callers | `:7356` | `:7382`, unconditional | pairs itself |
| 2 | `reject_pipeline_job_submit_attempt` | `:2959` master | `:3006` `records[-2]` | paired (pipeline_event is appended last) |
| 3 | `mark_pipeline_job_permanently_failed` | `:3084` master | `:3111` `records[0]` | paired (master is first of two) |
| 4 | `permit_pipeline_job_retry` | `:3311` cohort master | `:3336` `records[-1]` | paired (master is appended last) |
| 5a | `project_forecast_cohort_tasks` | `:3931` candidates | `:4153` `zip(..., strict=True)` | paired; candidates route to `by-cycle/` |
| 5b | `project_forecast_cohort_tasks` | `:4082` cohort master | `:4155` `pipeline_records[-1]` | paired |

**5b is missing from D10's inventory paragraph and from the implementation
brief.** It is a genuine second master write inside `project_forecast_cohort_tasks`
and it IS paired, so coverage holds — but the eyeballed list was not closed, which
is exactly what task 3.1 existed to catch.

The list is exhaustive by two independent enumerations: every
`payloads.append(("pipeline_job", ...))` and every `("pipeline_job", ...)` tuple
literal in the file (5 sites, all above), and every
`_append_validated_record_unlocked` call site (10 sites: `forecast_cycle` x3,
`hydro_run` x3, `pipeline_event` x4 — **none** `pipeline_job`). No module outside
`file_orchestration_journal.py` references `pipeline-jobs` at all.

## D12. Retention, closed (task 3.2) — six unlink sites, not two

`unlink|rmtree|os.remove|rmdir` over the whole file yields six sites, four more
than D2 named: `:6031`, `:6074`, `:6113`, `:6292` (rollback/rollforward
marker, fence and receipt files at the journal root, named by
`_RECONCILE_INVENTORY_MIGRATION_MARKER` / `_ROLLBACK_PREP_RECEIPT` /
`_ROLLFORWARD_RECEIPT`), `:6651` (`_remove_reconcile_atomic_residue_unlocked`,
reached only for names matching `_RECONCILE_INVENTORY_TEMP_RE` /
`_RECONCILE_MIGRATION_TEMP_RE`) and `:6928`
(`_remove_reconcile_inventory_anchor_unlocked`, scoped to
`_RECONCILE_INVENTORY_DIRECTORY`). None can name a path under `pipeline-jobs/`.
D2's conclusion stands; its enumeration did not.

## D13. Tasks 1.3 and 1.4 are unconstructible — and that makes the fix simpler

D3 argued that `_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE` (a predicate on the job_id
STRING) and `accepted_submit_row_kind` (a predicate on row CONTENT) are not the
same predicate, so a name-only enumeration would be an assumption. True in the
abstract, and **false on the flat surface**, which is the surface that matters:

`_iter_direct_pipeline_job_records` yields only rows that survive
`_validated_direct_pipeline_job_record`, which runs
`normalize_accepted_submit_evidence` -> `forecast_cohort_identity_is_valid`. That
forces `run_id == cycle_{source}_{cycle}[_cohort]` and
`job_id == job_{run_id}_forecast[_suffix]`, and no storage source id contains
`_` (`gfs` / `IFS` / `ERA5`). So every current-contract master on the flat
surface necessarily matches the regex. A synthetic counter-example is not
discovered — it is REJECTED, with `file_journal_evidence_invariant_invalid`
on `cohort_digest`. Task 1.3's end-to-end scenario cannot be built, and building
it would make the flat scan raise rather than fall open.

The same validator makes task 1.4 unreachable: every yielded row carries a
`cycle_id` that round-trips through `_require_cycle_id`, so
`_source_id_from_job` / `_cycle_time_from_job` cannot fail.

The implementation therefore **drops the name-parse step entirely** (a deviation
from task 2.3's ordering). `_released_candidate_cycle_scope` derives scope from
row content with the same two functions `_write_pipeline_job_direct_unlocked` and
`_write_pipeline_job_unlocked` used to place the row, which is the byte-exact
inverse of the write and satisfies the "identifier does not parse" scenario
vacuously rather than by a second, weaker predicate. The `None` fall-open branch
is retained per the spec requirement and pinned by injecting the precondition at
that one seam, with the unreachability asserted in the test rather than assumed.

## D14. The confirm half re-entered D9's growth law — memo + group by cycle

Cross-review found the fix incomplete on its own axis. D10's shape enumerates
the flat directory ONCE as a candidate filter, and that is what the code does —
but the confirm step then called `_iter_pipeline_job_records_scoped` **once per
candidate**, and that path reaches `_flat_direct_pipeline_job_paths_for_cycle`,
which lists the WHOLE unpartitioned `pipeline-jobs/` directory (4,557 files on
node-22) and only then filters by file name.

The existing `_direct_jobs_cycle_cache` does not rescue this, and the reason is
not that it goes unreached — the measured counts say it IS reached. Its
VALIDATION fingerprint (`_cycle_rows_source_fingerprint`) builds
`direct_signatures` by calling `_flat_direct_pipeline_job_paths_for_cycle`
itself, so every cache *hit* still pays a whole-directory listing. Measured on
the fixture: M=2 over K=2 costs 5 listings, M=6 over K=2 costs 9 — i.e. 1
(candidate scan) + 2 per cache-miss candidate (fingerprint + reader) + 1 per
cache-hit candidate (fingerprint alone). Linear in the candidate count either
way, each listing over the whole flat directory. That is structurally the same `O(N x flat-directory)` shape D9
measured at ">40 minutes for 230 cycles" and rejected as non-viable.

N=4 today is a property of current DATA, not of the code. The admitted shape is
`reservation_lost` + `identity_mismatch_released` + no slurm id, which is
exactly what a mass SLURM outage or a mass release puts many rows into at
once — the incident this operator command exists to serve. So the wedge is
largest precisely when the command is needed.

The fix is two independent bounds, because the two costs are independent:

1. **Memoize the raw flat LISTING for the duration of one query**
   (`_flat_direct_pipeline_job_paths`, activated by
   `_flat_direct_job_listing_memo_scope`). Only the unfiltered listing is
   memoized; the per-cycle filename filter still runs per call, so
   `_flat_direct_pipeline_job_paths_for_cycle` remains the ONE definition of
   that filter (its docstring's commitment, and D9's parity requirement).
   Memoizing the FILTERED result would fork that definition's output by cache
   key.
2. **Group candidates by `cycle_scope` before confirming**, so the cycle replay
   runs once per distinct cycle rather than once per candidate. This alone does
   NOT bound the work — K distinct cycles is still unbounded — which is why it
   is paired with (1) rather than offered instead of it. It matters because the
   incident shape concentrates many candidates in ONE cycle, where the memo
   fixes the listing but the journal leg would still be replayed M times.

Together the query costs one flat listing plus one cycle replay per distinct
cycle: flat-directory listings are constant, independent of both M and K.

**Correctness fence (D6, unchanged).** The listing is deliberately
point-in-time; that is safe only because the mutating path re-reads under
`_locked_cycle_write` and compares there, so a stale listing costs at most a
refused invocation. That property is preserved where the memo meets
`_direct_jobs_cycle_cache`: only the path LIST is memoized, per-file
`_stat_signature` calls stay live, and the next query recomputes the fingerprint
from a fresh listing — so a mid-query add or removal produces a mismatch and a
rebuild rather than a stale row surviving into a later call. The memo is
therefore a `ContextVar` scoped to one call frame — never an instance cache, never entered from a write path, and entered
from exactly one read-only caller. Nothing else is weakened: the fail-closed
`_RecordBudget` still propagates (no `try`/`except` was added around the confirm
loops), and the D4 fall-open still runs ONE whole-tree replay for the entire
unscoped set rather than one per row.

The oracle is a direct COUNT of flat-directory listings rather than wall time,
so the pin is independent of fixture size and cannot rot into a timing flake.
