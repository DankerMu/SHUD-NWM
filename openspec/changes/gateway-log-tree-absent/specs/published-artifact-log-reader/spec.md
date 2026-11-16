## MODIFIED Requirements

### Requirement: Compute side publishes display-readable log URIs

The compute-control production path SHALL write job log URIs that display readonly services can read.

#### Scenario: New production job log URI
- **WHEN** 22 submits or records a production pipeline job
- **THEN** `ops.pipeline_job.log_uri` is written as a supported published artifact URI
- **AND** the canonical MVP form is `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out` or `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.err`.

#### Scenario: Existing object-store log URI compatibility
- **WHEN** existing code emits an object-store log URI such as `s3://.../runs/<run_id>/logs/...`
- **THEN** it is treated as display-readable only if the bucket and prefix are explicitly allowlisted as a published log namespace
- **AND** private workspace or unallowlisted object-store URIs remain rejected.

#### Scenario: An unavailable log is not published as an empty log
- **WHEN** the compute side persists or advertises a job log and the gateway reports `log_tree_present: false`
- **THEN** it MUST NOT write a log object or record `ops.pipeline_job.log_uri`; the URI stays unset and a later pass may fetch it again
- **AND** a response without `log_tree_present`, from an older gateway, MUST be treated as present, and a genuinely empty existing log file is still written and recorded as before
