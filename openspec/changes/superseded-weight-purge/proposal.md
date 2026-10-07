# A node-27 tool deletes the interpolation weights of superseded models

Issue: #2699 (part 3 of 3). Fixture level: **expanded**.
Risk packs: **Production database write (node-27)**, **Destructive delete**, **IO budget (degraded RAID link)**.

## Why

Every Direct Grid generation registers its own `core.model_instance`, its own `met.met_station` rows and its
own `met.interp_weight` rows; nothing removes the previous generation's. Owner decision (2026-10-07): only the
latest displayable generation is kept; superseded generations and the legacy `basins_*_shud` models are
removed, not deactivated.

Measured read-only on node-27 (2026-10-07), `met.interp_weight` 857,145 rows / 838 MB:

| class | models | weight rows |
|---|---|---|
| current (model of the latest displayable forecast run of a basin version and source) | 132 | 173,556 |
| superseded Direct Grid, a run inside 30 days | 24 | 26,604 |
| superseded Direct Grid, no run inside 30 days | 46 | 58,380 |
| legacy `basins_*_shud` (last run 2026-07-04 or never; 11 rows still `active`) | 17 | 547,572 |

The 132 current models are exactly the 132 `dg_*` ids of the canonical scheduler manifest.

Station rows are **not** deleted by this change (owner decision, same day): `met.forcing_station_timeseries`
(37 GB, 27 chunks) and `…_legacy` (75 GB, 4 chunks) reference `met.met_station` with no index leading on the
station column, so every deleted station row costs a scan of both tables. That is deferred until the legacy
table is dropped (#1993) and the RAID link is repaired. `met.interp_weight` has no inbound foreign key and
is deleted by `model_id` through `interp_weight (model_id, station_id, variable, lower(source_id))`.

## What changes

1. New entry point `scripts/node27_purge_superseded_weights.py` (one file, under 1000 lines). It imports
   `packages.common.succession_receipt` for exclusive-create receipt files and nothing from
   `scripts/basin_retirement/` (those modules' CI selector rules are pinned to exactly the retirement suites,
   `tests/test_select_ci_tests.py` ~:14740); the few helpers it needs (UTC stamp, directory fsync, connection)
   are written in the file. Arguments: `--operator-id`, `--reason` (non-empty), `--env-file` (default
   `infra/env/node27-ingest.env` of the checkout), `--receipt-root` (default
   `<OBJECT_STORE_ROOT>/scheduler/weight-purge`), `--min-idle-days` (default 30; below 21, the production
   forcing retention, is refused), `--pause-seconds` (default 2.0, between models), `--max-models` (optional
   cap per run), `--apply`. `DATABASE_URL` and `OBJECT_STORE_ROOT` come from the environment; `DATABASE_URL`
   must equal the single unquoted `DATABASE_URL=` line of the env file (the tool deletes: it must be bound to
   the ingest database, as the retirement tool binds), otherwise refused before connecting.
2. Protection, evaluated in SQL with the server clock (`now() - make_interval(days => %s)`), once for the
   whole table and again for each model inside its transaction. A model's weights are deleted only when
   **all** hold:
   - it is not the model of the latest displayable forecast run (`run_type = 'forecast'`, status
     `succeeded` / `parsed` / `published`, non-null `cycle_time`; newest `cycle_time`, then highest `run_id`)
     of any `(basin_version_id, lower(source_id))`;
   - it is not a `model_id` of the canonical manifest
     `<OBJECT_STORE_ROOT>/scheduler/registry/manifest-last.json` (`models[*].model_id`). The manifest is read
     before classification and re-read before every model's transaction (node-22 rewrites it). Missing,
     unreadable, no `models` list, or any row without a non-empty string `model_id` → refused (whole run, or
     stop before that model);
   - no `hydro.hydro_run` row of it (any type, any status) has `cycle_time`, `start_time`, `created_at` or
     `updated_at` inside the window (NULLs do not count as inside);
   - no `met.forcing_version` row of it has `created_at` inside the window (forcing and weights land before
     the run row exists);
   - none of its `met.interp_weight` rows has `created_at` inside the window (weights rebuilt recently);
   - its `core.model_instance.created_at` is outside the window.
   The rule does not look at the id prefix, `active_flag` or `lifecycle_state`. Classes reported, first match
   wins: `current`, `in_manifest`, `recent_run`, `recent_forcing`, `recent_weights`, `recently_created`,
   `purgeable`.
3. Dry-run (no `--apply`): one connection with `set_session(readonly=True)`, no file or directory created;
   prints and returns the per-class model and row counts, the purgeable models with their row counts, the
   total rows and the largest single model (the write volume of an apply). It also prints `SHOW server_version`.
4. Apply: an exclusive non-blocking `fcntl.flock` on `<env-file>.weight-purge-lock` (local disk; held → "another
   instance", lock error → "cannot take the lock"); run directory `<receipt-root>/purge-<utc stamp>/`. Per
   purgeable model, ordered by `model_id`, one READ COMMITTED transaction:
   1. `SET LOCAL lock_timeout = '10s'`;
   2. for every distinct `(source_id, grid_id)` of the model's rows, ordered, the advisory lock the weight
      writers take: `SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))` bound to the Python string
      `f"met.interp_weight:{source_id}\x1f{grid_id}\x1f{model_id}"`, as `workers/forcing_producer/store.py`
      (~:339-341) and `packages/common/forcing_domain_handoff_apply.py` (~:1096) build it; a test builds the
      key for one sample through all three code paths' formats and compares them;
   3. the protection rule for this model, now under the locks; not purgeable any more → rollback, recorded as
      `skipped` with the class, continue with the next model;
   4. backup and delete in **one statement**, so the backup is exactly the deleted rows:
      `COPY (DELETE FROM met.interp_weight WHERE model_id = <literal> AND (source_id, grid_id) IN (<the
      locked pairs, as literals>) RETURNING <explicit column list>) TO STDOUT WITH CSV HEADER` (only the
      locked scopes: rows of a scope a writer added meanwhile survive and trip the count check below) through `copy_expert` into `weights-<model_id>.csv` (`O_EXCL`, mode 0600).
      `COPY` takes no bind parameters: `model_id` must match `^[A-Za-z0-9_.-]{1,128}$` (else the model is a
      failure, nothing sent) and is rendered with `psycopg2.sql.Literal`. The column list is a constant of the
      tool, in table order;
   5. `SELECT count(*) … WHERE model_id = %s` must be 0 and the CSV must hold at least one data row,
      otherwise rollback and failure; file and directory fsynced; then commit. The backup is removed only
      when the commit call was never reached; a commit that raises leaves the backup and reports the outcome
      as unknown.
   Any database error (including `lock_timeout`) or failure on a model stops the run: earlier models stay
   purged, later ones untouched. Then `--pause-seconds` of sleep before the next model.
5. Receipts, all exclusive-create through the shared writer, never rewritten: `model-<nnnn>.json` per model
   right after its commit or skip (`{model_id, status, rows, backup, sha256, class}`), and at the end exactly
   one of `purge-receipt.json` (schema `nhms.weight_purge.receipt.v1`: operator, reason, thresholds, manifest
   sha256 at start, classes, totals, the models not reached because of `--max-models`) or `purge-failed.json`
   (the failing model, the error, the models done). A rerun starts a new run directory; purged models are no
   longer candidates.
6. Never written: `met.met_station`, `core.model_instance`, `hydro.*`, `met.forcing_version`, any timeseries
   table. No `VACUUM`, `ANALYZE`, `TRUNCATE`.
7. Runbook: `docs/runbooks/production-ops/operating-scope.md` gets a section: dry-run and apply as single
   lines of the form `cd /home/nwm/NWM && … uv run --no-sync python -m scripts.node27_purge_superseded_weights …`
   (the node-22 entrypoint guard reads `uv run` lines without `/home/nwm/` as node-22 ones), reading the
   receipts, restoring one model — first delete whatever rows the model has again, then client-side
   `\copy met.interp_weight (<column list>) FROM '<backup>' WITH CSV HEADER` (server-side `COPY FROM
   '<file>'` cannot see the NFS path from the container) — what is deferred (the station rows) and why, and
   that a full display-coverage refresh will show zero station coverage for old runs of purged models, whose
   series retention has removed anyway.

## Must preserve

- No existing script or test changes behaviour; the tool opens the database only after its file-based
  refusals; Python 3.11 and 3.12; no file over 1000 lines.
- `model_id` immutability and every `core.model_instance` row.

## Out of scope

Deleting `met.met_station` rows; deleting `core.model_instance`, runs or forcing versions; scheduling the
tool; registering the activation flip hook; any production `--apply` (owner approval on the dry-run numbers
first).

## Evidence

Local, `tests/test_node27_purge_superseded_weights.py`: its own small fake database (recording
connection/cursor with committed state; no import from the retirement helpers), real files in `tmp_path`:

- classification: one model per class; a legacy `active` model with an old run → purgeable; current for one
  source only → protected; only a `failed` run, only a hindcast run, only an `updated_at` inside the window →
  protected; a forcing version inside the window without any run → protected; weights created yesterday →
  protected; a model created yesterday with no run → protected.
- manifest missing / unreadable / no `models` / a row without `model_id` → refused before the database is
  opened; `DATABASE_URL` differing from the env file → refused; `--min-idle-days 20` → refused.
- dry-run: the connection is read-only, nothing created under the receipt root, no commit, no statement
  containing `DELETE` sent; totals and largest model reported.
- apply: advisory locks taken before the per-model check, in the writers' spelling (`source_id` as stored, not lowercased); backups hold exactly the rows, mode 0600; purgeable models empty, all others
  untouched; per-model receipts and the summary; pause called between models; `--max-models 1` stops after
  one and the summary names the rest.
- a model that turns protected between classification and its transaction (new run; manifest rewritten to
  include it) → skipped with the class, rows stay, run continues.
- rows remain after the delete (a writer slipped in) → rollback, backup removed, `purge-failed.json`, later
  models untouched; `lock_timeout` on the third model → the first two stay purged with their receipts.
- a `model_id` outside the allowed pattern → failure, no statement sent for it.
- second instance while the lock is held → refused.
- commit raises → backup kept, failure receipt says the outcome is unknown.

Real database, `tests/test_node27_purge_superseded_weights_integration.py` (`pytest.mark.integration`,
migrations from zero, CI real-DB lane): seeded weights of a purgeable and a protected model; apply deletes
only the purgeable one and the CSV equals the rows that were there; restoring the CSV with `COPY … (<column
list>) FROM STDIN WITH CSV HEADER` gives back identical rows (including `weight_id` and NULLs); the tool's
column-list constant equals the table's columns in `information_schema` order (a later `ADD COLUMN` fails
this test); a session holding the writers' advisory lock makes the tool hit `lock_timeout` and delete nothing.

node-27: dry-run only (read-only); its output is the receipt and goes to the owner for approval of an apply.
