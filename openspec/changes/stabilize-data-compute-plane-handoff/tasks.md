## 1. Forcing-Domain Handoff

- [x] 1.1 Inventory the exact forcing-domain data required by node-27 display readiness, including `met.forcing_version`, `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`, and document the object-store handoff manifest fields/checksums needed to reconstruct them.
  Evidence: complete fixture input path validates required identity fields, `start_time`/`end_time`, payload checksums, station count, and per-table row-count output.
- [x] 1.2 Add fixture coverage for a complete object-store forcing-domain handoff package and at least one incomplete package with a stable unavailable reason.
  Evidence: missing payload/checksum/temporal fields produce named stable unavailable reasons, traversal/sibling package URIs are rejected without broad root scans, and validation output contains no credential-like values.
- [x] 1.3 Harden `scripts/node27_mirror_forcing.py` so transitional mirror mode requires `--node22-url` or `N22_DSN`, never reads `infra/env/display.env`, and returns credential-safe structured evidence.
  Evidence: `tests/test_node27_mirror_forcing.py` covers no-DSN rc 2 JSON skip without display-env fallback, explicit CLI/env `N22_DSN` source selection, transitional compatibility/read-only boundary evidence, and redaction of accidental credential-bearing DSN strings.
- [ ] 1.4 Update `scripts/node27_autopipeline.py` to treat the explicit mirror as transitional compatibility, with skip/fail summaries that preserve run-level failure isolation.
- [x] 1.5 Implement the object-store forcing-domain contract parser/fixture reader that reads package manifests and station payloads, returns structured reconstruction evidence, and does not change autopipeline behavior.
  Evidence: `parse_forcing_domain_handoff_path(...)` added in `packages/common/forcing_domain_handoff.py`; focused tests in `tests/test_forcing_domain_handoff_contract.py` cover complete parsed table keys/counts/fields/checksums, station coordinate evidence as longitude/latitude or geometry, unavailable handoff checksum evidence when the handoff manifest is readable, missing required field, missing payload, malformed payload, checksum mismatch, credential-safe unavailable results with empty `parsed`, unsafe traversal/sibling-package rejection, oversized manifest/package/payload reads, lattice-too-large empty `parsed`, parser-only finite `elevation_m` station rows, successful parsed business-value preservation for nested grid signatures, and unchanged #641 validator test expectations. No `scripts/node27_autopipeline.py` changes were verified by orchestrator diff evidence, not by a focused parser test. Round-3 verification passed: `uv run pytest -q tests/test_forcing_domain_handoff_contract.py` (78 passed, 1 skipped), `uv run ruff check packages/common/forcing_domain_handoff.py tests/test_forcing_domain_handoff_contract.py`, `openspec validate stabilize-data-compute-plane-handoff --strict --no-interactive`, and `git diff --check`.
  Evidence floor: focused parser tests cover the complete fixture path input -> public parser envelope with `available/status/unavailable_reasons/evidence/parsed`, exact `parsed.met.forcing_version` row shape including `checksum` from `forcing_package_manifest_checksum_sha256`, exact `parsed.met.met_station` row keys with required business fields plus coordinate evidence (`longitude`+`latitude` or `geometry`) and optional geometry preservation, exact `parsed.met.forcing_station_timeseries`/`parsed.met.interp_weight` row keys, expected row counts and payload checksums, missing required field, missing payload, malformed payload, checksum mismatch, credential-safe unavailable reasons with no parsed rows, unsafe traversal/sibling-package URI rejection, no object-store escape, oversized/large-input behavior, bounded package/run discovery without broad object-store scans, unchanged #641 validation output, and no `scripts/node27_autopipeline.py` changes.
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
