## Context

`infra/env/display.example` and `infra/compose.display.yml` expose
`NHMS_DISPLAY_API_PORT`, while `scripts/ops/start-display-api.sh` currently reads
`NHMS_DISPLAY_PORT`. Production binds node-27 display API on `127.0.0.1:8080`.

## Goals / Non-Goals

**Goals:**

- Use `NHMS_DISPLAY_API_PORT` as the single display API port variable.
- Keep the production-compatible default at `8080` for both hand-launch and
  compose-local binding.
- Preserve existing host default (`127.0.0.1`) and restart smoke behavior.

**Non-Goals:**

- No systemd, nginx, or public reverse-proxy change.
- No display API route/runtime behavior change.
- No node-27 live restart required for the repository fix.

## Decisions

- Prefer `NHMS_DISPLAY_API_PORT` over `NHMS_DISPLAY_PORT` because compose,
  display.example, static checker allowlists, and issue evidence already name
  `NHMS_DISPLAY_API_PORT`.
- Set template/compose fallback to `8080` to match the current node-27 runtime
  and `start-display-api.sh` default.
- Keep container uvicorn command on `--port 8000`; `NHMS_DISPLAY_API_PORT`
  controls host bind in compose, while the script launches host uvicorn directly
  on the configured port.

## Risks / Trade-offs

- Operators with a private untracked `display.env` that only sets
  `NHMS_DISPLAY_PORT` will now use the documented default `8080`. Mitigation:
  the documented variable has been `NHMS_DISPLAY_API_PORT`; the old variable was
  the bug and is intentionally not carried as a hidden alias.

## Risk Fixture

Issue type: bugfix
Project profile: NHMS
Blast radius: medium
Fixture level: expanded
Repair intensity: high
Why:
- Production display restart wrapper and compose/env defaults are operator entry
  surfaces.
- The original failure path is sourced `infra/env/display.env` being ignored for
  port selection, not only an ambient process env mismatch.
- No public API/schema/data behavior change.

Risk packs:
- Public API / CLI / script entry: selected - `start-display-api.sh` operator
  wrapper must source `display.env` before deriving the uvicorn port.
- Config / project setup: selected - display env and compose defaults must
  align.
- Documentation / migration notes: selected - runbook default port wording must
  match.
- Legacy compatibility / examples: selected - static checker tests must reject
  the old alias and accept the documented key.
- Error handling / rollback / partial outputs: selected - invalid or incomplete
  `display.env` must still fail before the wrapper stops/relaunches uvicorn.
- File IO / path safety / overwrite: not selected - no file path behavior.
- Schema / columns / units / field names: not selected - no payload schema.
- Auth / permissions / secrets: not selected - no credential change.
- Concurrency / shared state / ordering: not selected - no process sequencing
  change beyond existing restart wrapper.
- Resource limits / large input / discovery: not selected.
- Release / packaging / dependency compatibility: not selected.
Domain packs (NHMS):
- Published NHMS artifacts / display identity: not selected - display artifact
  identity and API payloads are unchanged.
- PostGIS / TimescaleDB domain behavior: not selected - DB connection semantics
  are unchanged.
- Slurm production lifecycle / mock-vs-real parity: not selected - no node-22 or
  Slurm surface.
- Other NHMS scientific/runtime packs: not selected - no hydro-met data,
  geospatial, provider, or SHUD runtime behavior.

Required evidence:
- `bash -n scripts/ops/start-display-api.sh`
- A shell harness test proving `NHMS_DISPLAY_API_PORT` from sourced
  `infra/env/display.env` controls the launched uvicorn `--port`, and
  `NHMS_DISPLAY_PORT` is ignored.
- Shell harness tests proving invalid `NHMS_DISPLAY_API_PORT` values fail before
  any stop/relaunch/probe action.
- `uv run pytest -q tests/test_two_node_docker_runtime.py`
- grep proves no active `NHMS_DISPLAY_PORT` usage remains outside tests that
  deliberately mutate the old alias.
- `openspec validate align-display-api-port-env --strict --no-interactive`

Invariant Matrix
- Governing invariant: every display API startup surface uses
  `NHMS_DISPLAY_API_PORT` as the single display API host/wrapper port contract.
- Source-of-truth identity/contract: `infra/env/display.env` copied from
  `infra/env/display.example`, plus compose interpolation in
  `infra/compose.display.yml`.
- Producers: `infra/env/display.example`, `infra/compose.display.yml`.
- Validators/preflight: `scripts/validate_two_node_docker_runtime.py`,
  `tests/test_two_node_docker_runtime.py`.
- Storage/cache/query: none - no persisted data or DB schema changes.
- Public routes/entrypoints: `scripts/ops/start-display-api.sh`.
- Frontend/downstream consumers: node-27 reverse proxy expects the API on local
  port `8080`; API routes and frontend code are unchanged.
- Failure paths/rollback/stale state: wrapper env-key preflight and object-store
  checks must still fail before stopping/relaunching uvicorn.
- Evidence/audit/readiness: OpenSpec validation, shell syntax check, runtime
  harness test, static checker tests, ruff, node-27 health after merge.
Regression rows:
- `display.env` contains `NHMS_DISPLAY_API_PORT=18080` and
  `NHMS_DISPLAY_PORT=19090` -> wrapper launches uvicorn with `--port 18080`.
- no explicit display API port in env template/compose -> host wrapper and
  compose bind default to `8080`.
- compose uses `${NHMS_DISPLAY_PORT:-...}` -> static checker rejects the alias.
- missing required `display.env` keys or invalid `OBJECT_STORE_ROOT` -> wrapper
  exits before stopping/relaunching uvicorn, as before.
- `display.env` contains non-numeric or out-of-range
  `NHMS_DISPLAY_API_PORT` -> wrapper exits before stopping/relaunching/probing
  uvicorn and reports `NHMS_DISPLAY_API_PORT`.
