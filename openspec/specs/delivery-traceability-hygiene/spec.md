# delivery-traceability-hygiene Specification

## Purpose
TBD - created by archiving change m6-system-hardening-alignment. Update Purpose after archive.
## Requirements
### Requirement: Delivery docs name canonical paths
Implementation planning and README documentation SHALL name the canonical source paths used by package entry points and active tests.

#### Scenario: Frontend path is canonical
- **WHEN** a developer follows project docs for frontend work
- **THEN** the docs MUST point to `apps/frontend` and not to retired frontend placeholder paths

#### Scenario: Worker paths are canonical
- **WHEN** a developer follows project docs for worker modules
- **THEN** the docs MUST point to underscore Python packages such as `workers/forcing_producer` unless a hyphen directory is explicitly labeled legacy or placeholder

#### Scenario: Storage root semantics are documented
- **WHEN** a developer follows storage or deployment docs
- **THEN** the docs MUST describe `WORKSPACE_ROOT` as temporary/HPC workspace and `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX` as durable artifact storage

#### Scenario: Slurm template ownership is documented
- **WHEN** a developer follows orchestration docs
- **THEN** the docs MUST identify `infra/sbatch` as the canonical real Slurm template path or explicitly document any supported legacy template path

### Requirement: OpenSpec task states are evidence-backed
OpenSpec task checkboxes SHALL distinguish implemented, tested, accepted, and deferred work, and completed claims SHALL link to source or test evidence.

#### Scenario: Implemented M4 work has test evidence
- **WHEN** an OpenSpec task is marked complete for IFS or multi-source behavior
- **THEN** the task entry MUST reference the implementation file or test that proves completion

#### Scenario: Incomplete delivery remains unchecked
- **WHEN** a capability has implementation code but lacks contract tests or accepted behavior
- **THEN** the task MUST remain unchecked or be marked as implemented-but-not-accepted

### Requirement: JSON schemas mirror runtime enums and fields
Standalone JSON schemas SHALL include statuses and fields used by runtime persistence and API payloads.

#### Scenario: Pipeline job schema includes M3 statuses
- **WHEN** `ops.pipeline_job.status` or API payloads use `queued`, `submission_failed`, `partially_failed`, or `permanently_failed`
- **THEN** `schemas/pipeline_job.schema.json` MUST include those statuses

#### Scenario: Pipeline job schema includes array metadata
- **WHEN** pipeline jobs include `model_id` or `array_task_id`
- **THEN** the JSON schema MUST include those fields with appropriate types

### Requirement: Verification evidence is recorded for release decisions
The hardening stage SHALL record the exact verification commands and outcomes required for release acceptance.

#### Scenario: Release acceptance cites commands
- **WHEN** the hardening change is considered complete
- **THEN** documentation or issue checklists MUST include Python tests, ruff, frontend tests, frontend build, bundle check, and relevant E2E/contract tests with pass/fail outcomes

### Requirement: The cross-PR review-gate memory SHALL hold one authority per issue with in-vocabulary outcomes

The tracked review-gate memory file SHALL carry exactly one top-level mapping, keyed by issue, and SHALL NOT
carry per-issue entries beside it at the top level. The escalation path reads only that mapping, so a second copy
of an issue's record placed outside it is silently ignored: a ceiling that was recorded there would not raise the
human decision it exists to force, and two copies that disagree leave no way to tell which one the next run trusted.
Both observed top-level defects arrived through hand-resolved merge conflicts, not through the tool, which cannot
write a top-level key at all — so the invariant SHALL be enforced by a check the repository itself runs, on the
commits that carry the file. Where the invariant is additionally enforced closer to the writer is not constrained
here.

Every recorded closure outcome SHALL be one of the four terminal outcomes the workflow defines — merged,
superseded-by-split, abandoned, or descoped. A value outside that set is one the vocabulary does not define and
the loop-log validator rejects, so it makes the record unreadable by the same pipeline that wrote it. Because such
a value can be produced by a closure recorded without an explicit outcome, the check's failure SHALL name that
cause and the remedy, so that whoever meets it is not left to re-derive why an unremarkable-looking word is refused.

Each per-issue entry SHALL carry its ceiling list, its gate-entry count, and its closure list, each of the declared
type, so that a truncated or partially merged entry is a failure rather than a silently permissive record.

#### Scenario: A per-issue record beside the mapping is rejected

- **GIVEN** the memory file carrying an issue-keyed entry at the top level, next to the issue mapping
- **WHEN** the structural check runs
- **THEN** it fails and names the offending key, because the escalation path would not have read that entry

#### Scenario: An out-of-vocabulary closure outcome is rejected

- **GIVEN** a closure record whose outcome is not one of the four terminal outcomes
- **WHEN** the structural check runs
- **THEN** it fails and names the issue and the value

#### Scenario: An entry missing a declared field is rejected

- **GIVEN** a per-issue entry lacking its ceiling list, its gate-entry count, or its closure list
- **WHEN** the structural check runs
- **THEN** it fails rather than treating the absent field as an empty or default value

