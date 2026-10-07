# A node-27 tool retires a basin after its node-22 removal

Issue: #2757 (part E2 of #2741 / #1815; its node-22 half, `--kind remove_basin`, is merged: PR #2760).
Fixture level: **expanded**. Risk packs: **Production database write (node-27)**, **Ordering fail-closed**,
**Partial-failure recovery**.

## Why

After the node-22 removal, retiring a basin is four manual steps on node-27 (`operating-scope.md` 7.2): mark
its runs `superseded`, add it to `AUTOPIPE_EXCLUDE_BASINS`, deactivate its `core.model_instance` rows, move
its Basins directory. Each retirement so far needed rework: model rows found by `model_id like` missed the
hashed `dg_*` rows; a deactivation done three seconds before the exclusion landed was reverted by the next
autopipe round; the baseline row was forgotten for 18 days; the set of run statuses that were superseded
differed between retirements. Nothing enforces the order.

Owner decisions (2026-10-06): the tool edits `AUTOPIPE_EXCLUDE_BASINS` in node-27's ingest env file itself;
it never moves a Basins directory; runs in `succeeded`, `parsed` and `published` are all superseded; after
the env edit the tool does not touch the autopipe timer but waits for one autopipe round that started after
the edit before it deactivates, and for one more full round before it verifies; the dry-run of the supersede
step runs the real statements and rolls back.

## What changes

1. New entry point `scripts/node27_retire_basin.py` with its logic in a package `scripts/basin_retirement/`
   (no `__init__.py`; every file under 1000 lines). Arguments: `--succession-id`, `--basin-version-id`
   (exact; one per run; must match `[A-Za-z0-9._-]{1,120}` because it names a directory), `--operator-id`,
   `--reason` (non-empty; recorded and passed to the lifecycle call), `--env-file` (default
   `infra/env/node27-ingest.env` of the checkout), `--receipt-root` (default
   `<OBJECT_STORE_ROOT>/scheduler/succession`), `--autopipe-wait-seconds` (default 1800), `--apply`.
   `DATABASE_URL` and `OBJECT_STORE_ROOT` come from the process environment.

2. Refusals before anything is read from the database or written, in both modes:
   - `NHMS_AUTH_MODE` or `AUTH_BACKEND` is set (the lifecycle call must run with the ingest environment
     only); the message names the `env -u` form.
   - `DATABASE_URL` of the process differs from the env file's: the env file must hold exactly one line
     `DATABASE_URL=<value>` (unquoted, whole line) and the value must equal the process's. This binds the
     database the tool writes to the env file it edits, so a scratch database cannot be paired with the
     production exclusion list.
   - Another instance is running: the tool holds an exclusive non-blocking `flock` on
     `<env file>.retire-lock` (created 0600 if absent) for its whole run, in both modes. The lock and the env
     backups sit beside the env file on purpose: `infra/env/*` is git-ignored, so they do not dirty the
     checkout, and the backup holds `DATABASE_URL`, which must not go to the group-readable receipt root.

3. One succession may retire several basins (node-22 `remove_basin` takes the models of several). The tool
   is run once per basin version; everything it writes for one basin version is in
   `<receipt-root>/<succession-id>/retire-<basin-version-id>/`: the step receipts `retire-<step>.json`
   (schema `nhms.basin_retirement.step_receipt.v1`, written with the shared receipt writer of
   `packages/common/succession_receipt.py`), failure receipts `retire-failed-<stamp>.json`, and
   `hydro-run-backup.csv`. A step with a receipt is skipped on a rerun. Order: `exclude`, `supersede`,
   `deactivate`, `verify`.

