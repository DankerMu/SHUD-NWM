## Context

Live facts behind the decisions below (node-27, 2026-09-25) are in proposal.md "Why". A few more that the decisions depend on:

- **The route smoke runs in-process.** `validate_readonly_db_boundary.py` drives the checkout's `apps.api.main:app` under the readonly DSN. So the smoke judges the code of the checkout it runs from, not the deployed `:8080`. The C2 rerun therefore has to run from the merged SHA (tasks §5).
- **Real success envelopes** (`apps/api/response_models/pipeline.py:18-35, 216-235`; `apps/api/routes/pipeline.py:1187-1204, 1801-1814`):
  - `pipeline_status`, `pipeline_stages` and `jobs` return `{request_id, status: "ok", data: <route data>, identity: {source, cycle_time: "<…>Z", run_id, model_id}}`;
  - `job_logs` returns the same with `identity.job_id`;
  - `latest_product` (`apps/api/routes/forecast.py:160-231`, `QhhLatestProductEnvelope`) has no `identity` block. Its `data` carries `source_id`, `cycle_time` (`…Z`), `run_id`, `model_id` and `basin_id`, and the route resolves `basin_id` to QHH when it is omitted.
- **The server already binds the identity.**
  - `_strict_pipeline_identity_or_none` resolves a complete strict tuple against `hydro.hydro_run`: 404 `PIPELINE_STRICT_IDENTITY_NOT_FOUND`, or 422 when the tuple is incomplete.
  - `job_log` returns 409 `PIPELINE_STRICT_IDENTITY_MISMATCH` when the job belongs to another identity.
  - The smoke's echo check is the client-side half of that contract. It catches a route that answers 2xx for a different identity than the one requested.
- **node-27 data:**
  - `hydro.hydro_run.source_id` is stored in mixed case (`IFS`, `gfs`);
  - `core.basin_version(basin_version_id, basin_id)` maps a run's `basin_version_id` to its basin;
  - on 2026-09-25 each source's newest cycle has 192 `published` runs, and `ops.pipeline_job` has 1348 logged jobs across 1213 runs.
- **Restart host facts:**
  - on node-27, `git rev-parse --show-toplevel` equals `readlink -f /home/nwm/NWM`, which is `/home/nwm/NWM`;
  - the unit's `ExecStart` ends in `exec /home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app …`, so the live master's `/proc/<pid>/cmdline` starts with the repo root. This equality is the condition under which the anchored pattern still finds this checkout's orphans.
  - the `bash -lc` wrapper is replaced by `exec`, and its own command line starts with `/bin/bash`.

## Goals / Non-Goals

**Goals:**
- An unattended `validate_readonly_db_boundary.py` run on node-27, with no hand-supplied tuple, reaches `PASS` whenever the routes and the boundary are healthy.
- A real contradiction still fails.
- The evidence names the exact field at fault.
- The canonical restart never signals a process outside its own checkout.

