## MODIFIED Requirements

### Requirement: retry classification

The system SHALL classify retryable job failures using stage-aware evidence so
that retries resume from the earliest required safe stage.

#### Scenario: downstream retry is widened when upstream forecast output is absent

WHEN a downstream stage fails with a transient runtime error
AND the scheduler cannot prove durable forecast output exists for the candidate
THEN the retry classifier SHALL widen the retry to the native forecast stage
AND the retry evidence SHALL expose the widening reason as
`missing_forecast_output_recompute`.
