# python-environment-truth Specification

## Purpose

Align repository Python defaults, environment safety of the compute hosts, diagnostic checkout identity, validation routing, and historical topology authority without narrowing supported Python versions or forcing an active environment cutover.

## Requirements

### Requirement: Default Python matches the merge gate

The repository SHALL track Python 3.11 as the default uv interpreter while
retaining `requires-python >=3.11`, CI pip resolution, and explicit supported
version runs.

#### Scenario: Default and explicit selection

- **WHEN** a clean checkout runs `uv run python -V`
- **THEN** it reports Python 3.11.x
- **AND** `uv run --python 3.14 python -V` can select Python 3.14

#### Scenario: Newer standard-library API fails by default

- **WHEN** default Python invokes `Path.rglob(..., recurse_symlinks=True)`
- **THEN** Python 3.11 raises `TypeError`

### Requirement: Active entrypoints of the compute role preserve the deferred environment

Every tracked automatic or required operator operation SHALL preserve the
active environment of the compute role's checkout, which Slurm jobs and
scheduler timers on `w3` and `w2` reach over NFS. It uses a checked-in wrapper
or the exact active `.venv` interpreter and SHALL NOT create, update,
synchronize, or replace that environment. The single exception is a deploy
that changes `uv.lock`: the operator MAY synchronize the environment while the
scheduler timer is stopped, no scheduler pass is in flight, and no NWM job is
in the Slurm queue.

#### Scenario: Exact interpreter is required

- **GIVEN** the active checkout's environment is shared by running timers and
  Slurm jobs
- **WHEN** an automatic unit or required operator command runs
- **THEN** it invokes exact active Python or a checked-in wrapper
- **AND** a missing interpreter fails closed without invoking environment-updating uv

#### Scenario: Lockfile deploy is the only synchronization window

- **WHEN** agent instructions or a current runbook describe synchronizing the
  compute role's environment
- **THEN** they limit it to a deploy that changes `uv.lock`
- **AND** they require all three conditions: scheduler timer stopped, no pass
  in flight, no NWM job in the Slurm queue
- **AND** they forbid running any uv command on the retired host
  `210.77.77.22`

#### Scenario: Isolated rollback remains isolated

- **WHEN** a rollback checkout explicitly synchronizes its own environment
- **THEN** the command remains scoped to that rollback checkout
- **AND** it does not authorize synchronization of the active checkout

### Requirement: Diagnostic entrypoints cannot act on the active checkout

QHH diagnostic entrypoints SHALL reject the canonical active physical root
before any state or environment action. This applies to continuous, cycle shell,
sbatch, and backend-smoke entrypoints.

#### Scenario: Active-root alias fails closed

- **WHEN** a diagnostic entrypoint resolves directly or through a symlink to `/scratch/frd_muziyao/NWM`
- **THEN** it exits non-zero before lock, state, mkdir, direct Python, uv, or subprocess action

#### Scenario: Backend-smoke uses detached exact Python

- **WHEN** backend-smoke runs from an allowed detached checkout
- **THEN** direct Python commands use that checkout's executable `.venv/bin/python`
- **AND** a missing interpreter fails closed

### Requirement: Environment-coupled validation follows node-27

The e2e/grib runbook and pytest guidance SHALL route validation to node-27's
existing Python 3.11 environment under fail-fast `uv run --no-sync` control.

#### Scenario: Guard and pytest status remain visible

- **WHEN** an operator follows the e2e/grib lane
- **THEN** remote `set -euo pipefail` precedes checkout, a Python 3.11 assertion, and pytest
- **AND** a failed assertion stops before pytest
- **AND** failed pytest remains non-zero through the `tee` receipt
- **AND** neither node's project environment is synchronized

#### Scenario: Status-swallowing mutations turn red

- **WHEN** the checked-in lane gains unquoted `||` or any `set` segment containing `+e/+u/+eu/+euo` or `+o errexit/nounset/pipefail`
- **THEN** the static mutation seam fails
- **AND** pure enable, quoted/escaped literal, comment, and lone `set +o` controls remain green

### Requirement: Historical topology authority uses complete governed markers

The production-topology audit SHALL classify non-current whole documents through
the complete status marker and SHALL keep incomplete or current-authority text
visible.

#### Scenario: Historical baseline is non-current

- **WHEN** a document has a complete `historical baseline` marker without `superseded_by`
- **THEN** its preserved topology text is classified non-current

#### Scenario: Incomplete or current-authority marker cannot hide drift

- **WHEN** a marker omits a required field or appears on a canonical/dynamically declared current authority
- **THEN** topology text remains scanned and gate-eligible

### Requirement: pytest temporary directories MUST be bounded

The repository pytest configuration SHALL set `tmp_path_retention_policy = "failed"` so only failing tests retain their `tmp_path` directories, and the node-27 host discipline SHALL place pytest's temporary root on the `/home` volume via `TMPDIR`, never via a shared `--basetemp`.

#### Scenario: Passing tests leave no tmp_path residue

- **WHEN** a pytest session finishes with passing tests that used `tmp_path`
- **THEN** each of those directories is removed at that test's teardown

#### Scenario: node-27 temporary root is off the root volume

- **WHEN** pytest runs on node-27 under the documented profile
- **THEN** `pytest-of-nwm` is created under `/home/nwm/tmp`, and root-volume free space is unchanged by the run
