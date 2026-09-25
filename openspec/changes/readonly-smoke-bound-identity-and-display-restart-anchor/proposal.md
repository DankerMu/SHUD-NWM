## Triage

```text
Issue type: bugfix (#2484, #2282)
Fixture level: expanded
Upstream suggested level: absent (#2484 names a node-27 live rerun; #2282 kills processes on a shared production host; both gate the C1-C4 checklist)
Blast radius: a wrong route-smoke verdict either keeps C2 permanently non-PASS (today) or lets a mis-bound identity PASS; a wrong process pattern keeps killing, or stops cleaning up, uvicorn processes on node-27
Selected risk packs: Public API/CLI/script entry, Error handling/rollback, Auth/permissions, Schema/columns/field names (evidence documents), Release/operational, Published NHMS artifacts/display identity, Legacy compatibility, Documentation (tasks.md)
Evidence floor: tasks.md Evidence Floor 1-7 (node-27 live C1-C4 receipt, including the decoy-process restart check)
```

## Why

Batch N of the 10-batch serial run (master `09acbd5cb`, after M #2635 / #2636).

### #2484: the readonly-DB route smoke cannot PASS on its unattended path

#2484 was filed from the 2026-09-18 C2 run. **Part of its text is stale.** #2420 (`ffeefc10b`, closed 2026-09-20) changed the real response schema after that run: `apps/api/routes/pipeline.py:_ok` now adds a top-level `identity` block (`_success_identity_payload`) to every strict-identity success, and `apps/api/response_models/pipeline.py:216-235` (`OpsIdentity` / `OpsLogIdentity`) types it. So the issue's table ("pipeline_status has no `run_id`", "pipeline_stages has no identity field", "jobs has no `source`/`cycle_time`") no longer holds, and "job_logs stays BLOCKED on #2420" no longer holds either.

**Live baseline, node-27, 2026-09-25, `4b7d1f43f`**, with `scripts/validate_readonly_db_boundary.py` under `nhms_display_ro` and **no identity overrides** (evidence `artifacts/issue2484-n/n-baseline-*`). The verdict is **`FAIL`**:

| route | http | verdict | cause |
|---|---|---|---|
| health, runtime_config, models, stations | 200 | PASS | — |
| jobs, pipeline_status, pipeline_stages | 200 | BLOCKED | all four fields extracted from the top-level `identity`; the only blocker is `cycle_time` `MISMATCH`, `2026-09-24T12:00:00Z` observed vs `2026-09-24T12:00:00+00:00` expected (exact string compare, `readonly_db_route_smoke.py:290`) |
| latest_product | 404 `QHH_LATEST_PRODUCT_UNAVAILABLE` | BLOCKED | discovery picked the newest `hydro_run` (basin `basins_sw_ylzb`), but the smoke sends no `basin_id`, so the route defaults to QHH. Re-requested with `basin_id=basins_sw_ylzb`, it returns 200. The result still carries MISSING×4 identity blockers, computed from a 404 error body. |
| job_logs | 409 `PIPELINE_STRICT_IDENTITY_MISMATCH` | **FAIL** | discovery takes the newest `ops.pipeline_job` with a `log_uri` **regardless of run**: a GFS cycle job paired with an IFS run. With that run's own newest logged job it returns 200 with a full `identity` echo. |

The deny-write half is clean in the same run: 23/23 probes denied, `failed_mutating_count 0`, and both control-plane mutations returned 409 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`.

The #2420 and #2450 C2 runs (2026-09-20, `artifacts/issue2420-readonly-87236ca54/…`) show 9/9 PASS on GFS and IFS. They got there only with a **hand-supplied** tuple (`--source`/`--cycle-time …Z`/`--strict-run-id`/`--model-id`/`--job-id`, recorded in `docs/runbooks/receipts/2026-09-20-issue2420-job-provenance.json`), where the `Z` spelling happens to match the echo byte for byte. So what is broken is the validator's own contract and its discovery path, not the routes:

1. `cycle_time` is compared as a string, so the same instant in `+00:00` and `Z` spelling is a MISMATCH (`readonly_db_route_smoke.py:290`). The sibling lane already compares cycle hours after UTC normalisation (`two_node_e2e_evidence.py:2388-2408`).
2. `_route_response_identity` is all-or-nothing (`:246`). One missing field makes it return `{}`, so the evidence drops the response identity and reports all four fields MISSING. That is how the 2026-09-18 receipt's first draft reasoned from the wrong symptom.
3. Identity blockers are computed for non-2xx responses too (`:184-185`), so a 404 error body is reported as MISSING×4 on top of its real error code.
4. `discover_display_identity` (`readonly_db_probe_adapter.py:61-113`) returns a tuple that contradicts itself:
   - the `job_id` is not bound to the discovered run;
   - the run has any status and ignores a configured `--source`/`--strict-run-id`;
   - the run's basin is never carried to the `latest_product` request.
5. The lane's route-smoke fixtures invent `data.identity` = the query parameters echoed back, a shape no route produces:
   - `tests/test_readonly_db_validation.py:549-559`;
   - `tests/test_readonly_db_validation_routes.py:91-131`.

   The only test that runs real route bodies through the validator's extraction and blocker functions is `tests/test_pipeline_ops_identity_envelope.py::test_strict_ops_success_identities_match_latest_product_shaped_canonical_selector` (`:143-155`, DB-backed, added by #2420). It passes only because its expected identity uses the `Z` spelling, so it never exercises the `+00:00` pairing that discovery produces.

The summary also cannot say "deny-write PASS, a read route is not" without a reader walking every item: `_overall_status` (`readonly_db_validation.py:324-338`) folds both into one verdict.

### #2282: the canonical display restart kills other checkouts' uvicorn

`scripts/ops/start-display-api.sh:23` matches `\.venv/bin/python -m uvicorn apps\.api\.main:app` with no checkout anchor. After `systemctl --user stop` has already stopped this checkout's server, the unconditional `pgrep -f` sweep at `:124-141` TERMs, then KILLs, every other checkout's display uvicorn. The legacy detached branch shares the same pattern.

On node-27 today (2026-09-25):
- the only matching process is this checkout's `:8080` (`/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2`, the unit's `MainPID`);
- `/home/nwm/yd-NWM` no longer exists, and the yd deployment now runs in Docker (`yd-web` on `:8082`).

The defect still stands: the canonical restart command kills any same-user uvicorn from another checkout. The live receipt proves the fix with a decoy process (design D8).

## What Changes

- **#2484: the route smoke keeps the echo contract and judges it semantically.**
  - An identity-bound route PASSes when it is 2xx **and** its response identity echo (top-level `identity` for the four ops routes, `data` for `latest_product`) matches the requested strict identity. `cycle_time` is compared as the same UTC cycle hour, whatever its spelling. `source` stays case-insensitive, other fields exact.
  - Partial extraction is kept in the evidence, and only the fields actually missing or contradicting are blocked.
  - Non-2xx responses carry no identity-echo blockers.
  - The cycle-hour comparison moves into one shared helper, which the two-node lane re-uses unchanged.
- **#2484: discovery returns a self-consistent tuple.** Discovery **deviates from #2484's boundary**; the justification is the FAIL/404 baseline above.
  - Discovery picks the newest display-ready run (`QHH_LATEST_READY_RUN_STATUSES`, the latest-product selector's own gate), filtered by a configured source or business run id.
  - It carries that run's `basin_id`, and binds `job_id` to that run's newest logged job.
  - The `latest_product` smoke sends the run's `basin_id`.
- **#2484: the summary separates the lanes.** `lane_statuses = {deny_write, read_routes}` in both the single-run and the merged summary. The overall `status` is unchanged, and is still the worst of the lanes.
- **#2484: fixtures use real response schemas**, validated against the `apps/api/response_models` envelopes. The old `data.identity` echo fixture is removed.
- **#2282:** the uvicorn pattern is anchored to the ERE-escaped `$REPO_ROOT` (`^<repo>/\.venv/bin/python -m uvicorn apps\.api\.main:app`) for both branches, and passed after `--`. The harness gains a fake `systemctl` and a staged unit, so the systemd branch is covered for the first time, plus an anchor-sensitive fake `pgrep`.
- **Spec:**
  - `readonly-db-and-e2e-evidence` "Display readonly DB validation" is MODIFIED: echo-consistency semantics, no echo blockers on non-2xx, self-consistent discovery, lane statuses;
  - `production-ops-readiness` "Display API restart is reproducible from a single command" is MODIFIED: "this checkout's" uvicorn.
- **Receipt:** a node-27 live C1-C4 receipt on the merged SHA (tasks §5). It covers:
  - the deploy, done with the fixed restart script while a decoy uvicorn from another checkout path survives;
  - C2 unattended plus per-source GFS/IFS merged;
  - C3 as the per-source identity chain;
  - C4 with the `live-c4-display` lane and the production-acceptance freeze/bind/verify.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `readonly-db-and-e2e-evidence`: identity-bound route PASS means a 2xx whose echo agrees with the request by field semantics; discovery is self-consistent; the summary reports the deny-write lane separately.
- `production-ops-readiness`: the restart script stops only this checkout's uvicorn.

## Impact

- Code:
  - `services/production_closure/readonly_db_route_smoke.py`, `readonly_db_probe_adapter.py`, `readonly_db_types.py` (the adapter protocol), `readonly_db_validation.py`, `readonly_db_merge.py`;
  - a new shared helper module under `services/production_closure/`;
  - `services/production_closure/two_node_e2e_evidence.py`: import only, with its behaviour unchanged;
  - `scripts/ops/start-display-api.sh`.
- Tests:
  - `tests/test_readonly_db_validation.py`, `tests/test_readonly_db_validation_routes.py`, `tests/test_pipeline_ops_identity_envelope.py` and `tests/test_two_node_docker_runtime.py`;
  - a real-DB discovery test;
  - `tests/test_two_node_e2e_evidence.py`, which must stay green unchanged.
- Docs:
  - the `docs/runbooks/production-ops/gateway-and-services.md:718` inspection command is anchored;
  - `docs/runbooks/node-27-bringup-checklist.md` C2 notes that discovery needs no hand-supplied tuple;
  - `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`, readonly DB row: the additive evidence fields and the MISMATCH→FAIL semantics, as `services/production_closure/AGENTS.md` requires.
- No HTTP route, OpenAPI, or frontend change. No DB write. No migration.
- Out of scope, reported only:
  - `openspec/specs/production-ops-readiness/spec.md:69`, which still calls the systemd unit an out-of-scope follow-up (#2282 defers it);
  - historical receipts containing `pkill -f "uvicorn apps.api.main:app"`.
