## ADDED Requirements

### Requirement: Published artifact log reader

The system SHALL read job logs through a published artifact reader instead of directly resolving 22 private workspace paths on display nodes.

#### Scenario: Canonical published artifact configuration
- **WHEN** ArtifactReader loads configuration
- **THEN** it uses `NHMS_PUBLISHED_ARTIFACT_ROOT` and `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX` as canonical runtime env names
- **AND** compose-only host path configuration uses `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` when the host mount source differs from the in-container root.

#### Scenario: Published URI log
- **WHEN** a job has `log_uri=published://logs/<source>/<cycle>/<run_id>/<job_id>.out`
- **THEN** `/api/v1/jobs/{job_id}/logs` reads the log from the configured published artifact root
- **AND** the response returns bounded tail content and a safe public `log_uri`.

#### Scenario: Allowed file URI log
- **WHEN** a job has a `file://` log URI under the configured published artifact root
- **THEN** the log reader can return bounded tail content
- **AND** symlinks and resolved paths outside the publish root are rejected.

#### Scenario: S3 URI log
- **WHEN** a job has a supported `s3://<bucket>/<prefix>/logs/...` log URI and readonly object-store credentials are configured
- **THEN** the log reader can return bounded tail content
- **AND** `<bucket>` and `<prefix>` match the configured published artifact S3 allowlist
- **AND** missing object or access-denied errors map to stable typed API errors.

#### Scenario: S3 URI outside allowlist
- **WHEN** a job has an `s3://` log URI whose bucket or prefix does not match `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET` and `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`
- **THEN** the log reader rejects the URI with `JOB_LOG_ACCESS_DENIED` or `JOB_LOG_URI_UNSUPPORTED`
- **AND** it does not attempt to read the object.

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

### Requirement: Private and unsafe log URIs rejected

The log reader SHALL reject private compute paths and unsafe URI forms.

#### Scenario: Private workspace path rejected
- **WHEN** a job log URI points to `WORKSPACE_ROOT`, `.nhms-runs`, 22 private `/scratch`, `/tmp`, or a relative local path outside the published artifact root
- **THEN** `/api/v1/jobs/{job_id}/logs` returns a stable forbidden or unsupported log error
- **AND** the response does not leak the private absolute path.

#### Scenario: Path traversal rejected
- **WHEN** a log URI contains `..`, backslashes, encoded path separators, encoded traversal, or a symlink escape
- **THEN** the log reader rejects it before opening the file
- **AND** the response contains a stable error code such as `JOB_LOG_ACCESS_DENIED` or `JOB_LOG_URI_UNSUPPORTED`.

#### Scenario: Credential-bearing URI rejected
- **WHEN** a log URI contains userinfo, query parameters, fragments, tokens, signatures, or apparent credential path components
- **THEN** the log reader rejects or redacts the URI
- **AND** no secret value appears in API responses or logs.

### Requirement: Stable log errors

Job log failures SHALL map to stable API error codes that do not depend on local filesystem exceptions.

#### Scenario: Log not published
- **WHEN** a job has no `log_uri`
- **THEN** the API returns `JOB_LOG_NOT_PUBLISHED`
- **AND** the response identifies the job without exposing server paths.

#### Scenario: Unsupported log URI
- **WHEN** a job has an unsupported scheme or location
- **THEN** the API returns `JOB_LOG_URI_UNSUPPORTED`
- **AND** the response includes a redacted safe URI summary.

#### Scenario: Log not found
- **WHEN** a supported published log URI does not exist
- **THEN** the API returns `JOB_LOG_NOT_FOUND`
- **AND** the response distinguishes missing log from unsafe log access.

#### Scenario: Tail limit enforced
- **WHEN** a published log is larger than `NHMS_LOG_TAIL_MAX_BYTES`
- **THEN** the API returns only the bounded tail content
- **AND** the response indicates truncation or bounded read behavior where the existing log schema allows it.
