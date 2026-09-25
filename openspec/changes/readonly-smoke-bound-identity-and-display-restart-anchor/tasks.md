## Risk packs

- **Public API / CLI / script entry: selected.** Changes:
  - `validate_readonly_db_boundary.py` changes its verdicts (D1/D2) and its discovery (D4), with flags and exit codes unchanged;
  - `scripts/ops/start-display-api.sh` changes its process selection (D8).
  - Callers: the C2 runbook step; `scripts/diagnostic/display-cold-waterfall.sh:103` and the runbooks for the restart.
  - Covered by 2.1-2.8 and the live receipt (§5).
- **Error handling / partial outputs: selected.** D1's partial extraction; D2's non-2xx rule; the MISMATCH-as-FAIL verdict; D4's "no unrelated job" rule (2.1, 2.2, 2.4).
- **Auth / permissions / secrets: selected.**
  - The deny-write lane's probes, SQL and verdicts are untouched (I3); `lane_statuses` only reports them (2.5).
  - Discovery stays SELECT-only under `nhms_display_ro` (I6).
  - Evidence redaction is unchanged; no DSN appears on argv (§5).
- **Release / operational: selected.**
  - The fixed restart script is the deploy step itself, run on node-27 with a live decoy (D9, §5).
  - Rollback is `git checkout <previous SHA> -- scripts/ops/start-display-api.sh` plus the previous unit behaviour; the script does not change the unit file.
- **Published NHMS artifacts / display identity: selected.** Strict identity binding is the subject of #2484. I1/I2 are covered by 2.1, and the two-node consumer by 2.6.
- **Legacy compatibility / examples: selected.**
  - the two-node lane's helper names and behaviour stay (D3, I4);
  - evidence file names and merge rules stay (Must-preserve);
  - `display_identity` gains `basin_id` additively.
- **Documentation / migration notes: selected.** The runbook C2 note and the anchored inspection command (2.8); the receipt (§5).
- **Schema / columns / units / field names: selected.** No HTTP field changes, but the evidence documents change under the same `LIVE_EVIDENCE_SCHEMA`:
  - `summary.json` gains `lane_statuses` and `display_identity.basin_id`;
  - `route_smoke.json` gains the `display_read_route_response_identity_mismatch` reason, and records `response_identity` partially;
  - an echo mismatch becomes `FAIL`.

  The consumer is `two_node_e2e_readonly_db_lane`. Covered by 2.5, 2.6 and the inventory update in 2.8.
- **Not selected:**
  - Migration (no DB schema change);
  - Concurrency (a single validator run; the restart script is operator-serialised);
  - File IO (the evidence writer is unchanged);
  - Config (no new env keys);
  - PostGIS/TimescaleDB (discovery reads two plain tables);
  - Resource limits (two indexed LIMIT-1 reads).

## 1. Baselines

- [x] 1.1 Live C2 baseline (proposal "Why"). node-27, `4b7d1f43f`, run from `/home/nwm/NWM`: `NHMS_DISPLAY_READONLY_DATABASE_URL=<ro DSN> uv run --no-sync python scripts/validate_readonly_db_boundary.py --evidence-root /home/nwm/NWM/artifacts/issue2484-n --run-id n-baseline-<ts>`. Results:
  - rc 1 (`FAIL`);
  - route table as in the proposal;
  - `permission_probe_summary` 23/23 denied;
  - evidence `artifacts/issue2484-n/n-baseline-*/db/readonly-db-boundary/`.
- [x] 1.2 Bound re-requests against the live `:8080`:
  - `latest-product` with the discovered run's `basin_id=basins_sw_ylzb` → 200;
  - `jobs/<that run's newest logged job>/logs` → 200 with the `identity` echo.
