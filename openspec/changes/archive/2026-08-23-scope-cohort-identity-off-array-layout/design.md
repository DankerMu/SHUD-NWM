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
(`:6854`) refuses to rewrite an existing row unless its `status` is `failed` or
`cancelled` — the `retriable_only` guard at `:6861-6873`, which raises
`HYDRO_RUN_NOT_RETRIABLE` — so a row's `array_task_id` is **frozen at the
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
builds `member_ids` from the **current** master's `cohort_members`
(`reconcile.py:1207-1211`)
and bijects them against **live sacct** `record.array_task_records`
(`reconcile.py:1213-1240`). Its input is sacct, not `hydro_run`, so removing a
`hydro_run`
field cannot weaken it.

(The sibling gate `_identity_mapping_matches_job`, `reconcile.py:921-951`, is a
different check again — one sacct accounting row against one `job` object, to
defend against `slurm_job_id` recycling. For a cohort master it is largely
inert, because a master carries no task number, guarded at
`reconcile.py:890-894`.)

**Does anything else read the field?** A whole-repo grep over `services`,
`packages`, `apps`, `workers`, and `db`, excluding tests, finds exactly **one**
production *comparator* of `hydro_run.array_task_id` — the comparison being
removed — and none after it. Every other `array_task_id` hit is
`pipeline_job.array_task_id`, a different record's own layout index backed by
`db/migrations/000012_pipeline_job_array_task.sql`, or a `SacctRecord` field.

State the residual precisely rather than as "no downstream consumer", which
would be too strong: the field is still **persisted** on the row and still
**travels outward** through `_hydro_run_for` → `_public_scheduler_row` (`:6884`)
into API/display projections. Nothing there branches on it or compares it; it is
carried, not consulted. So this change removes the last decision that reads the
field, not the field itself — which is the intended scope, since the persisted
value remains a true record of the submission that wrote the row.

## D4. `submission_attempt` is the same defect one field over — reported, not fixed

The retained strict set is **not** claimed sound. `:1841` compares
`hydro_run.submission_attempt` against the identity's, and the two ends have
asymmetric write semantics:

- the identity side **increases** — `:2219-2221` bumps the master's attempt on
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
cohort already reclaimed to attempt >= 2 stays blocked by `:1841`.

## D5. `project_forecast_cohort_tasks` is a legitimate use, and stays

`project_forecast_cohort_tasks` (`~:3363-3374`) writes `array_task_id` onto
**pipeline job** rows. This is the same idiom but not the same defect: there the
index is used to map *one submission's* task outcomes back to its members —
layout data used inside the layout's own lifetime. Only cross-submission
identity use is wrong. This is stated explicitly so a reviewer does not read the
narrowing as a general condemnation of the field, and so the acceptance
criterion "no implicit divergence between the two uses" has a written ruling.

## D6. The falsified premise, corrected in place in all three carriers

Each asserts that the per-model writers never persist
`candidate_id`/`basin_id`/`array_task_id`, which is what justified treating
`None` as "not stored" while keeping present-but-different fatal.

1. The comment above the comparison (`~:1813-1818`).
2. The donor change's design,
   `openspec/changes/fix-cohort-runtime-identity-absent-fields/design.md`.
3. The donor change's **proposal**, same directory — carrying the premise in
   its own words ("returns `False` for **every** inflight forecast cohort";
   the fixture shape is "a shape production never writes"). This section
   originally enumerated only the first two; the third was found by the
   invariant-closure audit (D9) after three sweeps had missed it.

All are false as statements about production: array-shaped cohorts are written
by `create_hydro_run_from_basin` (`:1718`, called from `chain_manifests.py:386`),
which persists all three (the `row` dict at `:1724-1731`), and every sampled
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

## D9. The same fix-the-named-item-not-the-class failure, five times in one PR — including once by the sweep meant to end it, and once by the correction commit itself

Five passes on this PR each found the same defect in a different file:

