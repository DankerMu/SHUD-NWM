# A model succession can be a cold start

Issue: #2740 (part D of #1815; depends on C #2739, merged). Fixture level: **compact**.
Risk packs: **Production control-plane (scheduler timer)**, **Ordering fail-closed**, **Kind misrouting
fail-closed**.

## Why

`scripts/node22_model_succession.py` accepts only `--kind recalibration`: the new model takes over the state
of the old one through clone rows. A structural change (mesh, river network, any other of the eight
state-compatibility surfaces, or different `cfg.ic` bytes) makes the old state unusable: the clone gate refuses it (`state_compatibility_unequal`), and
the succession has no way on. The new model then has to start from the calibrated initial condition shipped
in its package. The scheduler already does that on its own: a `model_id` without state history in any
generation is on the first-cycle branch of `services/orchestrator/scheduler_generation.py`, admitted as
`packaged_ic_bootstrap` when the packaged IC is qualified, blocked as
`first_cycle_initial_state_undecided` when it is not, and started as a legacy `cold_new_model`
(`cold_start_reason=no_prior_history`) when no qualification signal exists for the row. No approval artifact is involved. What is missing is
the succession kind that skips the clone, proves the packaged IC before anything is published, and says in
its receipts that the hydrograph is not continuous.

The two kinds must not be confused. A calibration-only change sent through a cold start throws away a usable
state; a structural change sent through `recalibration` is refused only late and with a message that does not
name the way out.

What is and is not structural is what the gate says, not a list in prose: `STATE_COMPATIBILITY_SURFACES` is
the ten hydrologic-core surfaces without `calibration` and `solver_config`. A change of only `cfg.calib` or
only `cfg.para` is state-compatible and belongs to `recalibration`.

## What changes

1. `--kind cold_start` is accepted. Its steps are `copyback`, `preflight`, `begin`, `publish`, `refresh`,
   `finish`: there is no `clone` step, no clone dry-run, no clone receipt, and no state index is read or
   written. Every step is still gated on the receipt of the step before it in the list of its kind
   (`publish` on `step-begin.json`). The timer rules, the resume rules, the failure receipt and `--abort` are
   those of `recalibration`. `--pair`, `--cutover-time` and the other arguments are unchanged.
   `recalibration` behaves as before, and its `plan.json` keeps its current content.

   Everything that today assumes the seven steps follows the step list of the kind. Known places:
   `STEPS`, `STEPS_NEEDING_STOPPED_SCHEDULER` and `completed_steps` in `model.py`; the step table, the
   preceding-receipt check, the apply loop and the dry-run report in `run.py`; `preflight` and `preview` in
   `tools.py` (both call the clone tool unconditionally); `finish` in `scheduler.py` (requires the receipt of
   every earlier step, `step-clone.json` included); the kind check in `plan.py` and the `--kind` choices,
   `DRY_RUN_NOTICE` and the `--cutover-time` help in the entry point; the `RUNBOOK` constant (section 5.7.1
   in every failure text; `cold_start` names 5.7.2).
   "The run has reached publish" is today "`step-clone.json` exists" (`published_without_receipt` and the
   differing-manifests refusal in `plan.py`, and through them the hard stop of `publish` and the
   `publish_state` of `--abort`). It becomes "the receipt of the step before `publish` in the list of the
   kind exists" (`step-begin.json` for `cold_start`). Without this a cold-start publish that took effect
   without a receipt would be reported as not published, and its abort would say the old models keep running.