- [x] 1.3 Archived runs:
  - `issue1987-52-c2-20260918`: latest_product 404 with MISSING×4;
  - `…-c2b-20260918`: latest_product 200 with a `cycle_time` MISMATCH. This answers #2484 acceptance 1: both shapes occurred, in two runs with different identities;
  - `issue2420-readonly-87236ca54` (GFS/IFS): 9/9 PASS with a hand-supplied `Z` tuple.
- [x] 1.4 #2282 host facts (design Context):
  - the only matching process is `:8080`, the unit's `MainPID`;
  - `git rev-parse --show-toplevel` equals `readlink -f` equals `/home/nwm/NWM`;
  - `/home/nwm/yd-NWM` is absent.
- [x] 1.5 The receipt §8 attribution correction in #2484's last acceptance item is already on master: `openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-18-i8-task52/README.md:458-468`. No edit needed.

## 2. Implementation

- [x] 2.1 **D1 + D2** in `services/production_closure/readonly_db_route_smoke.py`: extraction, per-field comparison, the verdict order (FAIL over BLOCKED over PASS), and no echo blockers on non-2xx. Unit tests in `tests/test_readonly_db_validation.py`, each with real-shaped bodies (2.3):
  - each of the five identity-bound routes PASSes with a `Z` echo against a `+00:00` request;
  - a tampered `run_id`, `model_id`, `cycle_time` (at least one hour off) and `job_id`, one test each → `FAIL` with a `…_MISMATCH` blocker naming only that field, `expected`/`observed` set;
  - an echo missing exactly one field → `BLOCKED`, `response_identity` present with the other fields, and exactly one `…_MISSING` blocker;
  - a missing field plus a contradicting field → `FAIL`;
  - an unparseable `cycle_time` echo → exact-string rule (MISMATCH unless byte-equal);
  - `source` `gfs` vs `GFS` agrees;
  - 404 `QHH_LATEST_PRODUCT_UNAVAILABLE` → `BLOCKED` with `error_code` and no `identity_blockers`;
  - 404 `PIPELINE_STRICT_IDENTITY_NOT_FOUND` → `BLOCKED`;
  - 409 `PIPELINE_STRICT_IDENTITY_MISMATCH` → `FAIL`, no `identity_blockers`;
  - a 500 → `FAIL`.
  - **Existing tests updated to the new contract** (not deleted):
    - `tests/test_readonly_db_validation_routes.py:134-165`: mismatch → `FAIL` / `display_read_route_response_identity_mismatch`;
    - `:168-236` (fragmented bodies): still not `PASS`, with no stitching; `response_identity` equals the single best candidate's partial identity (for the `data.item` case, `{cycle_time, run_id}`); MISSING blockers only for the absent fields;
    - `:91-131`: its query-echo fixture is replaced by real envelopes (2.3), and its path assertions stay;
    - `tests/test_pipeline_ops_identity_envelope.py:143-155` (DB-backed, node-27): keeps importing `_route_response_identity` / `_route_response_identity_blockers`. If their signatures change, the test is adapted. It gains a case whose expected `cycle_time` uses `+00:00`.
- [x] 2.2 **D3:** `services/production_closure/identity_matching.py` holds the two moved functions, verbatim. `two_node_e2e_evidence.py` imports them and binds its private names to them. Unit tests for the helper:
  - `Z`, `+00:00` and `+08:00` spellings of one instant are equal;
  - a naive ISO string is treated as UTC;
  - `YYYYMMDDHH` works;
  - garbage does not normalise.

  `tests/test_two_node_e2e_evidence.py` passes with **no edits**.
- [x] 2.3 **D7:** the route requester fixture emits real envelopes. The old `data.identity` echo shape is removed from every PASS-path fixture. One test validates every fixture success body through its envelope model:
  - `PipelineStatusEnvelope`, `PipelineStageListEnvelope`, `PipelineJobPageEnvelope` (one item, and `items: []`), `JobLogsEnvelope`, `QhhLatestProductEnvelope`;
  - and asserts the ops envelopes' `identity` validates as `OpsIdentity` / `OpsLogIdentity`.
