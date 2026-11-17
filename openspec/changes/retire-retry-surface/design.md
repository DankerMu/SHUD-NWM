## Design — retire-retry-surface (#3179)

Change surface:
- `apps/frontend/src/components/monitoring/JobsTable.tsx` and `MonitoringPage.tsx`, with their tests and the mocked
  e2e;
- `apps/api/routes/pipeline.py` (retry `response_model`), `apps/api/response_models/pipeline.py` and
  `apps/api/openapi_patching_pipeline.py`;
- `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts`;
- the contract tests;
- operator runbooks.

Must preserve:
- the retry route's status and body sequence, and its zero side effects;
- the `security` on `retryRun`;
- the cancel UI and API;
- the deny-write probes that post to the retry path.

Must change:
- no retry control in any mode;
- no `RetryRunResult` schema or 2xx response anywhere in the published contract.

Governing invariant: the page offers only controls that can succeed. The retry endpoint is the documented 409 manual-action stub, and the contract never promises a success it cannot return.

Sibling surfaces:
- `display-control-mutation-guard` and `rbac-policy-enforcement`: no behaviour change, the stub is still checked.
- `apps/api/openapi_patching_security.py:33` and `tests/test_slurm_gateway_openapi_security.py`: the operation still
  exists, so `security` stays.
- Deny-write probes: `tests/test_two_node_e2e_evidence.py:9473`, `services/production_closure/ops_validation.py:2434`
  and `playwrightConfig.test.ts:187-193`. They are unchanged.
- The marker lane, which is the real recovery path: not touched.
- The entropy path/API audit (`scripts/governance/entropy_audit/check_paths_and_api.py`) reads the static yaml, so the
  hard gate covers it.

Seats:
- correctness: frontend control removal and contract patch;
- test evidence + spec compliance;
- invariant/compatibility: the retry route sequence, probes and security.

Review focus:
1. Every shared `runAction` case (`:163`, `:267`, `:301`, `:339`, `:567`, `:591`, `:619`, `:650`) now runs through cancel and still tests the same guard, toast and refresh logic.
2. The OpenAPI operation has no 2xx at all, including FastAPI's default, and the static yaml equals the runtime.
3. Nothing that posts to the retry path for deny-write evidence was removed.
4. The mocked e2e keeps the controlled-failure → recovered evidence through `recoverViaMarkerLane()` plus refresh.
5. The response-model oracles: the retry stub is exempted explicitly (`NoReturn`, `response_model=None`), not by
   loosening the general rule.

Deviation: the route and operation are kept as the 409 stub in every mode. The issue asked for removal; see
proposal.md "Deviation from the issue".
