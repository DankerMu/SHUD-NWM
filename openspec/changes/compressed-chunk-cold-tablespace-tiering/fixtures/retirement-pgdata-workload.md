# R1.4 retained PGDATA workload fixture

Issue #1895 / epic #1891. Upstream tasks.md remains sole executable contract.
Suggested expanded accepted; repair intensity high. This slice completes R1.4 and matching R1.5/R4 only; R2/R3/R5 remain separately gated.

## Scope and invariant

Minimal mergeable slice: PGDATA-owned explicit-cycle SQL capture, canonical parameter validation, native named EXPLAIN execution and deterministic evidence identity, with real CLI, behavior tests, selector ownership and retained manual instructions. Do not migrate four-lane G7 protocol, positional compatibility, cold catalog or publication acceptance.
Governing invariant: measurements execute exactly the shipping explicit-cycle query and canonical cycle/run/model/segment bindings captured for the requested workload; drift refuses before SQL execution or evidence acceptance. Isolated measurements never imply live acceptance.

Must preserve shipping forecast_series API and named psycopg binding, readonly validator, independent C4 producer/binder and Bringup-C4 acceptance, PGDATA before/after workload and controlled-ingest authorization. Existing old recorder callers remain until R3 atomic deletion; no new facade or re-export. Transfer minimal behavior into a PGDATA owner, not entire performance_live/query modules. Use existing private IO/config conventions and existing schema patterns where needed; no broad new evidence framework.

## Concrete owner cutover and retained measurement contract

- Public owner: `packages/common/node27_pgdata_workload.py`; supporting query,
  plan/measurement and IO modules use the `node27_pgdata_workload_` prefix and
  existing module-size limits. Public CLI: `scripts/node27_pgdata_workload.py`.
  Refusal type: `PgdataWorkloadError`, independent of Issue1895ReadinessError,
  with stable QUERY_*/PLAN_*/SQL_*/API_* codes translated at this CLI.
- Retain the old issue1895 implementations and public errors only for still-live
  G7 callers until R3.6; do not migrate those callers in this owner-transfer
  slice, add re-exports or make the new owner depend on the old one. Extract only
  required named-query/measurement semantics; R3 removes the old implementation.
- Capture through `_CaptureForecastStore`-style adapter at `_transaction` and
  `_validate_series_target`, invoking real `forecast_series` with q_down,
  include_analysis=False and run_types=['forecast']. Do not clone shipping SQL.
  Preserve expected RUN_NOT_PUBLISHED capture behavior; selected_cycles,
  zero/multiple primary statements and noncanonical bindings refuse.
- Measurement discards exactly one warmup and accepts 20 serial samples of the
  identical captured named query and identical shipping forecast-series API
  request. Nearest-rank P95 is sorted sample index18. SQL P95 <=300ms, local
  API P95 <=500ms; buffers are root Plan Shared Hit+Read <=5000, never summed
  parent/child counters. Structural plan validation refuses relevant fact Seq
  Scan and unrelated chunk access/all-chunk decompression; its own candidate
  DecompressChunk is allowed. Candidate relation identities come from a bounded
  PGDATA-owned window/catalog loader, not cold topology or freeze_lanes.
- Validate API source/cycle/run/model/segment/window and response content, not
  merely HTTP200. Bound body/JSON/time and reject redirects away from the local
  loopback origin using existing safe HTTP patterns. Record sample timestamps,
  content digests, query identity, durations/P95 and plans/buffers; no G7 artifact
  version, commit-marker publication, four-lane discovery or cold target.
- CLI `measure` takes `--reader-dsn-file`, `--api-origin`,
  `--basin-version-id`, `--river-network-version-id`, `--segment-id`,
  `--issue-time`, `--run-id`, `--model-id`, `--source GFS|IFS`,
  `--reviewed-sha` and `--output`. Use PGDATA's existing raw private DSN-file
  convention (not display.env or shell sourcing); descriptor-bound mode0600
  nonsymlink read, readonly nhms_display_ro session, connect_timeout5,
  SET LOCAL statement_timeout5000 and lock_timeout2s, PGDATA application name.
  This is an intentional PGDATA-owner input choice instead of copying G7's
  display.env resolver. No unsafe caller-selected SQL or reduced sample mode.
- Register this actual command in the retained workload section of
  `tier-node27-timeseries-storage.md`, replacing pending SQL/API transfer prose.
  Repeat for representative already-present compressed/uncompressed windows,
  same before/after frozen inputs. Browser remains independent C4/river-click
  with click P95<2s; readonly remains its existing validator and ingest remains
  §D's separately authorized OLD-runtime operation. Do not point operators to
  withdrawn performance_oracle or manufacture selective-cold samples.
- Every new workload producer/CLI rule selects the new owner suites plus
  `test_forecast_api.py`, `test_forecast_store_routing.py`,
  `test_select_ci_tests.py`. Add new capture consumer suites to forecast_store's
  producer rule too; update selector expected maps atomically. Extract named
  binding/digest/drift behavioral tests into the new owner suites now; leave
  positional/G7 tests in ISSUE2227 until R3.6. No new G7 caller imports here.