- [x] 2.4 **D4 + D5:**
  - `readonly_db_probe_adapter.discover_display_identity(*, source=None, run_id=None)`, the protocol in `readonly_db_types.py`, and `_safe_discover_identity` passing `config.source` / `config.strict_run_id`;
  - `QHH_LATEST_READY_RUN_STATUSES` imported, not retyped;
  - `latest_product` carries `basin_id` when known.

  Tests:
  - **unit:** the fake adapter at `tests/test_readonly_db_validation.py:446` (and any other fake with `discover_display_identity(self)`) takes the new keyword arguments. A test asserts `config.source` / `config.strict_run_id` actually reach the adapter, because `_safe_discover_identity`'s `except Exception` would otherwise turn a `TypeError` into a silent discovery blocker. Also covered: the adapter's SQL and parameters (status filter only without a configured run id; case-insensitive source; the job query bound to the run id; no job query without a run id); the `latest_product` path carries `basin_id`, and does not without it; overrides still win.
  - **real-DB integration** (new file, skips without `NHMS_RUN_INTEGRATION`, disposable DB built from migrations). Seed:
    - two basins;
    - `hydro_run` rows for `GFS` (stored `gfs`) and `IFS`, where the newest-updated row is `running` and an older one is `published`;
    - `ops.pipeline_job` rows with `log_uri`, where the newest job overall belongs to a **different** run.

    Assert:
    - discovery returns the newest ready run, its `basin_id`, and that run's own newest logged job;
    - `source="GFS"` returns the `gfs` run, with `source` returned as `GFS`;
    - `run_id=<running run>` returns it regardless of status;
    - a ready run with no logged job yields no `job_id`.
- [x] 2.5 **D6:** the lane-status helper, `_overall_status` expressed through it, and `summary["lane_statuses"]` in `readonly_db_validation.py` and `readonly_db_merge.py`. Tests:
  - a table over the role / probe / manual-action / route combinations shows overall `status` identical to master's `_overall_status` (I3). Master's function is copied into the test as the oracle;
  - the four spec cases: deny-write PASS with read BLOCKED; deny-write FAIL with read PASS; a writer role; both PASS;
  - the merged summary carries `lane_statuses`; the merge CLI still refuses a non-PASS source bundle (exit 1, `READONLY_DB_MERGE_SOURCE_NOT_PASS`, unchanged); `_merged_readonly_db_summary` with a FAIL route item gives `status` `BLOCKED` together with `lane_statuses.read_routes` `FAIL`.
- [x] 2.6 **Two-node consumer:** injected components make a run simulated, and the merge rejects simulated bundles. So the test runs `run_display_route_smoke` with the real-shaped fixtures, per source (GFS and IFS). It wraps each output in a live-schema readonly summary built the way the existing two-node readonly-DB tests build theirs (reusing their builders in `tests/test_two_node_e2e_evidence.py`), replacing only `route_smoke` and `display_identity`, including `basin_id` and `lane_statuses`. It then runs the governed entrypoint (`validate_two_node_e2e_evidence`, or `evaluate_readonly_db` as the existing `-k readonly_db` tests call it).
  - Assert: the readonly DB lane has no `TWO_NODE_E2E_READONLY_DB_ROUTE_*` blocker or finding.
  - Assert: a route record with an echo MISMATCH (now `FAIL`) yields `TWO_NODE_E2E_READONLY_DB_ROUTE_CHILD_NOT_PASS`.
- [x] 2.7 **D8** in `scripts/ops/start-display-api.sh`: `REPO_ROOT_RE`, the anchored `UVICORN_PATTERN`, `pgrep -f --` at all three call sites, and the header comment. Harness tests in `tests/test_two_node_docker_runtime.py`:
  - the anchor-sensitive fake `pgrep` over real `sleep` children;
  - the fake `systemctl` with the staged unit;
  - the legacy branch;
  - the metacharacter repo path.

  Each asserts this checkout's sleeper is terminated and the foreign one is alive, with cleanup in `finally`. The existing 7 harness cases stay green. Their fake `pgrep` must accept `--`.
