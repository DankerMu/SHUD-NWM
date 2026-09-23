# Real Slurm gateway contract — single-submit refuses production array job types

## MODIFIED Requirements

### Requirement: Production jobs use fixed templates or constrained script mode

Real Slurm execution SHALL use fixed configured templates unless a constrained script mode is explicitly implemented and tested.

The single-job submit entrypoint SHALL refuse **every** job type whose configured template is a production array template, before any scheduler command runs, and SHALL name the array endpoint in the refusal's details. The refused set SHALL be derived from the production array template names rather than maintained as a second literal list, so that adding a production array template cannot leave the single-submit entrypoint accepting it. A deployment that overrides the job-type to template mapping SHALL only be able to widen that refused set, never to narrow it. Refusal SHALL NOT depend on an incomplete request failing some later validation: a fully authorized request carrying every field the array template needs SHALL still be refused.

#### Scenario: Legacy single-job path submits to real Slurm
- **WHEN** a legacy or analysis orchestration path submits a single job to RealSlurmGateway
- **THEN** the job MUST resolve to an available configured template or a validated constrained script mode
- **AND** unsupported legacy `job_type` values MUST fail before submission with a clear validation error

#### Scenario: Template ownership is documented
- **WHEN** developers inspect Slurm template documentation
- **THEN** the docs MUST state which paths are canonical for real Slurm, which are legacy, and which orchestrator paths still use them

#### Scenario: Every production array job type is refused by the single-job endpoint

- **WHEN** a fully authorized single-job submit names any production array job type, including one whose request carries a valid cycle identity and manifest index path
- **THEN** the submit SHALL raise a validation error before invoking the scheduler, with details naming the array endpoint
- **THEN** no scheduler submission command SHALL have been executed

#### Scenario: The refused set cannot drift from the production array templates

- **WHEN** the set of production array templates changes
- **THEN** the single-submit refusal set SHALL follow it without a separate edit, a deployment override of the job-type mapping SHALL only widen it, and a test SHALL fail if the derived set and the enumerated production array job types ever disagree

#### Scenario: Array submission of the same job type still works

- **WHEN** the same production array job type is submitted through the array entrypoint with a valid cohort
- **THEN** it SHALL be submitted with an array specification, exactly as before
