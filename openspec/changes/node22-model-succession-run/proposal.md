# One node-22 command runs the compute-side steps of a model succession

Issue: #2739 (part C of #1815; depends on A #2737 and B #2738, both merged). Fixture level: **compact**.
Risk packs: **Production control-plane (scheduler timer)**, **Ordering fail-closed**, **Partial-failure
recovery**.

## Why

After the node-27 provision step, a recalibration succession on node-22 is five manual steps with a hard
order: stop the scheduler timer, copy the new variant packages from the shared store to the compute store,
clone the state rows (old model -> new model, both state indexes), publish the merged registry manifest, run
the provider refresh, start the timer. `recalibration-and-archive.md` section 5.7.1 explains why the order
matters: if the manifest lands before the clone rows, the new model has no state row and is admitted on the
first-cycle branch (`PACKAGED_IC_BOOTSTRAP`) instead of continuing warm. Today nothing enforces the order, the
copy is a hand-run `cp -r`, and the timer is stopped and started by hand. Model succession is frequent.

The manual procedure copies the packages and runs the dry-runs while the scheduler is running and stops the
timer only for the applies; this tool keeps that shape, so the scheduler is stopped for the clone apply, the
publish, the refresh and nothing else.

Owner decisions (2026-10-05): one command per machine, no ssh between machines; receipts on the shared store,
each step refuses when the previous receipt is missing; the tool stops the timer itself and starts it itself
at the end; it stops the timer with a plain `stop` and re-checks before every writing step that nobody
started it (no systemd drop-in fence).

## What changes

1. New tool `scripts/node22_model_succession.py` (entry point; logic in a package
   `scripts/model_succession/` without `__init__.py`, like the other script packages; every file under 1000
   lines). DB-free: it refuses when a database variable is set. Invoked as
   `cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_model_succession`. It is a dry-run
   unless `--apply`. Receipts are written with the helpers of `packages/common/succession_receipt.py`.

2. The plan is given on the command line: `--succession-id`, `--provision-succession-id` (default: the
   succession id), `--kind recalibration` (the only kind in this change), `--pair
   <old_model_id>:<new_model_id>` (repeatable; one per source of each basin), `--cutover-time <YYYYMMDDHH>`,
   `--operator-id`, `--new-rows-registry <path>` (as in the publish tool), `--receipt-root` (default
   `<provider store root>/scheduler/succession`). Paths and roots come from the environment of
   `infra/env/compute.scheduler-provider-refresh.env` (`OBJECT_STORE_ROOT`,
   `NHMS_SCHEDULER_PROVIDER_STORE_ROOT`, `OBJECT_STORE_PREFIX`, the two registry manifests,
   `NHMS_SCHEDULER_STATE_INDEX`, `NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK`,
   `NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT`); the compute-side state index copy is
   `<OBJECT_STORE_ROOT>/scheduler/state-index/index-last.json` unless `--mirror-state-index` says otherwise.

3. The first apply exclusive-creates `plan.json` in `<receipt-root>/<succession-id>/`: the plan (kind, pairs
   in order, cutover time, provision succession id), the sha256 of `provision-apply.json` and of the new-rows
   registry. Every later run and `--abort` compare the command line with it; a difference is a refusal.
   `operator_id` is not compared. Before anything else, in the dry-run and in the apply, the tool refuses
   when: `provision-apply.json` of the provision succession is missing or not `outcome = applied`; a new id
   of the plan is not one of its `models[].model_id`; one of its `models[].model_id` is neither a new id of
   the plan nor in the canonical manifest; an old id is not in the canonical manifest; a new id already is;
   the new-rows registry's sha256 is not the one the provision receipt recorded; another succession under
   the same receipt root holds the timer (its `timer-before-stop.json` says the timer was active and it has
   neither `step-finish.json` nor an abort receipt; an unreadable one is a refusal naming it) - abort or
   resume that one first. The comparison with
   `plan.json` comes first, so a mistyped command line is reported as that. Once `publish-apply.json`
   exists, the two canonical-manifest checks (old id present, new id absent) are skipped: `finish` checks
   the opposite. These refusals, a database variable and an aborted succession exit non-zero without
   writing anything, including no failure receipt.

