# node-22 Python pin receipt

Date: 2026-08-24
Commit: `1b6f3b14d0b4d679b28154dde3fec1bc146238e7`
Node: `frd_muziyao@210.77.77.22:/scratch/frd_muziyao/NWM`

## Decision

The active node-22 checkout remains on Python 3.12.7 until the next operator-approved service maintenance window. Three live processes were using the shared `.venv` when the controlled sync was attempted:

- `services.slurm_gateway`
- `uvicorn apps.api.main:app`
- one activating `services.orchestrator.cli plan-production --submit --continuous --max-passes 1`

`lsof` showed loaded Python 3.12 extension objects and two NFS `.nfs*` residues under `.venv`. `uv sync --all-extras --dev` downloaded CPython 3.11.15, then correctly failed to replace the in-use `.venv/lib` with `Directory not empty (os error 39)`. Stopping live services was not authorized and would exceed this tooling-only issue, so the active environment was not forcibly replaced.

The next operator-approved maintenance window SHALL stop the processes that use `/scratch/frd_muziyao/NWM/.venv`, run `uv sync --all-extras --dev`, assert `uv run python -V` is Python 3.11.x, then restart and verify the services under their owning runbook. This is an operational migration of the committed pin, not a Slurm scheduling validation.

## Rollback/recovery evidence

The failed replacement removed three packages before encountering the open NFS files. The active environment was restored explicitly:

```text
uv sync --python 3.12 --all-extras --dev
Installed 3 packages:
  psycopg2-binary==2.9.12
  referencing==0.37.0
  six==1.17.0
RESTORED_PYTHON=Python 3.12.7
restored imports: psycopg2, referencing, six
uv sync --python 3.12 --all-extras --dev --dry-run
Would make no changes
```

The tracked worktree was clean, `.nhms-work/` was preserved, and the checkout was returned to its original `master@22db3a93339009dd539ce3828bb3093ff75f4fa7`. No live process was stopped.

## Isolated exact-commit acceptance

A disposable detached Git worktree at the exact implementation commit proved the tracked pin and dependency graph on node-22 without touching the active environment:

```text
RECEIPT_HEAD=1b6f3b14d0b4d679b28154dde3fec1bc146238e7
PIN=3.11
Using CPython 3.11.15
UV_PYTHON=Python 3.11.15
VERSION_ASSERT=3.11.15
TRACKED_STATUS_BEGIN
TRACKED_STATUS_END
SLURM_TRIGGERED=no (no Slurm command invoked)
TEMP_WORKTREE_CLEANED=yes
```

No `sbatch`, `srun`, scheduler submission, or SHUD command was invoked. The disposable worktree and its `.venv` were removed with `git worktree remove --force`; `git worktree list` showed no receipt worktree afterward.