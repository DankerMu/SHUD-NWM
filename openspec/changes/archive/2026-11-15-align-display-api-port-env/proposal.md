## Why

The display startup script reads `NHMS_DISPLAY_PORT` while the display env
template, compose file, and runtime validation use `NHMS_DISPLAY_API_PORT`.
Operators who set the documented variable can be silently ignored by the
hand-launch wrapper.

## What Changes

- Align `scripts/ops/start-display-api.sh` to read `NHMS_DISPLAY_API_PORT`.
- Align display template and compose defaults to node-27's production port
  `8080`.
- Update tests/docs that assert the display API port variable/default.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `two-node-docker-runtime`: display API port configuration uses
  `NHMS_DISPLAY_API_PORT` consistently.
- `production-ops-readiness`: display restart wrapper uses the same documented
  port env variable as compose and env templates.

## Impact

- `scripts/ops/start-display-api.sh`
- `infra/env/display.example`
- `infra/compose.display.yml`
- `docs/runbooks/two-node-deployment-overview.md`
- tests covering two-node docker/display runtime env contracts
