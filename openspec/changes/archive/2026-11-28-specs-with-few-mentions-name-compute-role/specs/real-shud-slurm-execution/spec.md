## RENAMED Requirements

- FROM: `### Requirement: Node-22 Slurm submission path`
- TO: `### Requirement: Slurm submission path of the compute role`

## MODIFIED Requirements

### Requirement: Real SHUD executable preflight
The production runtime SHALL reject stub executables and validate the configured SHUD binary before submitting work.

#### Scenario: Stub executable rejected
- **WHEN** `SHUD_EXECUTABLE` is `/bin/true`, `/bin/false`, empty, missing, or otherwise configured as a non-SHUD stub
- **THEN** runtime preflight fails with a production-blocking error
- **AND** no Slurm job or successful hydro run is recorded.

#### Scenario: Scheduler preflight blocks stub before submit
- **WHEN** scheduler or orchestrator Slurm mode prepares to submit a SHUD job
- **THEN** it validates `SHUD_EXECUTABLE` and required runtime paths before calling the Slurm gateway or `submit_job_array`
- **AND** a stub or missing executable produces no Slurm submission, no active pipeline job, and no hydro success state.

#### Scenario: SHUD binary dependencies validated
- **WHEN** a SHUD executable is configured
- **THEN** preflight verifies that the executable exists, is executable, is visible from the execution context of the compute role, has required shared libraries available, and can report a bounded version/help or dry preflight signal
- **AND** missing libraries are reported as blockers without leaking environment secrets.

#### Scenario: Project inputs validated
- **WHEN** SHUD runtime starts for QHH
- **THEN** it verifies required project input files, generated forcing files, workspace paths, and manifest identities before execution
- **AND** failures are recorded as pre-submit blockers.

#### Scenario: A symlink to a multi-call binary is rejected regardless of host layout
- **WHEN** `SHUD_EXECUTABLE` names a file called `shud` whose realpath leaf is a known stub, a `gnu`-prefixed stub (as on Ubuntu 26.04, where `/bin/true` resolves to `gnutrue`), or a multi-call utility binary (`coreutils`, `busybox`, `toybox`)
- **THEN** preflight fails with `SHUD_EXECUTABLE_STUB_REJECTED` before any probe runs
- **AND** the verdict does not depend on how the host lays out `/bin/true`.

#### Scenario: argv[0] echo is not SHUD identity
- **WHEN** a probe's output mentions `shud` only because the binary echoes its own argv[0] (for example GNU `true --help` printing `Usage: <argv0> ...`)
- **THEN** the output does not count as SHUD identity, because identity requires the SHUD banner phrase (`Simulator for Hydrologic Unstructured Domains`)
- **AND** the real SHUD binary, whose no-argument banner contains that phrase, still reports the identity signal as present.

### Requirement: Slurm submission path of the compute role
SHUD production execution SHALL use a working Slurm gateway or host submission path of the compute role with health and accounting receipts.

#### Scenario: Slurm gateway healthy
- **WHEN** scheduler is configured for Slurm execution
- **THEN** preflight verifies the gateway or host service health, submit capability, allowed sbatch template, log root, and account/partition policy before candidate submission
- **AND** evidence records the gateway mode without exposing credentials.

#### Scenario: Slurm unavailable blocks execution
- **WHEN** Slurm CLI/gateway/accounting is unavailable or points to an invalid self-reference
- **THEN** scheduler records a Slurm blocker
- **AND** it does not mark the SHUD stage submitted or succeeded.

#### Scenario: Slurm receipt persisted
- **WHEN** SHUD is submitted to Slurm
- **THEN** `ops.pipeline_job` and/or `ops.pipeline_event` records Slurm job id, array task id when applicable, submit time, status, log URI, and resource/accounting metadata when available
- **AND** repeated scheduler scans do not submit duplicate active jobs.
