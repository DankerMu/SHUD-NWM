## ADDED Requirements

### Requirement: Cold generation statement timeout is a retryable busy response

When a statement executed inside the cold generation gate is cancelled with SQLSTATE `57014`, the MVT route SHALL respond 503 `MVT_COLD_GENERATION_BUSY` with the same `Retry-After` and `Cache-Control: no-store` headers as a saturated gate, SHALL release its permit and DB checkout, SHALL NOT write any cache entry, and SHALL log a WARNING that distinguishes the timeout from gate saturation. Any other exception, including an `OperationalError` with a different or absent SQLSTATE, SHALL propagate unchanged.

#### Scenario: Producer cancelled by statement timeout
- **WHEN** the tile producer raises `sqlalchemy.exc.OperationalError` whose `orig.pgcode` is `57014`
- **THEN** the response is 503 `MVT_COLD_GENERATION_BUSY` with `Retry-After: 1`, the permit is available to the next request, and no cache write happened

#### Scenario: Database unreachable
- **WHEN** the tile producer raises `sqlalchemy.exc.OperationalError` whose `orig.pgcode` is `08006` or absent
- **THEN** the same `OperationalError` propagates and no 503 busy response is produced