2. Kind check, in `preflight` of `cold_start`, after `copyback` and before the audit: for every pair the
   eight-surface state-compatibility comparison (`STATE_COMPATIBILITY_SURFACES`, the gate of the
   recalibration clone) is evaluated on the two packages in the compute store. If the surfaces are equal for
   any pair, the run is refused: the pair is a recalibration, its state can be carried, and the message names
   the pair and `--kind recalibration`. Unequal surfaces, including a surface file present on one side only,
   let the run continue. Anything that prevents the comparison is a step failure, never "unequal": a model
   row that cannot be found (old rows come from the canonical manifest, new rows from the new-rows registry,
   as the clone tool merges them), a package root missing in the compute store, zero or several `*.sp.att` /
   `*.cfg.ic` / `*.cfg.para`, a `CutoverCloneError`, an unparsable file. A succession with pairs of both sorts is refused the same way; it is split into two
   successions.
   The comparison is not reimplemented and is not read off a clone dry-run (the clone gate also refuses for
   reasons that are not about the surfaces). `scripts/node22_clone_direct_grid_cutover_states.py` gets one
   public function that builds the per-side gate inputs from two package roots; its own pair loop uses it,
   and the succession tool calls `verify_hydrologic_core_fingerprint_equal` with those inputs. The clone
   tool's behaviour and receipts do not change.

3. The other direction, in `preflight` of `recalibration`: the same comparison runs for every pair before
   the clone dry-run (the clone gate checks the source state first, so a structural pair without a qualified
   state at the cutover time would otherwise be refused for that reason and never be told its kind is wrong).
   Unequal surfaces: step failure naming the pair and saying the change is structural and needs
   `--kind cold_start` with a new `--succession-id`. A comparison that cannot be made is a step failure as in
   item 2. The clone dry-run and its refusals are otherwise unchanged.
   In a dry-run of `recalibration` the comparison is one more entry of the report and of `would_be_refused`;
   the clone dry-run and the publish dry-run still run and are reported as today. Only the `preflight` of an
   apply stops at the comparison.

4. IC audit, in `preflight` of `cold_start`, after the kind check and before the publish dry-run:
   `scripts/audit_first_cycle_initial_state.py` is called in-process (`build_receipt`) on the new-rows
   registry of the provision step (the file the publish step takes its rows from), with the compute store as
   object store root (the packages the runs will read, there after `copyback`), without a workspace root, and
   with the sources of the plan's new models. The audit is not run on the merged manifest: a row of another
   basin that is not qualified today must not block every cold start. The gate: every new model of the plan
   has at least one audit row, and each of its rows has `ic_status` equal to `PACKAGED_IC_QUALIFIED`. Rows of
   other models in that registry are in the receipt and are not gated (they may be
   `unreadable`: `copyback` copies only the packages of the plan). A blocked audit, a missing row and a
   row that is not qualified are each a step failure before the timer is touched; the message lists the
   model ids with their `ic_status`.
   `ic-audit.json` is written only when the gate passes, once, in the succession directory with the shared
   receipt writer (`O_EXCL`, never overwritten); a failing audit leaves no file, its per-model result goes
   into the failure message and the failure receipt, and the same command can be run again after the cause is
   removed. On a resume an existing `ic-audit.json` is read and held to the same gate, not rewritten. The audit
   is also run again, read-only, and for every new model the `ic_status` and `ic_sha256` it finds must equal
   those in the receipt (a package changed in the compute store after the audit is a step failure; the
   receipt is never rewritten, so the way on is a new `--succession-id`). `step-preflight.json` records its path and sha256.

5. `publish` of `cold_start` requires the audit receipt: before the publish apply is called (not when an
   existing `publish-apply.json` is adopted) it reads
   `ic-audit.json`, compares its sha256 with the one in `step-preflight.json` and applies the gate of item 4
   again. Missing, changed or failing: step failure, nothing published.

6. Continuity notice. For `cold_start`, `plan.json`, `step-publish.json`, `step-finish.json`, the dry-run
   report and the report of an apply carry a `continuity` object: `mode` `cold_start`, `state_carried`
   false, `declared_cutover_time` (the `--cutover-time`), and a `notice` saying that no state is carried from
   the old models, that each new model starts from the calibrated initial condition in its package at the
   first cycle the scheduler plans after the timer is started, and that the hydrograph of these basins is
   discontinuous there. The cutover time is recorded as declared by the operator; the tool does not enforce
   it. `continuity` is written beside the plan record, not inside the part that a resume compares, so a later
   change of the notice text cannot refuse a resume.

