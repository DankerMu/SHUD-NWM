# A model succession can add a basin

Issue: #2756 (part E1 of #2741 / #1815; depends on C #2739 and D #2740, merged). Fixture level: **compact**.
Risk packs: **Production control-plane (scheduler timer)**, **Ordering fail-closed**.

## Why

`scripts/node22_model_succession.py` replaces models (`--pair old:new`). Bringing a new basin into the
scheduler has no old model: after the node-27 provision step its variants only have to be copied to the
compute store, proven to ship a qualified initial condition, added to the merged registry and picked up by a
provider refresh, with the scheduler stopped around the publish. Today that is hop 3b to hop 4 of
`service-bringup.md` 3.1.1, by hand, with the same ordering traps the succession tool already closes for the
other kinds.

Owner decisions (2026-10-06): E is split; this part is the node-22 side of adding a basin. Baseline publish,
the copy into the NFS Basins tree, the node-27 seed and the node-27 provision stay the existing commands; the
tool never touches a Basins directory.

## What changes

1. `--kind add_basin` with `--add <new_model_id>` (repeatable; one per source of each new basin). Its steps
   are those of `cold_start`: `copyback`, `preflight`, `begin`, `publish`, `refresh`, `finish`. The timer
   rules, receipt gating, resume, failure receipt, `--abort` and the "publish in effect without a receipt"
   hard stop are unchanged and follow from the step list of the kind.

2. Arguments by kind, refused before anything is written:
   - `add_basin` requires at least one `--add`, refuses `--pair` and refuses `--cutover-time` (there is no
     cutover: the basin has no history).
   - `recalibration` and `cold_start` refuse `--add` and still require `--pair` and `--cutover-time`
     (`--cutover-time` stops being required by the argument parser and is required by the plan check, with
     the same message shape as the other plan refusals).
   - a model id named twice, in any combination of `--pair` and `--add`, is refused.

3. The plan: `Plan` gets `adds` beside `pairs`; `new_ids` is the new ids of the pairs followed by the adds,
   `old_ids` stays the old ids of the pairs. Everything already written in terms of `new_ids` / `old_ids`
   (the provision-receipt checks, "new id already in the canonical manifest", the copy of `copyback`,
   `published_without_receipt`, the `finish` check) then holds for an add without a second code path.
   That covers the consumers of the id lists only. Every place that branches on the kind today is a
   two-way `== cold_start` test and would send `add_basin` down the `recalibration` side; each gets an
   explicit `add_basin` case: in `tools.py` the `preflight` and `preview` branches (no clone dry-run), the
   `ic-audit.json` re-check before the publish apply (must run for `add_basin`), the kind check (skipped, not
   run on zero pairs), `_operations` (`add=`), the audit refusal texts that say "cold start", and the publish
   preview (report `introduced_model_ids`); in `model.py` `RUNBOOK_BY_KIND` (5.7.3) and `continuity()`; in
   `plan.py` the argument rules, the `cutover_time` type (now optional) and the "neither a new id of a
   --pair" text of the unaccounted-id refusal; in `run.py` the dry-run notes that mention the kind check and
   the abort text "keeps running the old models"; in the entry point the `--kind` help and the choice of the
   dry-run notice. The existing refusal texts "is not YYYYMMDDHH", "at least one --pair" and "one --pair
   only" stay verbatim for the two existing kinds.
   The plan record of `add_basin` is `kind`, `adds`, `provision_succession_id`; the records of the two existing
   kinds keep exactly their current keys and values, so a succession directory written before this change
   resumes.

4. `preflight` of `add_basin`: no kind check (there is no pair to compare). The initial-condition audit of
   `cold_start`, unchanged: every new model qualified, `ic-audit.json` written only on a pass, re-audit on a
   resume. Then the publish tool's dry-run with `Operations(add=<the adds>)`.

5. `publish` of `add_basin`: the `ic-audit.json` re-check of `cold_start`, then the publish apply with
   `Operations(add=...)`. The publish tool already enforces that a new id is absent from the canonical
   manifest, that every basin ends with exactly one row per source, and the row count
   `before + adds`; nothing of that is duplicated here.

