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