- Additional regression rows: warmup excluded; P95 uses zero-based sorted
  index18 of the 20 accepted durations, never unsorted sample order;
  threshold equality accepted and above-threshold refused; root buffers not
  double-counted; own compressed candidate allowed while unrelated chunk or
  relevant Seq Scan refuses; API wrong identity/empty series/redirect refuses;
  CLI executes the complete measurement producer, not capture-only EXPLAIN.
  Actual isolated API+DB invocation and 1+20 evaluation are required alongside
  pure boundary tests; each receipt labels isolated versus live explicitly.

## Invariant matrix and boundaries

| Surface | Concrete seam | Scenario and expected result |
| --- | --- | --- |
| Producer | forecast_store.PsycopgForecastStore.forecast_series capture and PGDATA SQL/API sampler | Real named query captured once; canonical identity matches; zero/multiple primary queries refuse; discard one warmup and measure 20 accepted serial SQL/API samples |
| Validator | PGDATA binding/digest, structural plan and SQL/API evaluation | Missing/extra/mixed/positional/wrong bindings refuse; mapping order preserves digest; relevant Seq Scan/unrelated chunk refuses, own candidate allowed, root buffers<=5000, sorted-index18 SQL P95<=300ms/API P95<=500ms; loopback API identity/content and redirect checks hold |
| Query | Native psycopg EXPLAIN with same SQL/mapping throughout complete measurement | Isolated PostgreSQL/Timescale runs full 1+20 measurement with native named binding, bounded readonly transaction and cleanup, no interpolation or positional rewrite |
| Entry | scripts/node27_pgdata_workload.py measure | Actual CLI executes complete SQL/API measurement producer and emits attributable isolated/live-labeled evidence; malformed identity/config refuses without fabricated PASS or exposed DSN |
| Evidence | SQL, typed parameters, query identity and measurement output | Frozen inputs cannot be silently rederived from changed evidence; deterministic identity survives serialization with explicit datetime handling; bounded private IO and no-clobber output |
| Consumers | storage runbook retained workload section; CI selector | Document actual executable command and separate SQL/API/browser/ingest evidence owners; new owner selects existing assertion-bearing suites including shared forecast producer edges |
| Failures | DB failure, unsafe output/config, binding drift | Stable secret-safe error, closes cursor/connection, no success evidence or partial publish |
| Siblings | readonly_db_validation, independent C4 and unretired G7 callers | No interface break or change to shipping API, original C4 schema or legacy callers in this owner-transfer slice |

Boundary checklist: capture adapter shared forecast seam; explicit-cycle canonical identity; config/credential reads; bounded readonly query; private evidence publish; manual before/after consumers; selector producer closure. No live services, ingest, migration, data deletion or privileges changed.

## Risk packs

Selected: public CLI (real invocation/refusal); config/setup (safe private credentials); file IO/path safety (bounded private input and no-clobber receipt); schema/fields (typed query identity); auth/secrets (readonly and redaction); concurrency/ordering (validate before execute/publish); resource limits (statement timeout and bounded capture/output); legacy/examples (retained manual contract, old callers intact); errors/partial output (no false PASS, cleanup); release/dependencies (all caller/selector edges); docs/migration (actual owner instructions).
Domain selected: PostGIS/Timescale (real shipping SQL), published NHMS/display identity (canonical cycle/run/model/segment). Not selected: geospatial/CRS, forcing windows beyond unchanged forecast semantics, SHUD/numerics, Slurm, external providers, manifest/QC: untouched.

## Evidence floor

Parent runs checks after implementation, never concurrent agent validation.
- Local Ruff, active OpenSpec strict and affected canonical validation; scoped Markdown and frontend pnpm test for surviving C4/manual consumers.
- Node-27 isolated checkout/venv, owned TMPDIR under /home/nwm/tmp; capacity check / /home /data/GHDC before tests. No tests in production DB or active venv.
- New owner behavioral suite plus existing forecast named-binding, readonly, PGDATA, selector and C4 suites selected by actual closure. Mutation/red proof for uncertain binding, identity and publication refusal boundaries.
- Actual CLI `measure` executes one warmup plus 20 accepted SQL and API samples against disposable PostgreSQL 15.2/TimescaleDB 2.10.2 and its isolated shipping API. Verify plan/P95/content/query identity and isolated-vs-live receipt labeling, plus wrong API identity/content, redirect and unsafe config refusal. Capture/binding/digest checks are additional proof, never a substitute for this full producer path; no all-skipped, single-EXPLAIN-only or mock-echo proof.
- Backend default full on node-27; final tracked-tree entropy audit, exact-head PR CI and independent review. Record isolated vs live and marker skip boundaries explicitly.
- Apply Retained SQL performance evidence SHALL keep query identity ADDED requirement to canonical node27-pgdata-relocation with source delivery; do not apply unrelated pending deltas or archive the epic.

No automatic merge authorization or production handoff is inferred from fixture approval. R5.2 remains a separate effective-deployment authorization gate. No deviations may be hidden; new seams needed beyond this fixture must be reported before implementation changes the contract.
