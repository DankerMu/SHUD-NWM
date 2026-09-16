# R3 remaining cold-family deletion and R4 authority fixture

Issue #1895 / epic #1891. Expanded fixture, high repair intensity. Sole executable authority: parent tasks.md R3.1–R3.7, R4.1–R4.4 and R5. Publication requires R1.4 retained workload and already-delivered R1.1–R1.3/R1.6; paired-budget consumers belong to R2 atomic cutover, never an independently broken merge.

## Scope and invariant

Delete actual cold-only closures, not entire historical PRs or filename wildcards: packages/common compressed_chunk_cold_* and probe directory; node27_cold_tablespace_*, node27_cold_governance*, cold census policy; old node27_issue1895_* G0–G8 family; their script/shell entrypoints, cold env template, cold/G0–G8 schemas and synthetic examples, tests/fakes/mutants and all stale selector paths. Issue1895 importers must disappear before/together with imported cold owners. Enumerate current files and consumers before deleting. Every required survivor is handed off first.

Governing invariant: removing withdrawn selective-cold capability leaves no runnable cold activation or broken surviving consumer, while PGDATA, resource governance, ordinary maintenance, readonly, C4 and shipping forecast guarantees remain executable under their real owners. History and protective negative refusals remain explicit exceptions, not dormant implementations.

## Invariant matrix and required dispositions

| Surface | Required action | Proof |
| --- | --- | --- |
| Python owners/callers | Delete complete cold and G0–G8 closures after surviving consumers move | Dynamic imports, conftest, test fixtures, generated and manual/cross-language edges classified; surviving imports collect and execute |
| Executables/config/schema | Delete old CLI/wrappers/env, cold receipts and synthetic examples | No active command/config/schema references deleted path; no shim, stub or fallback |
| Roles | Remove nhms_cold CREATE grant and positive audit in db/roles/node27_write_roles.sql; provisioning description follows | All unrelated owner/flags/membership/trigger/default/deny-write tests survive; source change not live REVOKE |
| G7 | Remove recorder/capture-validator/digest and performance/lanes callers after R1.4 | Same cut removes test_issue2227_explicit_cycle_named_binding.py, ISSUE2227 selector edges; apply explicit-cycle-query-binding REMOVED and delete empty canonical capability |
| Mixed tests | Keep lifecycle mutex implementation; remove cold CLI branch only | Compression/retention/replay/PGDATA contention and override refusal execute without deleted imports |
| Collection gate | Move generic behavior from test_node27_cold_tablespace_marker_contract.py to test_node27_docker_collection_gate.py, delete cold AST pins | Docker opt-in alone still cannot unlock ordinary DB integration; tests/conftest.py gating unchanged |
| Selectors | All retained producers/shared helpers select actual surviving behavioral suites | No nonexistent file selections/empty collection; independent C4/readonly and new workload owner remain selected |
| Manual authority | Current storage/bringup/current-ops/ADR links follow surviving owners | Executable current guidance has no cold activation; withdrawn sections explicitly historical; immutable receipts/reviews unchanged |
| Survivor exclusions | Keep display-cold-waterfall cache diagnostic, migrations000058/000059, safe_fs/evidence_io, readonly, C4, shipping forecast, normal maintenance | Existing API/data/lag/retention/image identity meaning unchanged |
| Archive | Apply survivor canonical deltas with source, G7 removal before archive | No cold ADDED promotion; no nonexistent canonical cold capability deletion; final archive separately reviewed only after actual R5 closure |

R3.7 governance env cold block already retired with R1.3: reconcile proof, do not redo. Current role guidance must distinguish source retirement from pending effective privilege disposition. Original receipts/fixtures/reviews/probe history are not rewritten; runtime synthetic schema examples/dead tests are not historical exceptions merely because old.

