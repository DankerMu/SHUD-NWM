## ADDED Requirements

### Requirement: The display deployment receipt observes the running unit

The node-27 display deployment receipt (C1 of the bring-up checklist) SHALL be
the output of `scripts/ops/display-c1-receipt.sh`. The script SHALL only read:
it SHALL NOT start, stop or restart a unit or a process, SHALL NOT source
`infra/env/display.env`, and SHALL NOT print any value from that file other
than the two storage roots named below. It SHALL NOT call docker.

The repository root SHALL be the checkout that contains the script, unless a
root is given as the first argument. The receipt SHALL print the root and the
checkout's HEAD commit.

The script SHALL evaluate every claim below, print one line per claim with
`ok` or `FAIL`, and then print `C1=PASS` and exit zero only if every claim is
`ok`; otherwise it SHALL print `C1=FAIL` and exit non-zero.

| Claim | Holds when |
|---|---|
| `unit_active` | the user unit `nhms-display-api.service` is active and has a main process |
| `process_of_checkout` | the main process's command line is `<root>/.venv/bin/python -m uvicorn apps.api.main:app` with a `--port` |
| `checkout_clean` | no tracked file of the checkout is modified, staged or deleted |
| `process_loaded_head` | the main process started strictly later than the checkout's HEAD last moved and strictly later than `infra/env/display.env` was last modified |
| `listener_of_unit` | at least one socket listens on that port, every listening line names its processes, and each of them is the main process or a child of it |
| `health` | `GET /health` on the loopback address and that port answers 200 |
| `runtime_role` | `GET /api/v1/runtime/config` reports `service_role` `display_readonly`, `display_readonly` true, `slurm_routes_enabled` false and `control_mutations_enabled` false |
| `slurm_routes_absent` | `GET /api/v1/slurm/health` answers 404 |
| `no_slurm_client` | none of `sbatch`, `scancel`, `squeue`, `scontrol`, `sacct`, `srun`, `sinfo` is on `PATH`, and none of `/etc/slurm`, `/etc/slurm-llnl`, `/run/munge/munge.socket.2` exists |
| `storage_not_writable` | `OBJECT_STORE_ROOT` and `NHMS_PUBLISHED_ARTIFACT_ROOT` of `infra/env/display.env` each name an existing directory that the invoking user cannot write |

`storage_not_writable` is a statement about the two root directories
themselves, not about every file beneath them.

A claim whose input cannot be read SHALL be `FAIL`, never skipped: no main
process, no port, an empty reflog, an unreadable env file, a storage key that
is missing or empty, a listener query that fails or names nobody, a request
that gets no answer.

#### Scenario: A healthy display unit passes

- **WHEN** the unit is active and its main process is the clean checkout's uvicorn, started after the checkout's HEAD last moved and after the env file was last modified
- **AND** the port is held by that process and its children only, the three requests answer as the table says, the host has no Slurm client, and neither storage root is writable
- **THEN** every claim line says `ok`, the last line is `C1=PASS`, and the exit status is zero

#### Scenario: A process older than the checkout does not pass

- **WHEN** the checkout's HEAD moved after the main process started, or in the same second
- **THEN** `process_loaded_head` is `FAIL`, the last line is `C1=FAIL`, and the exit status is non-zero
- **AND** the other claims are still evaluated and printed

#### Scenario: A process older than its env file does not pass

- **WHEN** `infra/env/display.env` was modified after the main process started
- **THEN** `process_loaded_head` is `FAIL` and the receipt does not pass

#### Scenario: A modified checkout does not pass

- **WHEN** a tracked file of the checkout differs from HEAD
- **THEN** `checkout_clean` is `FAIL` and the receipt does not pass
- **AND** an untracked file alone does not fail the claim

#### Scenario: A foreign listener does not pass

- **WHEN** a process that is neither the main process nor one of its children listens on the port
- **THEN** `listener_of_unit` is `FAIL` and the receipt does not pass, whatever the three requests answer

#### Scenario: A reachable Slurm route does not pass

- **WHEN** `GET /api/v1/slurm/health` answers anything other than 404
- **THEN** `slurm_routes_absent` is `FAIL` and the receipt does not pass

#### Scenario: A role other than display_readonly does not pass

- **WHEN** `GET /api/v1/runtime/config` reports another `service_role`, or `display_readonly` false, or `slurm_routes_enabled` true, or `control_mutations_enabled` true
- **THEN** `runtime_role` is `FAIL` and the receipt does not pass

#### Scenario: A Slurm client on the host does not pass

- **WHEN** one of the listed commands is on `PATH`
- **THEN** `no_slurm_client` is `FAIL` and the receipt does not pass

#### Scenario: A writable storage root does not pass

- **WHEN** the invoking user can write the object-store root or the published-artifact root, or one of the two keys is missing
- **THEN** `storage_not_writable` is `FAIL` and the receipt does not pass

#### Scenario: An inactive unit fails every claim that needs the process

- **WHEN** the unit is not active or has no main process
- **THEN** `unit_active`, `process_of_checkout`, `process_loaded_head`, `listener_of_unit`, `health`, `runtime_role` and `slurm_routes_absent` are `FAIL`
- **AND** no request is sent to a guessed port

#### Scenario: The receipt changes nothing

- **WHEN** the script runs, passing or failing
- **THEN** it has called no `systemctl` verb other than `show` and `is-active`, has sent only `GET` requests, and has written no file, the checkout's git index included
