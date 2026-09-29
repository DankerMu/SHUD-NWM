## ADDED Requirements

### Requirement: MVT cold-generation busy tiles are retried per Retry-After
The M11 map SHALL load its MVT vector sources (the discharge overlay and the national river network) through a tile loader that retries a tile answered with `503` when that response carries a `Retry-After` header or an error body whose `error.code` is `MVT_COLD_GENERATION_BUSY`. The loader SHALL wait the `Retry-After` delay (clamped to a bounded range, with small jitter) and SHALL retry at most 3 times. Every other response status, and a busy tile whose retries are exhausted, SHALL fail exactly as a default MapLibre fetch would (an `AJAXError` preserving the HTTP status, so a `404` still reads as an empty tile). An aborted tile request SHALL cancel any pending wait and SHALL NOT issue further requests. The MVT URL template, `sourceKey` identity and cache-version query SHALL be unchanged.

#### Scenario: busy tile succeeds on retry
- **WHEN** a discharge tile request returns `503` with `Retry-After: 1` and body code `MVT_COLD_GENERATION_BUSY`, and the next attempt returns `200`
- **THEN** the loader waits about one second and resolves the tile with the second response's bytes
- **AND** no map error event is raised for that tile

#### Scenario: retries exhausted
- **WHEN** every attempt for a tile returns the busy `503`
- **THEN** the loader stops after 3 retries and fails the tile with an `AJAXError` whose status is 503

#### Scenario: non-busy failures are not retried
- **WHEN** a tile request returns `404`, `500`, or a `503` without `Retry-After` and without the busy code
- **THEN** the loader issues exactly one request and fails with an `AJAXError` carrying that status

#### Scenario: abandoned tile stops retrying
- **WHEN** MapLibre aborts a tile while its retry wait is pending
- **THEN** the wait is cancelled, the loader rejects with an abort error, and no further request is sent
