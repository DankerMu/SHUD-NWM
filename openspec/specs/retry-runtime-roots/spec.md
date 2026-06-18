# retry-runtime-roots Specification

## Purpose
TBD - created by archiving change issue-384-retry-runtime-roots. Update Purpose after archive.
## Requirements
### Requirement: Shared source retry preserves runtime roots

Manual retry submissions for `download_source_cycle` SHALL preserve the durable
runtime-root contract used by the original production submission.

#### Scenario: Split workspace and object-store roots

- **WHEN** an operator retries a failed shared IFS source-cycle download whose
  production runtime uses distinct `WORKSPACE_ROOT` and `OBJECT_STORE_ROOT`
- **THEN** the retry submission manifest MUST include the configured
  `object_store_root` and `object_store_prefix`
- **AND** the rendered sbatch environment MUST export `OBJECT_STORE_ROOT` to the
  durable object-store root, not to `WORKSPACE_ROOT`.

#### Scenario: Published artifact roots are present

- **WHEN** the original runtime context includes published artifact root and URI
  prefix values
- **THEN** the manual retry manifest MUST preserve those values without changing
  the retry API route shape.

### Requirement: Shared source retry fails closed without required roots

Manual retry for a shared source-cycle download SHALL fail before Slurm
submission when required runtime roots cannot be reconstructed safely.

#### Scenario: Missing object-store root

- **WHEN** a `download_source_cycle` manual retry cannot resolve a required
  object-store root
- **THEN** no Slurm job MUST be submitted
- **AND** the retry job MUST transition to `submission_failed`
- **AND** evidence MUST include a stable error code and actionable message.

### Requirement: Runtime-root evidence is auditable and redacted

Retry submission success and failure evidence SHALL record runtime-root
resolution in a bounded, redacted form.

#### Scenario: Secret-bearing root evidence

- **WHEN** a runtime root, prefix, or submission error contains URI userinfo,
  tokens, signatures, passwords, or credential-like query parameters
- **THEN** persisted retry events and API-visible retry errors MUST redact those
  secrets
- **AND** the evidence MUST still identify which root contract was resolved or
  missing.

### Requirement: Existing retry compatibility is preserved

The new shared source-cycle root contract SHALL NOT regress existing retry
semantics for non-source jobs or duplicate manual retry guards.

#### Scenario: Non-source job retry

- **WHEN** an operator retries a failed non-`download_source_cycle` job
- **THEN** the retry submission MUST continue to use the existing retry manifest
  behavior without requiring source-cycle object-store root fields.

#### Scenario: Duplicate active retry

- **WHEN** an active manual retry already exists for a run
- **THEN** a second manual retry MUST still be rejected with the existing
  conflict behavior and no additional Slurm submission.

