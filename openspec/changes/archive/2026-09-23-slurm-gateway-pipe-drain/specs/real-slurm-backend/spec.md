## ADDED Requirements

### Requirement: Subprocess output capture is complete

The real backend SHALL read each Slurm command's stdout and stderr until both streams reach end-of-file before returning, so every byte the command wrote (up to the safe capture limit) is returned, independent of when the process exits relative to the reader. The existing timeout and capture-limit behavior SHALL be unchanged.

#### Scenario: Process exits with unread output still in the pipe

- **WHEN** a Slurm command writes more than one read chunk (for example 20000 bytes) and exits before the reader has consumed it
- **THEN** the returned stdout MUST contain all 20000 bytes
- **THEN** the system MUST NOT raise `SlurmParseError` because of a truncated read

#### Scenario: Process writes its only output line and exits during an idle wait

- **WHEN** `sbatch` prints `Submitted batch job <id>` and exits after the reader's wait returned with no events
- **THEN** the returned stdout MUST contain that line and the job id MUST be parsed

#### Scenario: Deadline still bounds the read

- **WHEN** a Slurm command neither exits nor closes its pipes before `subprocess_timeout_seconds`
- **THEN** the system MUST kill the process and raise the timeout error, as before
