# Ops monitoring API — gateway error evidence rendering

## ADDED Requirements

### Requirement: Slurm gateway error evidence SHALL render host paths on ops responses while persisted events keep the raw text

When a Slurm gateway error reaches an ops API response body, its `message` and its `details` SHALL be rendered through the shared public evidence renderer, so that absolute host paths become `[local-path]` and URIs become `[uri]`/`[object-uri]`, including inside nested values and list elements such as the executed command's argument vector and the captured stderr snippet. Secret redaction SHALL keep its existing precedence.

This SHALL apply to the cancel endpoint's 200 body — every gateway error carried under its failed and blocked job entries — and to the queue-depth endpoint's upstream-error body. The response SHALL contain no configured scheduler binary path and no workspace root text.

The pipeline events persisted beside those responses SHALL keep the gateway's raw, secrets-redacted text, because operators diagnose from them and because the durable event is the system's record of what the scheduler actually reported. It is NOT an anti-laundering rule — `[local-path]` is deliberately persisted evidence elsewhere. The same raw-persistence rule protects the runtime-root recovery that reads a persisted gateway response's manifest subtree, on the `submission` events that recovery scans. A single rendered payload SHALL NOT be shared between a response and a persisted event: the persisted copy SHALL remain in the shape those readers can consume.

The upstream-error response schema's field names SHALL NOT change; only the values are rendered. Errors that carry no gateway text, such as the retry error family, SHALL keep their current rendering.

#### Scenario: Cancel response renders a failing scancel's command and stderr

- **WHEN** `scancel` fails for an active job and its error carries the configured scheduler binary path in the command vector and an absolute workspace path in the stderr snippet
- **THEN** the cancel response's failed-job entry SHALL carry the rendered command vector and a stderr snippet in which the workspace path is `[local-path]`
- **THEN** the response body SHALL contain neither the scheduler binary path nor the workspace root text

#### Scenario: The unproven-cancellation branch renders the same way

- **WHEN** the gateway's error is one the route classifies as an unproven cancellation, so the entry lands under blocked jobs
- **THEN** the same rendering SHALL apply to that entry's error details and message

#### Scenario: Queue depth upstream error renders the same way

- **WHEN** the queue-depth query fails with a gateway command error carrying the same two kinds of path
- **THEN** the upstream-error response body SHALL carry the rendered details, with the same field names as before

#### Scenario: An unproven cancellation's gateway response is rendered on the wire and raw in the event

- **WHEN** a cancellation returns a record whose manifest carries a workspace directory, a manifest index path or an array log directory, and the route reports it as an unproven cancellation
- **THEN** the response's gateway-response payload SHALL be rendered like any other gateway evidence
- **THEN** the persisted event's copy SHALL keep the raw values, in the shape the runtime-root readers consume — a real root, not a placeholder

#### Scenario: Persisted cancellation events keep the raw evidence

- **WHEN** the same failure is recorded as a cancellation-gap or cancel-failed pipeline event
- **THEN** that event's persisted error details SHALL still carry the raw command vector and stderr text, with secrets redacted as before, and SHALL NOT carry any public placeholder introduced by the response rendering
