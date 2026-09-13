## ADDED Requirements

### Requirement: Public job error messages SHALL render local paths and URIs

The system SHALL render public job error messages through the shared renderer. The manual-retry `submission_failed` 503 body (`error.message`, `details.error_message`) and job/basin
result payloads returned by pipeline read routes SHALL render `error_message` through the shared public
evidence renderer: secrets become `[redacted]`, absolute local paths `[local-path]`, URIs
`[uri]`/`[object-uri]`, while surrounding words are kept. Persisted job rows and pipeline events keep raw text.
The database and file-journal lanes SHALL produce the same public rendering for the same text.

#### Scenario: Gateway error with absolute path in retry 503

- **WHEN** retry submission fails with `sbatch: error: cannot open /srv/nhms/workspace/run-42/job.sbatch for writing`
- **THEN** `error.message` and `details.error_message` equal `sbatch: error: cannot open [local-path] for writing` and the body contains no workspace or object-store root

#### Scenario: Persisted event keeps raw text

- **WHEN** the same failure is recorded
- **THEN** the persisted pipeline event `message` and `details.error_message` still contain the raw path

### Requirement: Scheme-anchored URIs containing whitespace SHALL classify whole

The public evidence scalar sanitizer SHALL classify a value that begins with a URI scheme
(`^[A-Za-z][A-Za-z0-9+.-]*://`) as a whole URI placeholder even when it contains whitespace. Values that
merely mention a scheme mid-text SHALL keep token-wise rendering.

#### Scenario: Spaced object-store URI

- **WHEN** a runtime root value is `s3://nhms prod/objects`
- **THEN** it renders `[object-uri]` with no `prod/objects` tail

#### Scenario: Prose mentioning a URI stays tokenised

- **WHEN** a value is `see s3://x for details`
- **THEN** it renders `see [object-uri] for details`
