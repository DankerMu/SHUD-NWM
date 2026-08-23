# Design

## Context

`forecast_cohort_runtime_identity_matches`
(`services/orchestrator/file_orchestration_journal.py:1784`) validates an
accepted-submit cohort's members against independently written `hydro_run`
rows. The fatal leg at issue:

```python
observed_task_id = hydro_run.get("array_task_id")
if observed_task_id is not None and int(observed_task_id) != int(member.get("array_task_id")):
    return False
```

All evidence below is from the #1749 triage comment (read-only source review
plus read-only node-22 journal inspection). It is cited, not re-derived.

## D1. Why `array_task_id` is not an identity

`hydro_run` is a single row per `(source, cycle, model)`. `_write_hydro_run`
(`:6842`) refuses to rewrite an existing row unless its `status` is `failed` or
`cancelled` (`:6849-6861`), so a row's `array_task_id` is **frozen at the
submission that created it**.

The value itself is the member's index within one array submission. Renumbering
is not an edge case; it is what happens whenever the member set changes.
Measured, same `run_id` across three submissions of `gfs_2026080712`:
`dg_acbb…` was task **10** (17 members), **8** (15 members), **12** (22 members).

So the field is per-submission positional data compared across submissions.
The comparison is a category error, not a tuning problem.

## D2. Why the siblings stay strict

`candidate_id` and `basin_id` are derived from model/basin identity —
`candidate_id` is `"{source}:{cycle_time}:{model_id}:{scenario_id}"`, `basin_id`
is the basin name. Measured across the four submissions, for the 22 `run_id`s
appearing in more than one: `candidate_id`, `basin_id`, and `scenario_id`
changed **0 / 22** times, while `array_task_id` changed on every submission.

`array_task_id` is the only one of the three that is layout-dependent. Direction
A is therefore adopted **narrowed to that one field**; the siblings keep
present-but-different-is-fatal.

## D3. Why direction A forfeits nothing

Direction A gives up one layer of evidence: "this row belongs to *this* array
submission". Two questions had to be answered before adopting it.

**Is that proof held elsewhere?** Yes, and from a different input.
`_terminal_file_cohort_identity_matches` (`services/orchestrator/reconcile.py:1178-1240`)
builds `member_ids` from the **current** master's `cohort_members` (`:1206-1210`)
and bijects them against **live sacct** `record.array_task_records`
(`:1213-1240`). Its input is sacct, not `hydro_run`, so removing a `hydro_run`
field cannot weaken it.

(The sibling gate `_identity_mapping_matches_job`, `reconcile.py:921-951`, is a
different check again — one sacct accounting row against one `job` object, to
defend against `slurm_job_id` recycling. For a cohort master it is largely
inert, because a master carries no task number, guarded at `:892`.)

**Does anything else read the field?** No. A whole-repo grep over `services` and
`packages`, excluding tests, finds exactly **one** production reader of
`hydro_run.array_task_id`: the comparison being removed. There is no downstream
consumer.

## D4. `submission_attempt` is the same defect one field over — reported, not fixed

The retained strict set is **not** claimed sound. `:1829` compares
`hydro_run.submission_attempt` against the identity's, and the two ends have
asymmetric write semantics:

- the identity side **increases** — `:2207-2208` bumps the master's attempt on
  the reclaim path, and `:1793` reads that value;
- the `hydro_run` side **does not** — a `succeeded` row is frozen by the same
  `retriable_only` guard as in D1.

So a cohort reclaimed to attempt 2 is checked against attempt-1 rows and fails
per-member, exactly as `array_task_id` does. The precondition has already
occurred in production: the `…4a00ccdbaa78…` cohort reached attempt 2 while all
24 `hydro_run` rows in that cycle remained `succeeded` and frozen at attempt 1.

**Verified**: the write/compare semantics above, by source. **Not verified**:
that this particular cohort actually ran an identity check at attempt 2 and
failed on it. Precondition established is not the same as defect fired, and this
design does not claim the stronger statement.

Filed as **#1792**. Out of this issue's stated scope (`array_task_id`), so it is
reported and routed, not fixed here. The consequence worth stating for #1748:
this change stops **new** cohorts entering the wedge via layout churn, but a
cohort already reclaimed to attempt >= 2 stays blocked by `:1829`.

## D5. `project_forecast_cohort_tasks` is a legitimate use, and stays

