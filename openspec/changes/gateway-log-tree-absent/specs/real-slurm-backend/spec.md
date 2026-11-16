## MODIFIED Requirements

### Requirement: fetch_logs reads log files from workspace

The `fetch_logs` method SHALL read job output and error logs from the configured workspace log directory.

#### Scenario: Fetch stdout log for completed job

- **WHEN** `fetch_logs` is called with a valid `job_id` and `run_id`
- **THEN** the system MUST read the file at `workspace/{run_id}/logs/{job_id}.out`
- **THEN** the returned content MUST be the raw text content of the log file
- **THEN** a field `complete` MUST be `true` if the job is in a terminal state

#### Scenario: Log file does not exist

- **WHEN** the expected log file path does not exist on the filesystem
- **THEN** the system MUST return an empty string with a warning message
- **THEN** the system MUST NOT raise an exception

#### Scenario: Fetch logs for running job returns partial content

- **WHEN** `fetch_logs` is called for a job in `RUNNING` status
- **THEN** the system MUST read the current content of the log file (partial output)
- **THEN** a field `complete` MUST be `false`

#### Scenario: A deleted job log tree is reported as absent, not as an empty log

- **WHEN** `fetch_logs` is called for a job whose submission metadata binds a `run_id`, no candidate log file exists, and every job-specific log directory is absent: the stored array log directory, `workspace/{run_id}/logs`, and the manifest `run_id` lane, but not the shared `workspace/logs` fallback
- **THEN** the response MUST carry `log_tree_present: false` with empty `logs`
- **AND** for a job without that binding (for example a record rebuilt from sacct after a gateway restart), `log_tree_present` MUST be `true` only when a log file or array task logs are actually found
- **AND** when a candidate log file exists, or the job-specific tree is intact (including an array master whose `<job_id>.out` is never written), the response MUST carry `log_tree_present: true`, so an empty log or an intact tree is never mistaken for a deleted one

---
