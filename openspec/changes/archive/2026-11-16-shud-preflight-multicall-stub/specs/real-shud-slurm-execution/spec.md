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
- **THEN** preflight verifies that the executable exists, is executable, is visible from the node-22 execution context, has required shared libraries available, and can report a bounded version/help or dry preflight signal
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