`project_forecast_cohort_tasks` (`~:3363-3374`) writes `array_task_id` onto
**pipeline job** rows. This is the same idiom but not the same defect: there the
index is used to map *one submission's* task outcomes back to its members —
layout data used inside the layout's own lifetime. Only cross-submission
identity use is wrong. This is stated explicitly so a reviewer does not read the
narrowing as a general condemnation of the field, and so the acceptance
criterion "no implicit divergence between the two uses" has a written ruling.

## D6. Two falsified premises, corrected in place

Both assert that the per-model writers never persist
`candidate_id`/`basin_id`/`array_task_id`, which is what justified treating
`None` as "not stored" while keeping present-but-different fatal.

1. The comment above the comparison (`~:1813-1818`).
2. The donor change's design,
   `openspec/changes/fix-cohort-runtime-identity-absent-fields/design.md`.

Both are false as statements about production: array-shaped cohorts are written
by `create_hydro_run_from_basin` (`:1527`, called from `chain_manifests.py:386`),
which persists all three (`:1553-1554` and siblings), and every sampled
production row carries non-null values for all three. The `None` branch is dead
on production data; the leg that actually runs is the present-but-different one.

Corrections are recorded **in place** — the original reasoning stays visible —
per the project convention that produced ADR 0003's correction style.

> **Correction (round 2, P2).** This paragraph originally continued: "The donor
> was corrected and archived on this branch **before** this change's delta, so
> the false rationale never lands in `openspec/specs/`." That described the
> original plan, which round 1 found defective and commit `45e14464` reverted —
> but that commit only updated `tasks.md`, leaving this section asserting an
> archive that no longer happens. The PR's own two documents therefore
> contradicted each other, which is the same drift class the revert was fixing.
> **Current state**: the donor is corrected and stays an open change at
> `openspec/changes/fix-cohort-runtime-identity-absent-fields/`; both changes
> archive together in the post-merge chore commit, donor first. See D8 for why,
> and `tasks.md` section 1 for the probes that established the deferred path
> works.

## D7. Rejected directions, with the evidence that rejected them

**B — compare task numbers only within the same `submission_attempt`.** Refuted
by measurement, not by argument. `submission_attempt` scopes a *single
pipeline_job's* retry lineage, not a cohort's membership: it is incremented only
by `reclaim_pipeline_job_reservation` for the same `job_id`. The four cohorts of
`gfs_2026080712` are four distinct `job_id`s with independent lineages, three of
them at attempt 1. On the wedged 22-member cohort, the identity's attempt is 1
and every member row's attempt is 1 — **B's gate condition is satisfied**, the
task comparison runs anyway, and still fails on the 20 stale members. B is not
merely weak here; it is inert.

**C — add `array_task_id` to the degradation list.** Self-defeating. The
degradation only fires when the field is **absent**; the production failure is
present-but-**stale**. C re-arms the same trap while appearing to disarm it.

## D8. The #1759 standing rule has a hole, found by tripping over it while obeying it

PR #1759's three-round gate produced this standing rule:

> No delta edit lands without a clause-to-code check executed first.

I executed it. On commit `02b52cb4` I checked all of the donor's normative
clauses against the code and recorded "no clause is false". **That check was
correct and the rule still failed to protect anything**, because at that instant
the `array_task_id` comparison was still in the code, so
"present-but-different SHALL remain fatal" was *true*. Commit `764a1275`, two
commits later in the same PR, deleted the comparison and falsified it.

The hole is a time index. The rule checks the clause at **edit time**; what
`openspec archive` welds into `openspec/specs/` is the clause at the **landing
SHA**. Those are the same instant only when the PR contains no subsequent code
change to the surface the clause describes — which is exactly the case a fix PR
is not.

Sharpened rule, which this change now follows:

> A delta may only be archived at a SHA where its clauses are true, and for a PR
> that changes the behaviour a clause describes, that SHA is the PR's final head
> — never an intermediate commit. Concretely: **do not archive a change in the
> same PR that modifies the behaviour its delta asserts.** Defer the archive to
> the post-merge chore commit, where the code has stopped moving.

This is also why the repo's existing habit of deferring `openspec archive` to a
post-merge commit — adopted for a different reason, namely waiting on remote
receipts — turns out to be load-bearing for delta accuracy as well. The donor
archive was moved into that same post-merge commit for this change.

Recorded here rather than only in the loop log because the class has now fired
three times (twice inside PR #1759, once here) and the first two fixes did not
prevent the third.
