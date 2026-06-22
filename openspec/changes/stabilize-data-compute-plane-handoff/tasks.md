## 1. Forcing-Domain Handoff

- [x] 1.1 Inventory the exact forcing-domain data required by node-27 display readiness, including `met.forcing_version`, `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`, and document the object-store handoff manifest fields/checksums needed to reconstruct them.
  Evidence: complete fixture input path validates required identity fields, `start_time`/`end_time`, payload checksums, station count, and per-table row-count output.
- [x] 1.2 Add fixture coverage for a complete object-store forcing-domain handoff package and at least one incomplete package with a stable unavailable reason.
  Evidence: missing payload/checksum/temporal fields produce named stable unavailable reasons, traversal/sibling package URIs are rejected without broad root scans, and validation output contains no credential-like values.
- [ ] 1.3 Harden `scripts/node27_mirror_forcing.py` so transitional mirror mode requires `--node22-url` or `N22_DSN`, never reads `infra/env/display.env`, and returns credential-safe structured evidence.
- [ ] 1.4 Update `scripts/node27_autopipeline.py` to treat the explicit mirror as transitional compatibility, with skip/fail summaries that preserve run-level failure isolation.
- [ ] 1.5 Implement the object-store forcing-domain contract parser/fixture reader that reads package manifests and station payloads, returns structured reconstruction evidence, and does not change autopipeline behavior.
- [ ] 1.6 Implement the object-store forcing-domain DB apply path that idempotently writes or verifies `met.forcing_version`, `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`, covering complete package, missing field, checksum mismatch, and credential-safe failure reasons.
- [ ] 1.7 Update `scripts/node27_autopipeline.py` to prefer object-store forcing-domain import for contracted packages before explicit transitional mirror fallback, covering skip/fail summaries and one-run failure isolation.
- [ ] 1.8 Verify qhh and heihe forcing/display readiness on node-27 without relying on an implicit node-22 DB or display-env mirror fallback, and record evidence paths without secrets.

## 2. Node-27 Ingest Boundary

- [ ] 2.1 Add a committed node-27 ingest env template or wrapper contract and wire the actual cron/on-demand entry (`scripts/node27_autopipe_cron.sh`) to source or validate ingest-specific writer configuration instead of deriving it from `infra/env/display.env`.
- [ ] 2.2 Add preflight checks for writer `DATABASE_URL`, `OBJECT_STORE_ROOT`, `BASINS_ROOT`, work/log roots, and secret redaction before `node27_autopipeline.py` starts seed/import/activate/backfill/register/mirror/parse/coverage/publish work.
- [ ] 2.3 Add role/evidence fields to node-27 ingest summaries so operators can distinguish data-plane ingest health from display API health.
- [ ] 2.4 Add tests covering preflight-before-seed, missing env, basin seed failure isolation, one-run failure isolation, already-ingested skip behavior, and credential-safe logs.
- [ ] 2.5 Capture a node-27 live receipt showing ingest writer checks and display API `display_readonly` checks remain separate.

## 3. Production Topology Contract

- [ ] 3.1 Update current operational docs so node-22 is compute/artifact producer, node-27 is active DB/ingest/display host, and historical node-22 DB material is explicitly non-current.
- [ ] 3.2 Add or extend static governance checks that flag active node-22 DB writer assumptions and display-env reuse for data-plane writer or mirror jobs.
- [ ] 3.3 Update verification/oracle routing docs so local checks, node-27 live DB/display checks, and node-22 Slurm checks are not conflated.
- [ ] 3.4 Add focused tests or scripted checks for the new topology drift guardrails, including positive fixtures for active node-22 writer/display-env-writer drift and negative fixtures for historical/archive/compatibility-only text and display API readonly env use.

## 4. End-to-End Evidence

- [ ] 4.1 Run focused local tests for the forcing handoff, node-27 ingest boundary, and topology guardrails.
- [ ] 4.2 Run `uv run ruff check .`, `openspec validate stabilize-data-compute-plane-handoff --strict --no-interactive`, and relevant docs lint.
- [ ] 4.3 Fast-forward node-27, run the live ingest/display receipt, and record qhh/heihe evidence paths without secrets.
