## REMOVED Requirements

### Requirement: POST /api/v1/runs/{run_id}/cancel

**Reason**: The control-plane cancel route is retired (#3394): no production process could serve it (the display API runs read-only; the compute API unit is disabled and never had a database).
**Migration**: None. Work is cancelled by the scheduler on the compute host or through the Slurm gateway's job route; a stuck job row is handled as `docs/runbooks/node22-control-plane-manual-recovery.md` says.

### Requirement: Slurm gateway error evidence SHALL render host paths on ops responses while persisted events keep the raw text

**Reason**: The control-plane cancel route is retired (#3394); four of the five scenarios are about its response and its persisted events, and after it no ops route persists gateway error text. OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Queue-depth upstream errors SHALL render host paths through the public evidence renderer".

## ADDED Requirements

### Requirement: Queue-depth upstream errors SHALL render host paths through the public evidence renderer
When a Slurm gateway error reaches an ops API response body, its `message` and its `details` SHALL be rendered through the shared public evidence renderer, so that absolute host paths become `[local-path]` and URIs become `[uri]`/`[object-uri]`, including inside nested values and list elements such as the executed command's argument vector and the captured stderr snippet. Secret redaction SHALL keep its existing precedence.

This SHALL apply to the queue-depth endpoint's upstream-error body, the one ops response that carries Slurm gateway error text. The response SHALL contain no configured scheduler binary path and no workspace root text.

The upstream-error response schema's field names SHALL NOT change; only the values are rendered. Errors that carry no gateway text, such as the control-plane manual-action error, SHALL keep their current rendering.

#### Scenario: Queue depth upstream error renders host paths
- **WHEN** the queue-depth query fails with a gateway command error carrying the configured scheduler binary path in the command vector and an absolute workspace path in the stderr snippet
- **THEN** the upstream-error response body SHALL carry the rendered details, with the same field names as before
- **THEN** the response body SHALL contain neither the scheduler binary path nor the workspace root text