4. Steps, in this order. Each completed step writes `step-<name>.json` (exclusive create; schema
   `nhms.model_succession.step_receipt.v1`; fields `succession_id`, `step`, `outcome = completed`,
   `generated_at`, `operator_id`, `host`, `git_commit`, and the step's own facts). A step whose step receipt
   exists is skipped, so running the same command again resumes. A step refuses, naming the missing path,
   when the step receipt before it is missing. The first two steps run while the scheduler is running; the
   timer is stopped only for the steps that change what the scheduler reads.
   1. `copyback`: for every new row of the plan (read from the new-rows registry), the package directory of
      its `model_package_uri` is copied from the provider store root to the same key under
      `OBJECT_STORE_ROOT`. A symlink or a non-regular file in the source is a refusal. Copy into a sibling
      temporary directory (a leftover one from a killed run is removed first), then rename; file modes are
      not preserved (the compute store does not support it); a missing parent is created. A destination
      that exists is compared file by file (names and sha256): identical is `already_present`; different is
      a refusal and nothing is overwritten. After the copy the two trees are compared the same way. The
      receipt lists, per package, source, destination, file count, bytes and outcome.
   2. `preflight`: the clone tool's dry-run and the publish tool's dry-run, so that their refusals surface
      before the scheduler is stopped.
      - Clone: `scripts/node22_clone_direct_grid_cutover_states.py` is called as
        `build_parser().parse_args(argv)`, `enforce_mode_flags`, `args.dry_run = not args.apply`,
        `dispatch(args)`, with `--transfer-mode recalibration`, `--object-store-root OBJECT_STORE_ROOT`, both
        state indexes, `--variant-registry <canonical manifest>`, `--baseline-registry <new-rows registry>`,
        one `--pairs a:b,c:d` in plan order, the cutover time and `--receipt
        <succession dir>/clone-dry-run.json`. Dry-run success is `invocation_outcome = complete`. It is
        skipped when `clone-dry-run.json` exists with `dry_run = true`, `invocation_outcome = complete` and
        the plan's pairs and cutover time (an existing one that is anything else is a refusal).
      - Publish: `publish_merged_scheduler_registry` with one replace per pair and the succession id, which
        writes `publish-dry-run.json`; an existing one is reused only when its `operations`, succession ids,
        `dry_run = true` and `outcome = planned` match the plan, otherwise refusal.
   3. `begin`: exclusive-creates `timer-before-stop.json` recording whether
      `nhms-compute-scheduler.timer` was active (a file that exists is read, never re-derived), stops the
      timer (a resumed `begin` issues the `stop` again, which is harmless), then waits until
      `nhms-compute-scheduler.service` is not running (poll;
      `--pass-wait-seconds`, default 14400: a healthy pass has been measured at 193 minutes). It never stops
      or kills the service. Timeout is a failure; the same command resumes the wait.
   4. `clone`: the clone tool with `--apply` and `--receipt <succession dir>/clone-apply.json`. Success is
      `invocation_outcome = complete`, `cloned_pair_count = declared_pair_count`, `dry_run = false`.
      `step-clone.json` records the path and sha256 of `clone-apply.json`. A `clone-apply.json` that exists
      and is successful while the step receipt is missing (the run died in between) is validated against
      the plan (`cutover_time`, the `(source_model_id, target_model_id)` list, both index paths) and the
      step receipt is written without calling the tool again. One that exists and is not successful
      (`aborted`) is a hard stop: the tool does not retry the clone and points to the runbook's manual
      procedure. A clone apply that raised without leaving `clone-apply.json` wrote no row and is retried.
   5. `publish`: requires `step-clone.json`. The publish tool's apply. `step-publish.json` records the path
      and sha256 of `publish-apply.json`. A successful `publish-apply.json` without the step receipt is
      validated against the plan (`operations`, `succession_id`) and adopted the same way. Hard stops,
      pointing to the runbook: a `publish-apply-failed-*.json` with `outcome = inconsistent`, and a publish
      that is in effect without its receipt (both manifests equal, all new ids present, no old id, no
      `publish-apply.json`). `refused` and `rolled_back` are ordinary failures
      and are retried on resume.
   6. `refresh`: requires `step-publish.json`. Runs `systemctl --user start
      nhms-scheduler-file-provider-refresh.service` blocking, with a timeout of 7500 s (the unit's
      `TimeoutStartSec` is 7200); a non-zero exit or the timeout is a failure. Success is
      `<refresh receipt root>/latest.json` with `started_at` later than the step start, `outcome =
      published`, and `registry_classification.refused.total`, `.added.total`, `.removed.total`,
      `.package_changed.total` all 0 (a missing `registry_classification` is a failure). The unit's start
      condition can skip the run silently, so a zero exit of the start alone is not success.
   7. `finish`: requires all step receipts; requires the canonical manifest and the mirror to be
      byte-identical, to contain every new id and none of the old ids; then starts the timer if
      `timer-before-stop.json` says it was active (otherwise leaves it and says so). The step receipt and the
      final report carry `timer_action` (`started` / `left_stopped_was_inactive_at_begin`).

5. Unit state. A unit is "not running" when `systemctl --user is-active <unit>` prints `inactive` or
   `failed`; `active`, `activating`, `deactivating`, `reloading` are running; any other output or no output
   is a refusal. A non-zero exit of `is-active` is not an error. Before `clone`, `publish`, `refresh` and
   `finish` do anything, the tool checks that the timer and the scheduler service are both not running; if
   either is running it refuses: somebody started the scheduler during the succession.

6. Failure of any step in an apply: the timer is NOT started by the tool. It exits non-zero, prints, and
   writes `succession-failed-<UTC stamp>.json`: the step, the reason, which receipts exist, the timer and
   service state observed at that moment (not an assumed one), whether this tool stopped the timer, and the
   ways on: run the same command again after fixing the cause (completed steps are skipped), or `--abort`;
   for a hard stop, the runbook's manual procedure. A failure in `copyback` or `preflight` happens before
   the timer was touched and says so. A run that is killed leaves no failure receipt; the same command
   resumes.

7. `--abort --confirm-timer-start`: starts the timer if `timer-before-stop.json` says it was active, writes
   `abort-<UTC stamp>.json` (which steps had completed, the timer action taken, and what that state means:
   before `publish` the scheduler keeps running the old models, and any clone rows already written stay in
   both state indexes, where the scheduler uses the earliest clone row of a model as its cutover time, so a
   later succession of the same new id with a later cutover time would still take effect at this one; after
   `publish` the new models are live and the remaining steps must be finished by hand from the runbook). A
   succession that has an abort receipt is closed: any later run with that id refuses. `--abort` without
   the confirmation flag only reports.

8. Dry-run (no `--apply`): changes no file, writes no receipt, leaves no temporary file behind, and issues
   only `is-active` queries. It runs the checks of 3 and reports, per step, what the apply would do: per
   package whether it would be copied, is already present, or differs; the clone dry-run (receipt written
   to a private temporary directory and deleted) and the publish dry-run (called without a succession id,
   so it writes nothing) when the packages are already on the compute store, otherwise `needs copyback`;
   the timer and service state now; that refresh and finish are not predicted. Before the packages are on
   the compute store the dry-run cannot run the clone gate or the publisher's package checks; the apply
   runs them in `preflight`, still before the timer is stopped. The report is JSON on stdout (and
   `--output`).

9. `systemctl` is run as `<systemctl> --user ...`, the binary taken from `NHMS_MODEL_SUCCESSION_SYSTEMCTL`
   (default `/usr/bin/systemctl`), so tests use a fake.

10. Runbooks: `recalibration-and-archive.md` section 5.7.1 (heading unchanged) says to use this command
    (dry-run, read the report, `--apply` run detached with `setsid nohup`), keeps the manual per-step
    commands as the fallback for a stopped succession, and explains failure, hard stops and `--abort`,
    including the clone-row consequence of an abort. It states that the scheduler stall probe reports
    `timer_stopped` while the succession holds the timer, which is expected, and that this tool is the first
    to stop and start the scheduler timer, which `scripts/install_node22_scheduler_file_provider_refresh.sh`
    treats as protected (that installer only asserts it leaves the timer unchanged). `service-bringup.md`
    and `gateway-and-services.md` get a pointer where they describe stopping the timer for a succession.
    Added prose does not spell the publish tool's dotted module name (a guard counts those lines); the new
    command gets a guard of the same shape in `tests/test_node22_entrypoint_invariant.py`.

## Must preserve

- The clone tool, the publish tool, the provision step and the provider refresh keep their behaviour, receipt
  names, schemas and fields. This change adds no option to them.
- The tool never stops, kills or restarts `nhms-compute-scheduler.service`, the gateway or the compute API,
  never enables, disables or masks a unit, and writes nothing under any systemd directory.
- It reads no database variable and opens no database connection.
- `model_id` immutability is not relaxed; rows are replaced only through the publish tool.
- Python 3.11 and 3.12.

## Out of scope

Cold start without a clone (part D). Adding or removing a basin (part E). The node-27 provision step. A
post-restart check of the first scheduler pass (needs the database on node-27). Creating the shared receipt
root or changing its permissions.

## Required evidence

Tests use temporary directories, a fake `systemctl` (as `tests/scheduler_refresh_installer_harness.py`
does), the real clone and publish code against fake packages and state rows where the existing suites of
those tools show how, and a fake refresh unit that writes a `latest.json` built by
`scripts.scheduler_refresh.receipt._receipt` with a real `registry_classification`.

- a full apply runs the seven steps in order, leaves `plan.json`, `timer-before-stop.json`, the seven step
  receipts and the clone and publish receipts, both manifests equal with the new ids, both state indexes
  with the clone rows, and the timer started; the recorded `systemctl` calls are exactly: `is-active`
  queries, one `stop` of the timer, one `start` of the refresh service, one `start` of the timer; no
  `systemctl` call other than `is-active` happens before `begin`.
- each step refuses when the previous step receipt is missing, naming the path; `publish` without
  `step-clone.json` (the reverse order) is refused and neither manifest changes.
- every refusal of item 3 has a case, and nothing is written.
- `begin`: the service never stops running (timeout) -> failure, and running the command again completes
  with the timer started at the end; timer already inactive at begin -> `finish` does not start it; a
  service whose `is-active` prints `failed` counts as not running; unknown output is a refusal.
- timer or service found running before `clone`, `publish`, `refresh`, `finish` -> refusal, nothing written
  by that step, and the failure receipt records the observed state.
- `copyback`: fresh copy; identical destination skipped; differing destination refused and untouched; a
  symlink in the source refused; a leftover temporary directory removed; a copy interrupted half-way leaves
  no partial destination.
- resume: `clone-dry-run.json` present without later receipts; a successful `clone-apply.json` without
  `step-clone.json`; a successful `publish-apply.json` without `step-publish.json`; each completes without
  calling the finished tool again.
- hard stops: an `aborted` `clone-apply.json`; an `inconsistent` publish failure receipt.
- `refresh`: the start returns zero but `latest.json` is not newer; `outcome` not `published`; a non-zero
  `refused`, `added`, `removed` or `package_changed` total; `registry_classification` missing; start exits
  non-zero -> failure each.
- failure of each step leaves the timer as it was at that moment (stopped from `begin` on), a
  `succession-failed-*.json`, a non-zero exit; re-running the same command after the cause is removed
  completes and skips the finished steps.
- `--abort` without confirmation changes nothing; with it starts the timer and closes the succession.
- a dry-run changes no file under any root, leaves no temporary file, and its `systemctl` calls are only
  `is-active`.
- a command line that differs from `plan.json` (pairs, cutover time) is refused.
- a database variable in the environment is a refusal.