4. Preconditions before any step, in both modes:
   - node-22 `step-finish.json` of the succession exists with `outcome` `completed`, and its `plan.json` has
     `kind` `remove_basin`. (A removal finished by hand after a node-22 hard stop has no such receipt; the
     runbook says the node-27 steps are then manual too.)
   - `--basin-version-id` is a row of `core.basin_version`; its `basin_id` is read from there.
   - at least one `removes` id of the plan is a `core.model_instance` row of that basin version.
   - the canonical manifest (`<OBJECT_STORE_ROOT>/scheduler/registry/manifest-last.json`) has no row of the
     basin at all: no row whose `basin_id` equals the basin version's `basin_id`, compared both exactly and
     through the autopipeline's key normalisation, and no row whose `basin_version_id` equals the argument.
     The exclusion list works per basin, so a basin that still has any version in the manifest is not
     retired by this tool (changing a basin's mesh is not a retirement; `operating-scope.md` 7.2).
   In a dry-run each failed precondition is a `would_be_refused` entry and the remaining read-only checks
   still run; a dry-run does not report the missing receipts of its own earlier steps.

5. `exclude`:
   - The basin key is `basin_id` of the basin version through `_basin_key_set` / `_slug_id` of
     `scripts/node27_autopipeline.py` (imported, not copied); the current entries are compared through the
     same function.
   - The env file must be a regular file, not a symlink, mode exactly 0600, owned by the effective user.
     Exactly one line must match `^AUTOPIPE_EXCLUDE_BASINS=[A-Za-z0-9_,.-]*$`; the name
     `AUTOPIPE_EXCLUDE_BASINS` must occur nowhere else in the file (not in a comment, an `export`, a
     `declare`, a second assignment on a line, an `unset`); and the line before the assignment must not end
     with a backslash. Otherwise the step fails without writing. (The file is
     sourced by bash: a trailing comment, a quoted value, an `export` line or `+=` would make bash read
     something other than what the tool wrote.)
   - If the key is absent: backup `<env file>.bak-<succession-id>-<basin-version-id>` (`O_EXCL`, mode 0600),
     then the new content (that one line with `,<key>` appended, or `<key>` when the list is empty; every
     other byte unchanged) written to a temporary file in the same directory with mode 0600, fsynced,
     renamed over the env file, directory fsynced. If the key is already present nothing is written and the
     receipt says so. A backup of that name that already exists while the key is absent (an earlier attempt
     stopped before the rename) is kept when its bytes equal the env file's and is otherwise a failure; it
     is never overwritten.
   - Then the autopipe wait (item 6) with the reference taken right after the rename, or at the start of the
     step when the key was already present.
   - The receipt records the key, whether the file was changed, the backup path, sha256 before and after,
     and the autopipe round it waited for (start, exit, exit status).

6. The autopipe wait, used by `exclude` and `verify`. The reference is a `CLOCK_MONOTONIC` reading in
   microseconds taken by the tool in the same process. The unit is read with `systemctl --user show
   nhms-node27-autopipe.service -p LoadState,ActiveState,ExecMainStartTimestampMonotonic,
   ExecMainExitTimestampMonotonic,ExecMainCode,ExecMainStatus` (binary overridable through an environment
   variable, as the node-22 tool does). A `LoadState` other than `loaded` fails the step at once. The unit
   is a oneshot service, `activating` while it runs.
   Production rounds take longer than the timer period, so the next round starts in the same second the
   previous one ends (measured 2026-10-07: rounds of about 13 minutes, back to back); the unit is then
   never seen at rest and the exit status of a finished round is overwritten at once. The wait therefore
   tracks rounds by their start timestamp. A round is a candidate once its start is later than the
   reference. A candidate has ended, and qualifies, in one of two ways:
   - seen at rest: the start timestamp is still the candidate's, the unit is not `activating`, the exit
     timestamp is not earlier than the start, `ExecMainCode` is 1 (exited by itself; systemd prints the
     numeric `CLD_EXITED`) and `ExecMainStatus` is 0 or 1. Status 1 is a round in which some basin's run or
     seed failed: it read the list in full and counts. Status 2 (blocked at its bootstrap or preflight) and
     a round ended by a signal prove nothing: that candidate is dropped and the wait goes on with the next
     round.
   - followed: a later poll shows a start timestamp later than the candidate's, and the candidate was never
     seen at rest. A oneshot unit does not overlap itself, so the candidate has ended; its exit status is
     not known and is recorded as such. This counts: the tool polls without a gap from the reference on, so
     a round that is never seen at rest ran until the next one started, and a blocked round exits within
     seconds and is then at rest until the timer fires again. (Residual, recorded: a blocked round followed
     by a new start inside one poll interval would be accepted.)
   After a round qualifies the tool probes the autopipe lock file with a shared non-blocking `fcntl.flock`
   (the cron script holds it with `flock(1)`, i.e. `flock(2)`; `fcntl.lockf` would not see it), released at
   once. The path is, in this order: the value of a line `AUTOPIPE_LOCK_PATH=<value>` of the env file,
   `NODE27_AUTOPIPE_LOCK_PATH` of the process environment, `/tmp/autopipe.cron.lock`; the file is opened
   read-only and never created, and a missing file counts as not held. When the candidate was seen at rest
   and the lock is held, a round outside systemd is still running with the list it read earlier and the
   systemd round only skipped: the wait continues with the next round. When the candidate qualified by
   being followed, the lock is held by the round that followed it, and the probe is skipped. (Residual: a
   manual round that ends between a skipped systemd round and the probe lets `verify` accept the skipped
   round; no round with the old list is running then.)
   The tool polls (15 s), starts and stops nothing, and fails on an unreadable or unparsable answer. On
   timeout the step fails and says which round it was following; the rerun takes a new reference at its own
   start. The receipt records the round (start, how it ended, exit code and status or `unknown`) and the
   probed path. A dry-run reads the unit once, reports what it read, and does not wait.

7. `supersede` (requires `retire-exclude.json`): one transaction on a psycopg2 connection. The backup is
   `cursor.copy_expert` of `COPY (SELECT * FROM hydro.hydro_run WHERE basin_version_id = <literal> AND status
   IN ('succeeded','parsed','published') ORDER BY run_id) TO STDOUT WITH CSV HEADER` into
   `hydro-run-backup.csv` (`O_EXCL`, fsynced). `copy_expert` binds no parameters: the statement is rendered
   with `cursor.mogrify` (or `psycopg2.sql.Literal`), never by string formatting of the raw value. The
   transaction starts with `SET LOCAL lock_timeout = '10s'` (an ingest holding a row lock fails the step
   instead of hanging it). Then
   `UPDATE hydro.hydro_run SET status = 'superseded' WHERE basin_version_id = %s AND status IN
   ('succeeded','parsed','published') RETURNING run_id`; no other column is set. The set of returned
   `run_id` must equal the set of `run_id` in the backup (the two statements see separate snapshots, so equal
   counts are not enough), and afterwards no row of the basin version may be in one of the three statuses;
   otherwise rollback and failure. Apply commits after the backup is on disk. Whether the backup file is removed is decided by one fact only, whether the commit call was reached, never
   by the type of an exception: a failure before the commit call removes the backup file this run created;
   once the commit call has been made (returned, raised, or interrupted, and through every later statement
   up to and including the receipt write) the backup stays. Zero rows is a success with zero.
   A rerun that finds `hydro-run-backup.csv` already there (the commit happened and the receipt did not, or
   the process was killed before it could remove the file): with no row left in the three statuses it writes
   the receipt from the backup (row count, sha256) and changes nothing; with rows left in them it fails,
   names both facts, and the operator decides (the runbook says how: compare the CSV with the table, move
   the CSV aside, rerun).
   The dry-run runs the same statements in a transaction that is rolled back, writes the CSV to a temporary
   file under `TMPDIR` (never in the succession directory) that is removed, and reports the per-status counts.

8. `deactivate` (requires `retire-exclude.json` and `retire-supersede.json`): first re-read the env file and
   fail if the key is no longer in the list. The active rows are `SELECT model_id FROM core.model_instance
   WHERE basin_version_id = %s AND active_flag ORDER BY model_id` (exact match, never a pattern; baseline
   and `dg_*` alike). For every one, `preflight_model_operation(model_id, operation="deactivate",
   policy_decision=trusted_internal_policy_decision("models.deactivate", target_type="model_instance",
   target_id=model_id, actor_id="ops:<operator-id>", roles=("sys_admin",)), override_missing_active=True,
   reason=<reason>)` on `PsycopgModelRegistryStore`; a blocker on any row fails the step before any row is
   changed. Then `model_lifecycle_operation` with the same arguments, one row at a time, never
   `trusted_internal=True`. These calls return their outcome instead of raising: a row is done only when the
   returned status is the transition outcome `allowed` or `already_current`; any other status (`blocked`, an
   audit failure result, anything else) is a step failure at that row. The receipt lists each row with its
   preflight warnings and returned status. A failure after some rows leaves a failure receipt naming the
   rows done; the rerun acts on the rows still active. The dry-run runs the preflights only.

9. `verify` (requires `retire-deactivate.json`): the autopipe wait with the reference taken at the start of
   the step, then read-only: no active `core.model_instance` row of the basin version, no `hydro.hydro_run`
   row of it in `succeeded` / `parsed` / `published`, the key still in the env file. Any of these failing is
   a step failure that says what was reverted and why that happens (a round that read the old list, a forced
   ingest). The receipt records the counts and the autopipe round. It then prints what the tool does not do:
   the Basins directory, the static geojson filter, and that the exclusion entry must stay while run
   directories of the basin exist.

10. Never written: `core.basin`, `core.basin_version`, `updated_at` of a run, any Basins directory, any unit
    state. Without `--apply`: no file is changed, no receipt is written, no write transaction is committed
    (the registry store's preflight commits a read-only transaction of its own; that is not a write).

11. Runbook: `docs/runbooks/production-ops/operating-scope.md` 7 gets a section for the tool (the command,
    the order with the node-22 half, several basins in one succession, what stays manual, reading the
    receipts, that `DATABASE_URL` may be loaded by sourcing the env file or by cutting its line, the "backup exists and rows remain" case, reviving a basin by hand from the backups).
    `recalibration-and-archive.md` 5.7.4 gets a pointer to it.

Design: see `design.md`.

## Must preserve

- Every existing test passes unchanged; no existing script changes behaviour.
- The tool opens the database only after its file-based refusals.
- Python 3.11 and 3.12; no file over 1000 lines.

## Out of scope

Moving Basins directories; the static geojson; deleting rows of a retired basin; reviving a basin by tool;
`AUTOPIPE_EXCLUDE_MODEL_IDS`; any production `--apply`.

## Evidence

In `tests/test_node27_retire_basin_*.py`, with a fake database (recording connection and cursor with
`copy_expert`, committed state shared per test; the style of `tests/test_node27_oneshot_sql.py`), a fake
`systemctl`, a real env file and lock file in `tmp_path`, and a fake registry store for the lifecycle calls:

- full apply: the four receipts under `retire-<basin-version-id>/`; the env file gains the key, keeps mode
  0600 and every other byte; the env backup and the run CSV exist; runs of the three statuses are
  `superseded` with `updated_at` untouched and runs of other statuses and other basin versions untouched; all
  active rows of the basin version (baseline and `dg_*`) deactivated one by one through the store, inactive
  rows not touched; `core.basin` and `core.basin_version` never written.
- the SQL given to `copy_expert` contains the basin version as a rendered literal and no placeholder.
- two basin versions of one succession, one run each: each has its own directory, backup and receipts, and
  the second run is not skipped.
- preconditions, each refused before any write: missing node-22 finish receipt; finish receipt of another
  kind; `--basin-version-id` not in `core.basin_version`; no removed id of the plan is a row of the basin
  version; the manifest has a row with the basin version's id; the manifest has a row of another version of
  the same `basin_id`.
- `NHMS_AUTH_MODE` set; `AUTH_BACKEND` set; `DATABASE_URL` differing from the env file's; a second instance
  while the lock is held: refused before anything.
- the replacement is atomic and backed up first: with the rename made to fail, the env file's bytes are
  unchanged, the backup exists and no temporary file is left.
- env file a symlink / mode 0644 / two assignment lines / a quoted value / a trailing comment / an extra
  `export AUTOPIPE_EXCLUDE_BASINS=` line / the name in a comment / a preceding line ending in a backslash: `exclude` fails, file unchanged.
- key already present (also in another spelling the normalisation equates): no write, the wait still runs.
- env backup already there with the key absent: same bytes, kept and the step goes on; different bytes,
  failure and no write.
- the wait: a `LoadState` that is not `loaded` fails at once; back-to-back rounds (the unit `activating` on
  every poll): the round in flight at the reference does not count, the next one is the candidate and it
  qualifies when a third start timestamp appears, with its status recorded as unknown and the lock probe
  skipped; a candidate seen at rest with status 2 is dropped and the following round qualifies;
  a round in flight at the reference does not count; a round with exit status 1 counts; a round with exit status 2 and a round ended by a signal do not
  count; a qualifying round while the autopipe lock is held (by `flock(2)` from another process) does not count
  and the next one does; the lock path is taken from the env file's `AUTOPIPE_LOCK_PATH` line when there is one; a round
  started after the reference and ended with status 0 counts; unparsable `systemctl` output fails; timeout
  fails the step and a rerun waits again without writing a second backup.
- `supersede` or `verify` without the receipt of the step before; `deactivate` without `retire-exclude.json`
  or `retire-supersede.json`: refused.
- `supersede`: zero rows succeeds; the transaction sets the lock timeout first; returned ids differing from
  the backup's (same count, another id) roll back, write no receipt and remove the backup; a database error
  on the update does the same; the commit call raising keeps the backup, writes no receipt, and the rerun
  takes the "backup present" branch; an interrupt or an error after the commit returned (closing the file,
  writing the receipt) keeps the backup; rerun with the backup present and no candidate row writes the receipt and
  changes nothing; rerun with the backup present and candidate rows fails.
- `deactivate`: the key gone from the env file fails before any row; a preflight that raises fails the
  step with no row changed; a preflight blocker on the second of
  three rows deactivates none; the operation returning `blocked` (and, separately, an audit-failure result)
  on the second row is a failure whose receipt names the first as done, and the rerun deactivates the
  remaining rows only; `already_current` counts as done.
- `verify`: an active row again; a run back in a candidate status; the key gone from the env file: each a
  failure naming it.
- dry-run: no file changed (env file, lock file aside, and the whole succession directory), no write
  transaction committed on any connection, the supersede statements executed and rolled back, preflights
  run, the unit read once without waiting, the report lists the four steps.
