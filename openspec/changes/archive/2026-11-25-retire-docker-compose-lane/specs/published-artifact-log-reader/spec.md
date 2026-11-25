## MODIFIED Requirements

### Requirement: Published artifact log reader

The system SHALL read job logs through a published artifact reader instead of directly resolving 22 private workspace paths on display nodes.

#### Scenario: Canonical published artifact configuration

- **WHEN** ArtifactReader loads configuration
- **THEN** it uses `NHMS_PUBLISHED_ARTIFACT_ROOT` and `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX` as canonical runtime env names

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
