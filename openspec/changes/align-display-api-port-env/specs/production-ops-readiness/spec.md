## ADDED Requirements

### Requirement: Display API restart wrapper uses documented port env

The node-27 display API restart wrapper SHALL read the display API port from
`NHMS_DISPLAY_API_PORT`, matching `infra/env/display.example` and
`infra/compose.display.yml`. The wrapper SHALL default to port `8080` when the
documented env var is unset.

#### Scenario: documented display API port controls wrapper

- **WHEN** `infra/env/display.env` contains `NHMS_DISPLAY_API_PORT=18080`
- **THEN** `scripts/ops/start-display-api.sh` SHALL launch uvicorn with
  `--port 18080`
- **AND** the script SHALL NOT read `NHMS_DISPLAY_PORT` as the active port
  configuration variable

#### Scenario: old display port alias is ignored by wrapper

- **WHEN** `infra/env/display.env` contains `NHMS_DISPLAY_API_PORT=18080`
- **AND** the same file or process environment also contains
  `NHMS_DISPLAY_PORT=19090`
- **THEN** `scripts/ops/start-display-api.sh` SHALL launch uvicorn with
  `--port 18080`
- **AND** it SHALL NOT launch with `--port 19090`

#### Scenario: default display API port matches node-27 runtime

- **WHEN** no explicit display API port env var is configured
- **THEN** the restart wrapper and compose host bind SHALL default to `8080`