6. `continuity` for `add_basin` in `plan.json`, `step-publish.json`, `step-finish.json` and the reports:
   `mode` `new_basin`, `state_carried` false, and a notice that the basin has no earlier forecasts and each
   model starts from the calibrated initial condition in its package at the first cycle the scheduler plans
   for it after the timer is started, which is the earliest cycle of its lookback window, not the current
   one. No `declared_cutover_time`.

7. Dry-run and abort texts name the kind's steps and do not mention pairs, old models or clone rows.

8. Runbook: `docs/runbooks/production-ops/recalibration-and-archive.md` gets 5.7.3 (when it applies, what
   stays manual before it — hops 1, 1b, 2, 3 —, the arguments, what `preflight` refuses, that a pure add
   needs no forcing backfill per `service-bringup.md`, that the new basin then catches up cycle by cycle from the
   earliest cycle of the lookback window, and the lookback constraints of that runbook while it does). `service-bringup.md` is 989 lines of 1000: hop 3b/4 get a pointer reworded in
   place, adding no line. The node-22 command-line count pinned by the entrypoint guard for the succession
   tool stays as it is (5.7.3 points to the code block of 5.7.1, as 5.7.2 does).

## Must preserve

- Every existing test passes unchanged; the plan record in `plan.json` of `recalibration` and `cold_start`
  (everything but the header fields) is unchanged for the same arguments.
- DB-free; Python 3.11 and 3.12; no file over 1000 lines (`scripts/model_succession/tools.py` is 633).
- No step changes anything before `begin` except the copy of `copyback` and receipts in the succession
  directory.

## Out of scope

Removing a basin (#2757); the node-27 seed; forcing backfill; any production `--apply`.

## Evidence

Tests in a new suite `tests/test_node22_model_succession_add_basin.py`, on real package files.

Helper work this needs (`tests/model_succession_helpers.py`; every new parameter defaults to today's
behaviour, the four existing suites run unchanged): the helpers build single-source (`gfs`) canonical
manifests and rows, and the publish tool requires every basin to have exactly the sources the canonical
manifest has. So: a source parameter for the row builder, a workspace whose canonical manifest has two
sources, and an argv builder for `--add` without `--pair` / `--cutover-time`. Reusable as they are: the
package-file layer of the cold-start suite (the qualified initial condition and the package writer).

- `add_basin` apply with the two models (two sources) of one new basin in a two-source workspace: six steps in
  order, no clone receipt, both state indexes byte-identical, manifest row count `before + 2`, every earlier
  row byte-identical, `ic-audit.json` present, `continuity.mode == "new_basin"` in `plan.json`,
  `step-publish.json`, `step-finish.json`.
- `--pair` with `add_basin`, `--add` with each other kind, `--cutover-time` with `add_basin`, no `--add`,
  `recalibration` without `--cutover-time`, an id named twice: each refused, nothing written.
- an `--add` id already in the canonical manifest; an `--add` id that is not in the provision receipt; a
  provisioned id of the receipt that is neither added nor in the canonical manifest (the refusal names
  `--add`, not `--pair`, for this kind): each refused before any step.
- a new model whose initial condition is not qualified: refused in `preflight`, timer never stopped.
- only one source of a new basin added, with a provision receipt that lists only that model (otherwise the
  unaccounted-id refusal comes first): the publish dry-run's refusal surfaces as the `preflight` failure.
- `publish` reached without `step-begin.json`: refused, manifests unchanged.
- `publish` with `ic-audit.json` removed: refused, manifests unchanged (the re-check runs for this kind).
- resume after a failure in `publish`; publish in effect without receipt (hard stop, abort reports
  published); abort after `begin` (timer started, text without pairs or old models).
- dry-run: nothing changed, the report lists the six steps, the audit result and the ids the publish would
  introduce; no clone and no kind check in it.
- `plan.json` of a `recalibration` and of a `cold_start` plan: its plan record (everything but the header
  fields `generated_at`, `host`, `git_commit`) equals a literal of today's content.