7. Dry-run and abort. A dry-run of `cold_start` reports the kind check and the audit result (gate outcome
   and per-model `ic_status`) when the packages are already in the compute store, writes no `ic-audit.json`,
   and lists no clone step. The abort text of `cold_start` does not mention clone rows.

8. Runbook: `docs/runbooks/production-ops/recalibration-and-archive.md` gets a section 5.7.2 for the
   cold-start succession (when it applies, the command, what `preflight` refuses, the continuity notice).
   `service-bringup.md` is 989 lines of 1000: it gets at most a pointer that adds no line.

## Must preserve

- Every existing test of the succession tool, the clone tool, the publish tool and the audit passes
  unchanged; the clone tool's receipts and refusal scopes are byte-for-byte what they were.
- DB-free: the tool still refuses when a database variable is set, and importing the audit must not open a
  database.
- No step of `cold_start` changes anything before `begin` except the copy of `copyback` and receipts in the
  succession directory.
- Works on Python 3.11 and 3.12; no file over 1000 lines; `scripts/` packages have no `__init__.py`.

## Out of scope

Add-basin and remove-basin successions (#2741), the open gaps listed on #2739, #2749, any change to the
scheduler's first-cycle decision, any production `--apply`.

## Evidence

Tests, in a new suite `tests/test_node22_model_succession_cold_start.py`, on real package files so that the
fingerprint and the audit actually run:

- `cold_start` apply with structurally different packages and qualified ICs: steps run in order without
  `clone`; no clone receipt; both state indexes byte-identical before and after; manifests published;
  `ic-audit.json` present; `continuity` in `plan.json`, `step-publish.json`, `step-finish.json`.
- `cold_start` with state-compatible packages: refused in `preflight`, timer never stopped, manifests
  unchanged, message names `--kind recalibration`.
- `recalibration` with structurally different packages: refused in `preflight`, message names
  `--kind cold_start`.
- One new model with an unqualified IC, one with no package manifest reference (`absent`): each refused in
  `preflight`, timer never stopped, nothing published.
- `publish` with `ic-audit.json` removed, and with it altered: refused, manifests unchanged.
- Resume after a failure in `publish`: `ic-audit.json` is not rewritten, the run completes.
- Resume of `preflight` after the IC in the compute store was changed since `ic-audit.json` was written:
  refused, nothing published.
- A pair that differs only in `cfg.ic` bytes, and a pair that differs only in a core surface file: each is
  structural under both kinds.
- Dry-run: no `ic-audit.json`, no file changed at all, the report lists no clone step and carries
  the audit result and `continuity`.
- Abort after `begin`: timer started with the confirmation flag, text without clone rows.
- `cold_start` publish that took effect but left no receipt: hard stop on the rerun, and `--abort` reports it
  as published.
- `cold_start` with differing manifests after `begin`: refused as for `recalibration`.
- A pair whose comparison cannot be made (a package root missing, a surface file missing where exactly one is
  required): step failure under both kinds, not treated as unequal.
- `recalibration` with a structural pair and no qualified source state at the cutover time: the message
  still names `--kind cold_start`.
- Dry-run of `recalibration` with a structural pair: the report carries the kind refusal and, unchanged, the
  `clone_dry_run` refusal and the `publish_dry_run` result.

Test fixtures: the existing helpers cannot produce a package the audit qualifies (the package manifest has no
`included_files`, the rows carry no `shud_input_name`, and the helper IC's first line has no numeric tokens),
and they pin `--kind recalibration` and seven steps. The new suite builds its own rows and packages:
`shud_input_name` in `resource_profile`, an IC whose first line has three or four numeric tokens, a
structural difference through a core surface file or different IC bytes. Helper functions get parameters
whose defaults keep today's behaviour. The `absent` case cannot go through the provision helper (the
publisher requires `manifest_uri`); its registry is written by hand with its sha256 in the provision receipt.
