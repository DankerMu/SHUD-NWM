# Tasks — retire-retry-surface (#3179)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Public API / CLI / script entry | yes | `retryRun` loses its unreachable 2xx and `RetryRunResult`; static and runtime OpenAPI, generated types: E2, E3 (`check:api-types`), E4 |
| Legacy compatibility / examples | yes | route kept as the 409 stub, deny-write probes unchanged, cancel UI preserved, marker-lane e2e keeps the controlled-failure evidence: E2, E3, E5 |
| Auth / permissions / secrets | yes | `pipeline.retry_run` kept; operation `security` kept: E2 (auth matrix, openapi security) |
| Error handling / rollback / partial outputs | yes | shared `runAction` error, toast and finally-refresh path rewritten onto cancel: E3, E4 (finally-refresh mutation) |
| Documentation / migration notes | yes | four runbooks point to the marker lane: E7; 6 spec deltas: E6 |
| Schema / columns / units / field names | no | no DB or payload schema; `retry_count` column unchanged |
| Others | no | no IO, config, concurrency or migration change. The runtime change is the frontend control removal plus the contract patch, both covered above |

- [ ] 1.1 Frontend: remove the retry control and its prop at every caller (What 1).
- [ ] 1.2 `JobsTable.test.tsx`: move the shared-path cases onto cancel and add the no-retry case (What 2). Rework the
  mocked e2e with marker-lane recovery (What 3).
- [ ] 1.3 API contract: handler shape, schema and model removal, patch opt-out, static yaml, regenerated types
  (What 4). Update the pinned oracles (What 5).
- [ ] 1.4 Docs (What 6).
- [ ] 1.5 Evidence: E1-E4 and E7 locally.
- [ ] 1.6 E5 on node-27 (orchestrator), E6.
