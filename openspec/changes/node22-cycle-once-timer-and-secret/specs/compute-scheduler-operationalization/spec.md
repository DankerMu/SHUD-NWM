## ADDED Requirements

### Requirement: Node-22 exact-cycle wrapper serializes with the scheduler timer

`scripts/ops/node22-run-cycle-once.sh` SHALL run with the same environment the `nhms-compute-scheduler.service` unit loads: the scheduler env file, the Slurm gateway secret env file (default path identical to the tracked drop-in), and `NHMS_PYTHON_VENV_BIN`.

- In `--submit` mode it SHALL refuse to run (exit 2) when the secret file is missing or unreadable. In `--plan` mode it SHALL only warn.
- When `nhms-compute-scheduler.timer` is active, it SHALL stop the timer and wait, with a bounded timeout, until the service is no longer running before invoking the planner.
- It SHALL restart a timer it stopped on every exit path: success, planner failure, wait timeout, INT, TERM, or HUP; on a signal it SHALL forward the signal to the planner process and wait for it before exiting. It SHALL NOT start a timer that was inactive on entry.
- It SHALL fail closed (exit 2) when the timer or service state cannot be determined, or when stopping the timer fails.
- Secret values SHALL NOT appear in its stdout or stderr. Stdout SHALL carry only planner output.

#### Scenario: Active timer is paused around the run and restored

- **WHEN** the operator runs the wrapper with `--plan` while the timer is active and a scheduler pass is still running
- **THEN** the wrapper stops the timer, waits until the service is inactive, runs the planner, and starts the timer again before exiting with the planner's exit code

#### Scenario: Wait timeout restores the timer without running the planner

- **WHEN** the scheduler service is still running when the wait bound elapses
- **THEN** the wrapper exits non-zero with `timer_wait_timeout` on stderr, the planner is never invoked, and the timer is started again

#### Scenario: Submit without the gateway secret is refused

- **WHEN** the operator runs the wrapper with `--submit` and the gateway secret env file does not exist
- **THEN** the wrapper exits 2 without touching the timer and without invoking the planner