- [x] 2.8 **Docs:**
  - `docs/runbooks/production-ops/gateway-and-services.md:718` inspection command anchored to `/home/nwm/NWM`;
  - `docs/runbooks/node-27-bringup-checklist.md` C2: discovery binds the tuple itself, a hand-supplied tuple is optional, `lane_statuses` is where the deny-write verdict is read, and the `cycle_time` spelling no longer matters;
  - `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md` readonly DB row (unconditional, per `services/production_closure/AGENTS.md`): the additive `lane_statuses` / `display_identity.basin_id`, the MISMATCH→FAIL route semantics, and the cycle-hour comparison.

## Implementation deviations

- **New test files.** New cases for 2.1, 2.5 and 2.7 live in new files (`tests/test_readonly_db_route_identity.py`, `tests/test_readonly_db_discovery_and_lane_statuses.py`, `tests/test_start_display_api_restart_anchor.py`, plus `tests/test_readonly_db_discovery_integration.py`, `tests/test_readonly_db_two_node_consumer.py`, `tests/test_identity_matching.py`). The reason is that `.large-file-guard.json` (maxLines 1000) blocks any edit to `tests/test_two_node_docker_runtime.py` (5496 lines), which is unchanged, and `tests/test_readonly_db_validation.py` is at 868 lines. The existing tests that 2.1 names were updated in place.
- **Inspection command path.** The inspection command moved with the #1103 runbook split: `docs/runbooks/production-ops/gateway-and-services.md:718`, not `current-production-ops.md:1994`.
- **Merge refusal.** The merge refuses a non-PASS source bundle at load (exit 1), not at summary build (exit 2); see the correction in D6.
- **CI routing (3.3).** The `services/production_closure/**` rule plus same-name selection already route the changed modules. No duplicate rule was added; two `tests/test_select_ci_tests.py` cases pin the coverage.

## 3. Verification

- [ ] 3.1 Local:
  - `uv run ruff check .`;
  - `uv run pytest -q tests/test_readonly_db_validation.py tests/test_readonly_db_validation_routes.py tests/test_pipeline_ops_identity_envelope.py tests/test_two_node_e2e_evidence.py tests/test_two_node_docker_runtime.py <new test files> tests/test_select_ci_tests.py` (`test_pipeline_ops_identity_envelope.py` is DB-backed and may skip locally);
  - `openspec validate readonly-smoke-bound-identity-and-display-restart-anchor --strict --no-interactive`.
  - Red proofs: the new 2.1 and 2.7 tests fail on master's `readonly_db_route_smoke.py` / `start-display-api.sh` (temporary checkout of the two files, reverted), with counts in the PR.
- [ ] 3.2 node-27 disposable-DB pytest on the frozen SHA (`/home/nwm/tmp/node27-pr-runner.sh`): the files from 3.1 plus the new real-DB discovery test, with 0 skipped for the integration file and for `tests/test_pipeline_ops_identity_envelope.py`. Then the node-27 full pytest; its failure set must equal master's (#2615 only).
- [x] 3.3 `scripts/select_ci_tests.py` routes `services/production_closure/readonly_db_*.py`, `identity_matching.py` and `scripts/ops/start-display-api.sh` to their tests. A `tests/test_select_ci_tests.py` row is added if a route is new.

## 4. PR

- [ ] 4.1 PR with body per the project PR rules; CI green; review rounds per the fixture level; merge.

## 5. node-27 live receipt (after merge, on the merged SHA)

