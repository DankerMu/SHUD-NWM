# A model succession can remove a basin

Issue: #2757 (part E2 of #2741 / #1815; this is its node-22 half, the node-27 retirement tool is a second
change). Fixture level: **compact**. Risk packs: **Production control-plane (scheduler timer)**,
**Ordering fail-closed**.

## Why

Retiring a basin starts on node-22: its rows are removed from both registry manifests so the scheduler stops
producing runs for it (`operating-scope.md` 7.2). Today that is the publish tool by hand with the timer
stopped by hand. The succession tool already owns exactly this (timer, receipt gating, publish, refresh,
resume, abort) for the kinds that replace and add models.

Owner decisions (2026-10-06): E is split; the tool never touches a Basins directory; the node-27 half (runs,
exclusion list, model rows) is a separate tool that refuses to run before this succession has finished.

## What changes

1. `--kind remove_basin` with `--remove <model_id>` (repeatable; every source of each removed basin). Steps:
   `preflight`, `begin`, `publish`, `refresh`, `finish`. No `copyback`: nothing is provisioned and no package
   is copied. Timer rules, receipt gating, resume, failure receipt, `--abort` and the hard stops follow from
   the step list of the kind.

2. Arguments by kind, refused before anything is written: `remove_basin` requires at least one `--remove` and
   refuses `--pair`, `--add`, `--cutover-time`, `--provision-succession-id` and `--new-rows-registry`; the
   other kinds refuse `--remove`. A model id named twice is refused. The refusal texts of the existing kinds
   stay verbatim.

3. Plan: `Plan.removes`; `old_ids` is the old ids of the pairs followed by the removes, `new_ids` is unchanged
   (empty for this kind). The plan record of `remove_basin` is `kind`, `removes`; the records of the three
   existing kinds keep exactly their keys and values.

4. Inputs: this kind has no provision receipt and no new-rows registry. `check_inputs` for it reads neither
   and checks only: the manifests-differ refusal past `begin`, every removed id is in the canonical manifest
   (while no `publish-apply.json` exists), and the "publish in effect without receipt" refusal. `Inputs` for
   this kind has `provision_receipt` `None`, `new_rows_registry` `None` and `new_rows` `{}`. The places that
   dereference them today each get a guard for it: `_plan_record` and `write_plan` in `plan.py` (the keys
   `provision_apply_receipt_sha256`, `new_rows_registry_sha256`, `provision_apply_receipt`,
   `new_rows_registry` are left out for this kind, not written as null), the dry-run in `run.py` (no copyback
   report, straight to the preview; `_PREFLIGHT_CHECKS_BY_KIND` gets an entry for the kind), the report of
   `run.py`, and `_call_publish` (`new_rows_registry=None`). `Plan.provision_succession_id` keeps the default
   of `build_plan` (the succession id) and is passed to the publish tool as today: the publish tool defaults
   it the same way and writes it into its receipts, which the reuse check of the dry-run receipt on a resume
   compares.

5. "Publish in effect without receipt" needs no new predicate: with the removes in `old_ids` the existing
   one holds, and its gate on the receipt of the step before publish (`step-begin.json` for this kind) makes
   it false before `begin`. So before `begin` a removed id that is not in the manifest is the ordinary "is
   not in the canonical manifest" refusal, never the hard stop; a test pins that. The order of the checks in
   `check_inputs` is not changed for any kind.

6. `preflight`: the publish tool's dry-run with `Operations(remove=...)`. It already refuses a basin left with
   only some of its sources and an id that is not in the manifest. No kind check, no audit, no clone.
   `publish`: the publish apply with the same operations. `finish`: the existing check (no old id in either
   manifest) holds through `old_ids`.

7. Every place that branches on the kind gets an explicit `remove_basin` case (the dispatch points listed in
   the add-basin change): `tools.py` preflight / preview / `_operations` / publish preview fields (the publish receipt's key is `removed_model_ids`; the
   preview reports it under that same name in place of `replaced`), `model.py` `RUNBOOK_BY_KIND` and `continuity()`, `plan.py`, `run.py` dry-run notes and abort
   text ("keeps running the old models" becomes, for this kind, that the basin keeps being scheduled), the
   entry point help and dry-run notice. Three texts say "new models" and are wrong for this kind; each gets
   its own wording for it (the removed models are out of both manifests): the two abort outcomes `published`
   and `published_without_receipt` in `run.py`, and the hard-stop refusal "already hold every new model_id
   of this plan and no old one" in `plan.py`.

8. `continuity` for this kind: `mode` `basin_removed`, `state_carried` false, and a notice that the scheduler
   no longer plans the basin, that runs already submitted may still finish and be ingested, and that the
   node-27 retirement tool is the next step and takes the same `--succession-id`.

9. No check of the batch queue. The Slurm job name is the same for every basin, so jobs of a removed model
   cannot be told apart by `squeue`; `begin` already waits for a running scheduler pass, and a run that still
   arrives is handled on node-27 (the exclusion comes before the runs are superseded).

10. Runbook: `operating-scope.md` 7.2 (the block "退役流域的 manifest 发布" at its end) points to the tool
    for the manifest removal (the manual publish command stays as the fallback); `recalibration-and-archive.md` gets 5.7.4, pointing to the code block of 5.7.1
    (the entrypoint guard's command-line count stays).

## Must preserve

- Every existing test passes unchanged; plan records of `recalibration`, `cold_start`, `add_basin` unchanged.
- DB-free; Python 3.11 and 3.12; no file over 1000 lines.
- Nothing is changed before `begin` except receipts in the succession directory.

## Out of scope

The node-27 retirement tool; Basins directories; any production `--apply`.

## Evidence

New suite `tests/test_node22_model_succession_remove_basin.py`, on the two-source workspace of the add-basin
suite. Helper additions (`tests/model_succession_helpers.py`), each defaulting to today's behaviour:
`build_space` can skip the provision (no `provision-apply.json` written) and build with no new rows;
`Space.argv` takes `removes` and then emits neither `--pair`, `--add` nor `--cutover-time`; `Space.pairs` and
`Space.registry` tolerate no new rows; a variant of `main_as` that does not append
`--provision-succession-id`.

- apply removing both sources of one basin: five steps in order, no `step-copyback.json`, no provision
  receipt in the receipt root at all, manifests lose exactly those two rows and every other row is equal,
  both state indexes byte-identical, timer stopped then started, `continuity.mode == "basin_removed"` in
  `plan.json`, `step-publish.json`, `step-finish.json`.
- only one source removed: refused in `preflight` by the publish dry-run, timer never stopped.
- a removed id not in the manifest, before any step: the "not in the canonical manifest" refusal, not the
  hard stop.
- `--remove` with each other kind; `--pair`, `--add`, `--cutover-time`, `--provision-succession-id`,
  `--new-rows-registry` with `remove_basin`; no `--remove`; an id named twice: each refused, nothing written.
- `publish` reached without `step-begin.json`: refused, manifests unchanged.
- resume after a failure in `publish`; publish in effect without receipt (hard stop; abort reports
  published); abort after `begin` (timer started). None of these texts contains "new model" or "--pair".
- past `begin` with manifests that differ: refused.
- the continuity notice names the node-27 retirement tool and the same `--succession-id`.
- dry-run: nothing changed, the report lists the five steps and the ids the publish would remove, and has no
  copyback, audit, clone or kind check.
- `plan.json` of a `recalibration`, a `cold_start` and an `add_basin` plan equals a literal of today's
  content (header fields aside).