R4 also closes the retained workload runbook's digest-reconstruction omission:
normalize captured SQL with `" ".join(sql.split())`, pair it with the published
typed `query.parameters` envelopes, serialize compact key-sorted JSON with
`ensure_ascii=False` and `allow_nan=False`, encode UTF-8, then SHA-256.
Current storage guidance must agree with the surviving `query_digest_preimage`;
do not alter its tests or immutable history. Verify both committed lane receipts
independently reproduce their digests using those exact steps, without adding
source-text pins or a new runtime contract.

Boundary checklist: deleted exported symbols and every caller; non-Python deployable/manual entry; security SQL/audits; shared test/CI closures; survivor identity/error/lock boundaries; canonical capability deletion; historical source vs active authority.

## Risk packs


Explicit mixed-consumer census (apply at the first deletion affecting each
consumer, including the R2 cold-runner cutover, not after a broken merge):
- `test_node27_connection_attribution.py` and
  `test_node27_connection_attribution_delegated.py`: remove only cold CLI import,
  registered component/invoker/delegated-closure/map entries when the CLI retires;
  preserve ordinary component attribution and all surviving delegated connects.
- `test_timeseries_storage_schemas.py`: remove cold receipt SCHEMA_BASES entry
  and cold example-validation cases with their schemas/examples; keep ordinary
  compression/retention schema behavior.
- `test_node27_write_roles.py`: remove cold CREATE positive test and cold
  SET TABLESPACE source-scan test, remove deleted cold paths from
  _CONVERTED_LANE_SOURCES; keep other ownership/flags/membership/trigger/default/
  deny-write behavior. Do not replace these with new source-text absence pins.
- Selector/expected-map closure includes tests/conftest.py selecting the new
  Docker collection-gate owner, and lifecycle rule dropping cold CLI tests while
  preserving all surviving suites. Each mixed suite must collect and execute.
- `docs/runbooks/node-27-database-container-operations.md` §5 is also a manual
  consumer: withdraw its current installer/CREATE procedure explicitly, reference
  surviving PGDATA/governance and R5.2 privilege/deployment boundary. Preserve
  normal container restart/stats operations and immutable receipts; the old
  do-not-execute-live warning alone is not retirement disposition.
Selected: CLI (real deletion), config (removed activation), file IO/delete (classified source-only scope), schema (retired contracts/examples), auth (source role grant/audits), concurrency (surviving mutex tests), resource/discovery (maintenance unchanged), legacy (protective refusals/history), errors (no masked imports/fallback), packaging (all selector/collection edges), docs (manual consumers). Domain selected: Timescale maintenance/roles and published display identity preservation. Not selected: geospatial, forcing semantics, SHUD/numerics, Slurm, providers, manifest/QC: unchanged.

## Evidence floor

Parent runs validation after all integrated edits; workers skip tests/build/lint/formatters.
- Per-path deletion/transfer/survivor/history/negative-guard disposition, Python and non-Python scan. Exact current census, not old count95. Include source and manual consumers, not grep count alone.
- Local full Ruff, active/canonical strict OpenSpec and affected Markdown; frontend pnpm test for independent C4.
- Node27 owned isolated checkout/venv/TMPDIR: PGDATA command/container/evidence/migrate, governance/working-set/maintenance, workload, compression/retention/lifecycle, write roles, external contract, selector, readonly and C4 suites; new Docker collection gate actually executes.
- Actual isolated PostgreSQL15.2/Timescale2.10.2 PGDATA oracle with triple marker opt-in and assertion execution, plus normal compression wrapper launch/refusal and workload EXPLAIN. No production DB tests, no all-skipped proof.
- Node27 backend default full, actual entropy audit against final tracked tree, exact-head CI, high-risk cross-review/verifier/final gap sweep.
- R4 canonical survivor deltas land with corresponding source closures. Pending R5.2 stays explicit until separately authorized effective unit/dropin/env/source-pin handoff; then reviewed archive and #2293/#2298/#1938 applicability disposition before #1895 then #1891 closure.

No live DROP/REVOKE, no data/PGDATA/private evidence deletion, no node22, no cold sample waits or blanket I8 dependency. Unexpected deployed cold state requires separately approved safe disposition. Source-complete is not epic-complete.