- [ ] 5.1 **Decoy + red proof (read-only, before pull):** launch the D9 decoy and record its pid. `pgrep -af` with the old pattern lists `:8080` and the decoy; the anchored pattern lists only `:8080`.
- [ ] 5.2 **Deploy = C1:**
  1. `git status --porcelain` is empty; `git pull --ff-only` to the merge SHA;
  2. `bash scripts/ops/start-display-api.sh`;
  3. the decoy pid is unchanged and alive;
  4. the new `MainPID` differs from the old one;
  5. `/health` 200, `/api/v1/runtime/config` `display_readonly`, `/api/v1/slurm/health` 404;
  6. the public `https://test.nwm.ac.cn/` serves the dist.

  Write `c1-approved.json` (status, `head_sha` = `reviewed_sha` = merge SHA, checks). Stop the decoy and remove its tree.
- [ ] 5.3 **C2:**
  1. **unattended** (no identity flags): expect `status` PASS, 9/9 routes PASS, `lane_statuses` both PASS;
  2. per source, `--source GFS` and `--source IFS`, each PASS;
  3. merged via `--merge-source-dir` (both), which gives full-scope PASS.

  Evidence stays under `artifacts/issue2484-n/`, with the DSN only in env. Any non-PASS is recorded as observed, not re-run into a PASS by hand-picking a tuple.
- [ ] 5.4 **C3 (27-side cross-plane identity, both sources):** per source, the same `run_id`/`source`/`cycle_time`/`model_id`/`basin_id` is shown to chain:
  - the node-22-produced `hydro_run` row (`published`);
  - its published job log (`job_logs` 200 with echo);
  - `latest-product` (200, strict);
  - `/` and `/ops` in 5.5's browser lane.

  Recorded honestly as C3's node-27 half. The two-node producer-bundle aggregator (`validate_two_node_e2e_evidence.py`) needs a node-22 producer bundle and is not run. Whatever it cannot claim is stated.
- [ ] 5.5 **C4:**
  1. `c1-c4-approval.json` (GFS/IFS identities and job ids from 5.3), following the #2420 layout;
  2. `node27_c4_production_acceptance.py freeze`, then `test:e2e:live-c4-display` (basin `basins_qhh`, segment `basins_qhh_shud_reach_000001`, or the checklist's current pins), then `bind` and `verify`, following the checklist C4 sequence.

  The verdict is recorded as observed. A basemap-throttle or source-error non-PASS is reported, not waived.
- [ ] 5.6 The receipt `evidence/node27-live-receipt.md` in the archive PR:
  - C1-C4 each PASS / PARTIAL / BLOCKED, with the reason;
  - digests of the private approval and freeze files;
  - #2282's decoy survival;
  - the #2420 premise correction ("job_logs stays BLOCKED" no longer holds).

## Evidence Floor

1. Every identity-bound route PASSes on a `Z` echo against a `+00:00` request. A tampered field FAILs, naming only that field (`cycle_time` at least an hour off). A single missing field BLOCKs, naming only that field, with `response_identity` kept. Fragmented bodies never stitch into a PASS. Non-2xx carries no echo blockers (2.1).
2. Fixture success bodies validate against the real envelope models. No PASS-path fixture uses the invented `data.identity` echo (2.3).
3. Discovery on a real PostgreSQL returns the newest ready run, its basin, and that run's own logged job, never another run's (2.4, node-27 3.2).
4. The overall status equals master's for every combination not touched by D1/D2. `lane_statuses` separates deny-write from read routes (2.5). The two-node suite passes unchanged (2.2, 2.6).
5. On both restart branches the restart script terminates this checkout's stale uvicorn and leaves a foreign checkout's running, including for a metacharacter repo path (2.7).
6. node-27 live: an unattended C2 run gives PASS 9/9, per source and merged, with the deny-write lane PASS (5.3). The deploy restart leaves a decoy foreign uvicorn alive (5.1/5.2). C1, C3 (node-27 half) and C4 are each recorded as observed (5.2, 5.4, 5.5).
7. No local ruff or unit run is presented as a C1-C4 PASS. Every live claim cites its node-27 evidence path.