**Non-Goals:**
- Changing any route, envelope, OpenAPI document or frontend consumer. #2484's alternative, a route-side echo, is already there since #2420.
- The deny-write probe matrix.
- `two_node_e2e_evidence` behaviour.
- The yd deployment.
- `production-ops-readiness/spec.md:69` (the stale "systemd is out of scope" sentence, deferred by #2282).

## Decisions

### D1. The echo contract stays; it is judged by field semantics

An identity-bound route (`latest_product`, `jobs`, `pipeline_status`, `pipeline_stages`, `job_logs`) is judged in this order:

1. The request carried the complete strict identity (unchanged). Otherwise the route is `BLOCKED` before any request.
2. The response is 2xx (see D2 for non-2xx).
3. **Echo extraction.** The existing candidate order is kept: `body.identity`, `body.strict_identity`, `data.identity`, `data.strict_identity`, `data`, the nested keys, then list items. `source_id` still aliases to `source`.
   - The first candidate holding every required field wins.
   - If none holds them all, the candidate holding the **most** required fields wins, the earliest on a tie.
   - **No stitching.** Fields are never merged across candidates. A body whose fields are spread over several objects (e.g. `data.identity` has `source_id`, `data.item` has `cycle_time`/`run_id`) yields the single best candidate's partial identity plus MISSING blockers, never a stitched PASS.
   - `response_identity` is written to the evidence whenever the chosen candidate is non-empty.
   - Required fields: `source`, `cycle_time`, `run_id`, `model_id`, plus `job_id` for `job_logs`.
4. **Per-field comparison** against the requested strict identity:
   - `source`: case-insensitive, as today.
   - `cycle_time`: the same UTC **cycle hour**, through the shared helper (D3), which normalises both sides to `%Y%m%d%H` after UTC conversion. Forecast cycles are hourly (00/12Z), and this is the two-node lane's existing rule, so both lanes agree on what "same cycle" means. If either side does not parse, only exact string equality matches.
   - Others: exact after `strip()`.
   - The `job_logs` path fallback for the expected `job_id` stays.
5. **Verdict:**
   - no blocker → `PASS` (`display_read_route_succeeded`);
   - any missing field → `BLOCKED` (`display_read_route_response_identity_invalid`, `READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING` per missing field only);
   - any contradicting field → **`FAIL`** (`display_read_route_response_identity_mismatch`, `READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH` per field, with `expected` and `observed`). This mirrors the two-node lane, where incomplete evidence is a blocker and a mismatch is a finding. A 2xx answer for a different identity is a correctness failure, not missing evidence.
   - When both occur, `FAIL` wins.

The spec's PASS wording ("scoped to one strict identity") gains the echo-agreement clause, so the contract the code enforces is written down (spec delta).

**Rejected alternative:** per-route "binding fields" with no echo requirement (#2484's recommendation). It was designed against the pre-#2420 schema. Now every identity-bound route echoes all fields, so requiring the full echo is both satisfiable and stricter.

### D2. Non-2xx responses carry no identity-echo blockers

An error body has no identity to echo. For a non-2xx response the verdict comes only from the existing rules:
- `fixture_blocker_allowed` plus an error code in `ROUTE_FIXTURE_BLOCKER_ERROR_CODES` → `BLOCKED`;
- anything else → `FAIL`.

`error_code` and `error_message` are recorded, and `identity_blockers` is absent. `ROUTE_FIXTURE_BLOCKER_ERROR_CODES` is unchanged:
- `PIPELINE_STRICT_IDENTITY_NOT_FOUND` stays `BLOCKED`;
- 409 `PIPELINE_STRICT_IDENTITY_MISMATCH` stays `FAIL`. With D4, a 409 means a hand-supplied `--job-id` that belongs to another run.

### D3. One cycle-time helper (UTC cycle hour), shared

`_cycle_time_identity_matches` and `_normalized_cycle_time_identity` (`two_node_e2e_evidence.py:2388-2408`) move verbatim, `YYYYMMDDHH` form included, into a new `services/production_closure/identity_matching.py` as public `cycle_time_identity_matches` / `normalized_cycle_time_identity`.
- `two_node_e2e_evidence.py` imports them and binds its existing private names to them. Its call sites (`:1413`, `:2384`) and behaviour do not change, and `tests/test_two_node_e2e_evidence.py` passes unchanged.
- The readonly lane imports the public names. The readonly lane does not import the 4.5k-line two-node module.

### D4. Discovery returns a self-consistent tuple

The signature becomes `ReadonlyDbProbeAdapter.discover_display_identity(*, source: str | None = None, run_id: str | None = None)`, protocol in `readonly_db_types.py`. `_safe_discover_identity` passes `config.source` and `config.strict_run_id`. One `psycopg2` connection, read-only.

1. **Run.** It picks the newest `hydro.hydro_run` row, ordered as today (`updated_at DESC NULLS LAST, cycle_time DESC, run_id DESC`), with non-null `source_id`/`cycle_time`/`model_id`, and:
   - when `run_id` is configured, `r.run_id = %s`, with no status filter (the operator chose the run);
   - otherwise `r.status::text = ANY(%s)` over `list(QHH_LATEST_READY_RUN_STATUSES)`, imported from `packages/common/forecast_store.py:35` and not retyped. That is the latest-product selector's own gate (`:3824`). `services/production_closure` reaches `apps.api` only through `importlib` (`readonly_db_types.py:13`), so it does not import `apps/api/routes/hydro_display_constants.py`'s equal set;
   - when `source` is configured, `upper(r.source_id) = upper(%s)`.

   It selects `upper(r.source_id) AS source` (see Risks) and `bv.basin_id` via `LEFT JOIN core.basin_version bv ON bv.basin_version_id = r.basin_version_id`.
2. **Cycle fallback.** The `met.forecast_cycle` fallback (no run row) stays, filtered by the configured `source` the same way. It yields no `run_id`, so the strict routes are `BLOCKED` as today.
3. **Job.** Only when a `run_id` is known (discovered or configured): the newest `ops.pipeline_job` with `run_id = %s AND log_uri IS NOT NULL`, ordered as today. There is **no fallback to an unrelated job**. With no logged job for the run, `job_id` is absent and `job_logs` is `BLOCKED` with its existing reason.
4. **Overrides.** `_merged_identity` overrides still win field by field (unchanged). A hand-supplied `--job-id` for another run is therefore still a live 409 → `FAIL`, which is correct.

The discovered row's `basin_id` is kept in `display_identity`. `_json_ready` already renders the `cycle_time` datetime as ISO (`+00:00`). D1 makes that spelling irrelevant.

### D5. `latest_product` requests the run's basin

When the merged identity has a `basin_id`, the `latest_product` path is `_query_path("/api/v1/mvp/qhh/latest-product", {**strict_identity, "basin_id": basin_id})`. `strict_identity` (the comparison target) stays the four fields. Without a `basin_id`, the path is unchanged, so the route defaults to QHH as before.

### D6. Lane statuses in the summary

A single helper returns `{"deny_write": …, "read_routes": …}`. `summary["lane_statuses"]` is **always** written by the single-run live summary, the single-run simulated summary and the merged summary (`readonly_db_merge.py`). `_blocked_summary` (no probes ran) **omits** it.
- **`deny_write`** covers the role, the permission probes and the manual-action probes:
  - `FAIL` if `role_type == "writer_or_mutating"` or any of those items is `FAIL`;
  - else `BLOCKED` if any is `BLOCKED`;
  - else `PASS`.
- **`read_routes`** covers `route_smoke` with the same `FAIL` > `BLOCKED` > `PASS` order.

`_overall_status` is rewritten as the worst of the two lane statuses. Its result is identical to today's for every input (I3).

The two paths that are stricter than worst-of-lanes stay unchanged, and the spec says so:
- the **simulated** path still forces `BLOCKED` (`READONLY_DB_VALIDATION_SIMULATED`, `readonly_db_validation.py:189-201`) while its lanes may read `PASS`;
- the **merge** refuses a source bundle that is not `PASS` before any merged summary is built: `_validate_merge_source_live_provenance` raises `READONLY_DB_MERGE_SOURCE_NOT_PASS` (`readonly_db_merge.py:420-430`), and the CLI exits 1. The later `:538` blocker is unreachable from the CLI. This is kept unchanged (merge rules are Must-preserve) and pinned by a CLI test. The merged summary's `status` is `BLOCKED` on any merge blocker (`:631`). Its `lane_statuses` is computed from the merged items (`:583-595`), and a direct `_merged_readonly_db_summary` test pins that a non-PASS item shows in `lane_statuses` next to a `BLOCKED` status. *(Corrected during implementation: the fixture first said such a bundle reaches the merged summary with exit 2.)*

### D7. Fixtures are real envelopes

`tests/test_readonly_db_validation.py`'s route requester builds success bodies in the shapes from Context, and one test validates each fixture body through its envelope model:
- `PipelineStatusEnvelope`, `PipelineStageListEnvelope` and `PipelineJobPageEnvelope` (with one real-shaped job item and with `items: []`);
- `JobLogsEnvelope`;
- `QhhLatestProductEnvelope`.

So a response-schema change breaks the fixture instead of letting it drift. `cycle_time` in fixture echoes uses the `Z` spelling, and the requested identity uses `+00:00` (the live pairing). The `data.identity` query-echo fixture is deleted.

### D8. Restart pattern anchored to this checkout

In `scripts/ops/start-display-api.sh`:

```bash
REPO_ROOT_RE=$(printf '%s' "$REPO_ROOT" | sed -e 's/[][\.*^$+?(){}|]/\\&/g')
UVICORN_PATTERN="^${REPO_ROOT_RE}/\.venv/bin/python -m uvicorn apps\.api\.main:app"
```

- Every `pgrep` becomes `pgrep -f -- "$UVICORN_PATTERN"`. The escape set is the full ERE metacharacter set, not #2282's `][\.*^$` subset, because `pgrep` uses ERE.
- Both branches keep the sweep. In the systemd branch it now only ever finds this checkout's orphans, such as a stale detached launch left from before the unit, which is #597's original purpose. The alternative, skipping the sweep under systemd, would leave the legacy branch killing other checkouts and drop orphan cleanup.
- The header comment states the anchor and its condition: the unit and the detached launch both exec `$REPO_ROOT/.venv/bin/python`.
- The node-27 inspection command at `docs/runbooks/production-ops/gateway-and-services.md:718` is anchored the same way, with `/home/nwm/NWM`. The node-22 command (`:123`) and historical receipts are not changed.

**Harness** (`tests/test_two_node_docker_runtime.py`):
- **Anchor-sensitive fake `pgrep`.** It reads a process table file (`pid<TAB>cmdline`) and prints each pid whose **cmdline field alone** (not the whole line, or `^` never matches) matches `grep -E -e "$pattern"`, **and** that is still alive and not a zombie (`kill -0` plus `ps -o stat= -p <pid>` not starting with `Z`). It accepts `-f` and `--`. Sleepers are pytest children, and a TERMed child stays a zombie until reaped, so without the zombie check `kill -0` succeeds and the script would always fall through to its 10-second SIGKILL path. The test asserts the TERM path: the stdout has no `SIGTERM timed out` line. The tests spawn two real `sleep` processes and list them:
  - one with this temp checkout's command line (`<temp repo>/.venv/bin/python -m uvicorn apps.api.main:app …`);
  - one with a foreign checkout's (`<other dir>/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --port 8081`).

  They assert the first is terminated and the second is still alive, then clean up. `kill` is a bash builtin and cannot be faked; real child pids make that safe.
- **Systemd branch.** A fake `systemctl`: exit 0 for `--user show-environment`, `daemon-reload`, `stop …`, `enable --now …`, and it prints a pid for `show … --property MainPID --value`. It records its argv. The unit file is staged at `<temp repo>/infra/systemd/nhms-display-api.service`, and `XDG_CONFIG_HOME` points inside the temp dir. The tests assert:
  - `stop` precedes `enable --now`;
  - the unit was installed;
  - the anchored sweep ran;
  - the foreign sleeper survived.
- **Legacy branch.** Same process assertions without the unit.
- **Escape.** The temp repo lives under a directory name containing `+` and `.` (for example `a+b.c`). The anchored pattern must match that checkout's own command line and not a sibling path that differs only where an unescaped metacharacter would have matched.

### D9. The live receipt proves the restart fix with a decoy

node-27 has no second display checkout any more, so the receipt builds one without touching production data:
- `/home/nwm/tmp/n/yd-NWM/.venv/bin/python` is a symlink to `/usr/bin/python3`;
- `PYTHONPATH=/home/nwm/tmp/n/decoy` holds a stub `uvicorn/__main__.py` that only sleeps;
- it is launched detached as `/home/nwm/tmp/n/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8081 --workers 2`.

`/proc/<pid>/cmdline` keeps the invoked path. The stub binds no port.

**Before the deploy (read-only):**
- the old pattern's `pgrep -af` lists both `:8080` and the decoy;
- the anchored pattern lists only `:8080`.

**Deploy:** `git pull --ff-only`, then the fixed `bash scripts/ops/start-display-api.sh`.

**After:**
- the decoy's pid is unchanged and alive;
- `:8080` has a new `MainPID`, `/health` answers 200, and C1 passes.

Then the decoy is stopped by pid and its directory removed.

**Governing invariant:** an identity-bound route PASSes only on a 2xx whose single echoed identity agrees field by field with the requested strict identity (I1/I2), and the restart signals only this checkout's uvicorn (I5).

**Sibling surfaces:**
- `two_node_e2e_evidence.py` comparators `:1413`, `:2384` (D3);
- the route re-keying in `readonly_db_merge.py:583-604`;
- the consumer `two_node_e2e_readonly_db_lane.py:1156-1290`;
- `tests/test_pipeline_ops_identity_envelope.py` (private-function imports `:10-13`, `:143-155`);
- `scripts/diagnostic/display-cold-waterfall.sh:103` (calls the restart script);
- `docs/runbooks/production-ops/gateway-and-services.md:718`;
- `infra/systemd/nhms-display-api.service` `ExecStart`.

## Invariants

- **I1:** a 2xx whose echo contradicts the requested strict identity never PASSes. It is `FAIL`.
- **I2:** a 2xx missing any required echo field never PASSes. It is `BLOCKED`, naming exactly the missing fields. Extraction takes one candidate mapping and never stitches fields across candidates.
- **I3:** the overall `status` is unchanged for every combination of role, probe and manual-action results, and for every route result not affected by D1/D2. The deny-write probes, their SQL and their verdicts do not change.
- **I4:** `two_node_e2e_evidence` behaviour is unchanged. Its test suite passes without edits.
- **I5:** the restart script signals only processes whose command line starts with `$REPO_ROOT/.venv/bin/python -m uvicorn apps.api.main:app`. This checkout's orphans are still stopped in both branches.
- **I6:** no HTTP route, envelope, OpenAPI or frontend change. No DB write. Discovery is SELECT-only under the readonly role.

## Must-preserve

- **The two-node consumer.** `two_node_e2e_readonly_db_lane.evaluate_readonly_db` (`:1156-1290`) re-reads each route record's `name`, `status`, `identity_blockers`, `source` and strict identity, and compares them semantically with `display_identity`. Route records keep those keys and meanings:
  - a PASS record never carries `identity_blockers`;
  - `display_identity` only gains `basin_id`;
  - the new `lane_statuses` key is additive.
  - Its tests (`tests/test_two_node_e2e_evidence.py -k readonly_db`) stay green unchanged, and one new test feeds a summary produced by the new route smoke through `evaluate_readonly_db`.

- **Extraction never stitches** fields across candidate mappings (pinned by `tests/test_readonly_db_validation_routes.py:168-236`, whose `"response_identity" not in` assertions become "the single best candidate's partial identity").
- Manual-action probes target `_manual_action_run_id(identity)` (`readonly_db_validation.py:173-177`). D4 changes which run that is: a ready run, filtered by source. The probes still return 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with `write_executed: false`.
- The CLI flags and exit codes of `scripts/validate_readonly_db_boundary.py` / `readonly_db_validation.main` (0 PASS, 2 BLOCKED, 1 FAIL/error).
- Evidence file names (`role.json`, `route_smoke.json`, `permission_probes.json`, `summary.json`) and the approved evidence-root rule.
- Merge (`--merge-source-dir`) rules and blocker codes.
- `ROUTE_FIXTURE_BLOCKER_ERROR_CODES`.
- The restart script's preflight exits (2/3), the env-key assertions, the post-launch smoke, and its stdout lines apart from the pid lists.

## Risks / Trade-offs

- **FAIL instead of BLOCKED on an echo mismatch** could turn a formerly-BLOCKED run into FAIL. The only historical mismatch was the `cycle_time` spelling, which D1 makes equal. A real mismatch is a real defect.
- **The status filter** can skip the newest run while it is still `running`. That is the intent: a non-ready run 404s on `latest_product`.
- **The newest ready run may have no logged job yet** (publication lag), so `job_logs` would be `BLOCKED`. That is honest. The operator can pin `--strict-run-id` to an older run.
- **A logged job whose `log_uri` is a local path** (not readable under `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`) gives `job_logs` a `JOB_LOG_*` `BLOCKED`. That is honest and is recorded as observed.
- **Discovery keeps the stored `source_id` spelling** (`gfs`). The merge keys `display_identity` by it, and the consumer looks sources up in upper case (`two_node_e2e_readonly_db_lane.py:1372-1387`). So D4 returns `source` upper-cased (`upper(r.source_id) AS source`); the routes accept both spellings.
- **`REPO_ROOT` follows the caller's working directory** (`git rev-parse --show-toplevel` from cwd, with the script directory only as a fallback). The anchor therefore protects every checkout except the one the operator stands in: running `/home/nwm/NWM/scripts/ops/start-display-api.sh` from inside another checkout would target that checkout. This predates the change (review round 1, P2), is out of scope, and is tracked as a follow-up. On node-27 the canonical invocation is `cd /home/nwm/NWM && bash scripts/ops/start-display-api.sh`.
- **Hand-supplied `--cycle-time` / `--model-id` overrides are not discovery filters.** They must name the same run as `--strict-run-id`; otherwise the tuple contradicts itself and the routes answer 404/409 (`BLOCKED`/`FAIL`, never PASS). This is documented in the C2 runbook note (review round 1, P2).
- **The systemd branch always restarts `/home/nwm/NWM`,** whichever checkout invokes the script, because the unit hardcodes that path. This predates the change and is out of scope; it is reported in the PR.
- **The anchored pattern misses a process launched through a different path to the same checkout** (for example a symlinked root). Context records that node-27's three spellings agree. Other hosts get the #597 cleanup only for the canonical path.
