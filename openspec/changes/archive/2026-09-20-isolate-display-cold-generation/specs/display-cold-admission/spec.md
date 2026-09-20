## ADDED Requirements

### Requirement: Cold generation preserves fast-request capacity
Every MVT route SHALL use a process-wide bounded cold admission gate with effective limit strictly less than its display pool connection ceiling. A rejected request and a request waiting for same-key generation SHALL NOT retain a checked-out DB connection. An admitted request SHALL release its DB transaction before returning its permit. Cache hits SHALL not consume a cold permit. Existing identity, cache, permission and business-error contracts SHALL remain unchanged.

#### Scenario: Saturated distinct cold requests
- **WHEN** real checked-out cold requests occupy the configured cold limit and another cold key arrives alongside a cached tile and a layers request
- **THEN** the extra cold key returns 503 MVT_COLD_GENERATION_BUSY with Retry-After: 1 and no-store, while cached tile and layers return 200 without QueuePool timeout.

#### Scenario: Same-key follower and failed producer
- **WHEN** an admitted request waits behind a same-key producer, or a producer/cache probe raises
- **THEN** waiting holds no DB checkout, permits and transactions are released on termination, and subsequent fast and cold requests can proceed; single-flight cache identity remains correct.

#### Scenario: Minimal or overconfigured capacity
- **WHEN** pool capacity is one or the requested cold limit is at least capacity
- **THEN** the effective cold limit remains below capacity, with zero cold admission at capacity one, and no invalid configuration creates an unbounded or full-pool cold gate.

#### Scenario: Overload response contract
- **WHEN** any of the six MVT routes rejects cold work
- **THEN** the typed error envelope, canonical request-id, 503, Retry-After and no-store headers are observable and the declared OpenAPI agrees.

### Requirement: Joint node-27 deployment is evidence-bound
The A and #2346 step-two configuration/code cutover SHALL be a single deployment and rollback unit after the committed cold baseline. It SHALL record connection capacity, role statement timeout/parallelism before and after, relevant EXPLAIN results, cold burst latency and accepted/rejected counts, and post-prewarm full-axis cache behavior.

#### Scenario: Same-worker cold burst and warmed playback
- **WHEN** GFS/IFS switching and 4x-equivalent timeline traffic plus prewarm load hit the same API worker set
- **THEN** river/cache-hit/catalog 500 counts are zero, hot tiles and catalog remain 200, cold requests have useful successes and any excess uses the declared 503 policy, and after complete prewarm full-axis replay is predominantly cache hits without 500s.

#### Scenario: Role and rollback safety
- **WHEN** the candidate is deployed
- **THEN** pool 8+8 and 2 workers are capacity-admitted, cold limit 8 and role timeout=30s/parallel ceiling=2 take effect together, display write denial and disabled Slurm remain enforced, and recorded prior code/env/role settings can be restored together.
