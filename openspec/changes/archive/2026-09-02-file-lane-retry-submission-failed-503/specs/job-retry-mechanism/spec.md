## ADDED Requirements

### Requirement: File-journal manual retry submission failures surface through the route's structured 503

The pipeline retry route SHALL treat the file-journal retry lane and the database retry lane through one service seam: both lanes SHALL provide `attempt_manual_retry` and `submission_runtime_root_resolution`, and the route SHALL depend only on that seam. When a file-journal manual retry attempt ends in `submission_failed`, the route SHALL respond HTTP 503 with `error.code` equal to the recorded submission error code, a redacted `error_message`, and `details.status`, `details.run_id`, and `details.job_id` — never an unclassified HTTP 500. When the recorded submission event carries `runtime_root_resolution`, the response SHALL carry the persisted (already public-scrubbed) mapping unchanged under `details.runtime_root_resolution`; when the event carries none, the key SHALL be absent. A typed journal read fault while reading that evidence SHALL leave the key absent and the 503 intact. The response SHALL contain no traceback, journal path, or journal root text.

#### Scenario: File-lane submission failure with resolved runtime roots

- **WHEN** a manual retry on the file lane resolves runtime roots and the gateway submission raises
- **THEN** the route responds 503 with `error.code == "SBATCH_SUBMISSION_FAILED"` (or the gateway's own code), `details.status == "submission_failed"`, and `details.runtime_root_resolution` equal to the persisted submission event's `runtime_root_resolution`

#### Scenario: File-lane submission failure without runtime-root evidence

- **WHEN** a manual retry on the file lane resolves no runtime roots and the gateway submission raises
- **THEN** the route responds 503 with the same code and status fields and `details` has no `runtime_root_resolution` key

#### Scenario: Evidence read fault after the failure was recorded

- **WHEN** the failure has been recorded and the route's evidence read raises a typed journal error
- **THEN** the route still responds 503 with the failure's code and status, `details` has no `runtime_root_resolution` key, and the body carries no traceback

#### Scenario: Both lanes satisfy the seam

- **WHEN** the database retry service and the file-journal retry service are checked against the shared service protocol
- **THEN** both satisfy it without lane-specific adaptation
