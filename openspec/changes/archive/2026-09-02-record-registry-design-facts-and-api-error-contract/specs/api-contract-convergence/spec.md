## ADDED Requirements

### Requirement: Declared error responses cite reachable raise sites

Every error response declared on an operation in `openapi/nhms.v1.yaml` SHALL correspond to a status code the runtime can actually return from that operation, and the runtime schema patch in `apps/api/openapi_patching.py` SHALL declare the same response so `tests/test_openapi_drift.py` keeps static and runtime equal. An operation with no reachable 4XX SHALL NOT declare one to satisfy a lint rule; the retained `operation-4xx-response` warning is recorded in the change's `tasks.md` with the raise-site audit that justifies it. No lint rule is skipped or ignored to reduce the warning count.

#### Scenario: Queue depth declares its reachable gateway failures

- **WHEN** the static contract and `app.openapi()` are compared for `GET /api/v1/queue/depth`
- **THEN** both declare `503` with error code `CONTROL_PLANE_QUEUE_UNAVAILABLE`, `502` with error codes `SLURM_COMMAND_ERROR` and `SLURM_PARSE_ERROR`, and `504` with error code `SLURM_TIMEOUT`
- **AND** `tests/test_api_contract.py` asserts the three code sets are equal between static and runtime

#### Scenario: Operations without a reachable 4XX declare none

- **WHEN** `redocly lint openapi/nhms.v1.yaml --skip-rule no-unused-components` is run at the pinned CLI version
- **THEN** it reports 0 errors
- **AND** the only warnings are `operation-4xx-response` on `GET /api/v1/queue/depth`, `GET /api/v1/slurm/health`, `GET /health`, and `info-license` on `#/info`
- **AND** `tasks.md` records the per-operation reason no 4XX is reachable and that `info.license` awaits an owner decision
