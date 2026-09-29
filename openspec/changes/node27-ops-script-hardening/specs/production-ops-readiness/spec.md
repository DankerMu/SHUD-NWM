## ADDED Requirements

### Requirement: The display API restart wrapper resolves its repository from its own location

`scripts/ops/start-display-api.sh` SHALL derive the repository root it restarts, sources and sweeps from the script's own path, independent of the caller's working directory.

#### Scenario: Invoked from inside another git checkout

- **WHEN** the wrapper of checkout A is run with the working directory inside a different git repository B
- **THEN** the printed `repo_root`, the sourced `display.env`, the installed unit source and the uvicorn sweep pattern SHALL all refer to checkout A, and no process of B SHALL be signalled

### Requirement: The node-22 refresh timer health installer restores exactly once on an enable failure

The installer's ERR handlers SHALL run only in the main shell, and a probe timer that is not active after `enable --now` SHALL produce exactly one restore sequence.

#### Scenario: Probe timer inactive after enable

- **WHEN** `is-active` for the probe timer answers `inactive` with exit code 3 after `enable --now`
- **THEN** `--enable` SHALL exit non-zero without a status line, and the restore sequence and the protected-unit reads SHALL each appear exactly once