| pass | where | what it said |
|---|---|---|
| round 1 (P1) | `openspec/specs/` | donor archived here, landing a clause this PR falsifies |
| round 2 (P2) | `design.md` D6 | "now archived as `archive/2026-08-23-…`" |
| Phase 7 (P2) | `proposal.md` | "corrected before archive on this branch" |
| gate retro (invariant-closure audit) | donor `proposal.md`, donor `tasks.md`, `tests/test_gateway_reconcile.py`, the PR body | the falsified premise and the archive-state claim, in the four documents no prior pass had opened |
| round 4 (P2) | the correction blocks written by `9e962bd3` | `create_hydro_run_from_basin` cited at `:1527`/`:1553-1554`; actually `:1718`/`:1724-1731` |

Each fix corrected **the file it was shown** and left the others. Findings two
through four are not new defects — they are the first defect, in the places the
preceding fix did not look. The fifth is the class reappearing inside its own
remedy.

This is a recorded personal failure mode, not a novel one.
`docs/review-loop-log.jsonl` carries it against issue #1671 under the class
`same-defect-class-recurred-after-a-named-fix`, with the note: *"After T10/T10b's
missing evidence was fixed, T11/T12 were still ticked with nothing pasted — the
identical defect, one task later. I had fixed the two items that were named
instead of sweeping the class."* That entry is dated `2026-08-23` — the **same
day** as every commit on this PR, not a historical lesson from some earlier era.
(An earlier revision of this section said "the day before"; that was wrong.)

**The Phase 7 sweep did not close it, and failed for the same reason as the
fixes it was correcting.** The sweep was
`grep -rn "archive\|archived\|before this change\|on this branch"` across both
changes' documents. It found a live falsehood in `proposal.md` and a stale claim
in `tasks.md` that no reviewer had flagged — real value — but it was scoped to
the terms of *the assertion it had been shown*. The donor's `proposal.md` and
`tasks.md` do not carry archive terms; they carry the **premise** terms
("returns `False` for every inflight forecast cohort", "a shape production never
writes", "the three degradable fields"). So the sweep walked past them, exactly
as each targeted edit had walked past the file it was not shown. A grep is only
as broad as the vocabulary its author already suspects.

**What actually closed it** was the gate's invariant-closure audit: an
independent pass that enumerated *every* factual claim in all eight documents of
both changes plus the PR body, and adjudicated each against head — a closed list,
not a keyword filter. It returned nine false claims, six of which no reviewer and
no sweep had seen. That is the second time the closed-list pattern has earned its
place (PR #1759 was the first).

**Round 4 added a fifth instance, in the correction commit itself.** The
"Superseded"/"Correction" blocks written by `9e962bd3` — the commit whose entire
purpose was to close this class — cited `create_hydro_run_from_basin` at
`:1527` with its row dict at `:1553-1554`. Both are wrong: the function is at
`:1718` and the dict at `:1724-1731`; `:1527` lands in an unrelated migration
journal loop. The *substance* of the correction was right, but a reader
following the pointer to check the evidence lands on unrelated code. Recorded
rather than quietly fixed, because it is the sharpest available demonstration
that this failure mode is not closed by intending to close it. The
countermeasure is the same closed-list discipline applied one level down: a
citation is a claim, and `file:line` claims are checked by opening the file, not
by trusting the memory that wrote them.


Standing rule, revised — the earlier phrasing was the one that just failed:

> When a review finding is "document X asserts something no longer true", the
> fix is neither an edit to X nor a grep for X's wording. It is a closed-list
> pass: enumerate every claim-bearing document in the change set — both changes,
> test comments, and the PR body — and rule on each one's claims against head.
> A grep can only find the vocabulary you already suspect; the claim you missed
> is by construction phrased in words you did not think to search.

The cost is visible: three extra review passes, a three-round gate entry with a
persisted retro, and four extra commits, on a PR whose production change is three
deleted lines. Every verified finding on this PR was in orchestrator-authored
prose; none was in the code. The code was right at round 1.
