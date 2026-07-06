## MODIFIED Requirements

### Requirement: Backend scheduler entrypoint

The system SHALL provide a backend scheduler entrypoint that can run once or
continuously and create production forecast work for all selected registered
basins. Production live passes SHALL make bounded progress, release their lock
on completion or stable blocker, and remain business-runnable on node-22 in
DB-free mode under configured concurrent submission.

#### Scenario: downstream output stage cannot mask missing forecast output

WHEN a candidate has a failed downstream output-dependent stage such as
`state_save_qc`
AND the candidate has no durable SHUD forecast output to reuse
THEN the scheduler SHALL classify the candidate for native forecast recompute
with `restart_stage=forecast`
AND it SHALL NOT permanently block the candidate solely because the downstream
stage exhausted its retry limit.

#### Scenario: all registered basins business-ready on node-22

WHEN node-22 runs the compute scheduler for the current registered basin set
THEN every selected basin candidate SHALL either reach terminal business success
or carry a specific actionable blocker
AND a generic downstream `NODE_FAILURE` caused by missing forecast output SHALL
NOT be accepted as normal business readiness.
