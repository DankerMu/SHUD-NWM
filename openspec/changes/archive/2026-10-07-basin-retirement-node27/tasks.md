# Tasks

## 1. Implementation

- [x] 1.1 `scripts/node27_retire_basin.py` and `scripts/basin_retirement/`: arguments, environment refusals,
      preconditions (node-22 finish receipt, manifest, basin version, model rows), receipts, dry-run report.
- [x] 1.2 `exclude`: env file checks, backup, atomic one-line edit, the autopipe wait.
- [x] 1.3 `supersede`: backup CSV and update in one transaction; dry-run by rollback.
- [x] 1.4 `deactivate`: preflight of all active rows, then the lifecycle operation row by row.
- [x] 1.5 `verify`: second autopipe wait and the read-only checks.
- [x] 1.6 Suites `tests/test_node27_retire_basin_*.py` (every Evidence bullet).
- [x] 1.7 CI selector rules for the new files (extend an existing rule where a path already has one).
- [x] 1.8 Runbooks: `operating-scope.md` 7; pointer in `recalibration-and-archive.md` 5.7.4.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; the new suites; the suites of every existing module the tool imports
      from; `tests/test_entropy_audit_line_references.py`; `tests/test_select_ci_tests.py` (after staging,
      last); `openspec validate basin-retirement-node27 --strict --no-interactive`.
- [x] 2.2 CI green on the PR.
- [ ] 2.3 node-27 after merge and pull: the tool's dry-run for one live basin version with a throwaway
      `--receipt-root`. Expected: `would_be_refused` for the missing node-22 finish receipt; the env-file
      checks and the autopipe unit query run against the real file and unit; no file changes. This run opens
      the production database read-only for the precondition queries only if the owner agrees; the supersede
      dry-run (a transaction that is rolled back) is run only with the owner's go-ahead.

Deviation: node-27 真实 DB receipt 待链路恢复后补。Pending once the RAID link is repaired:
`cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH TMPDIR=/home/nwm/tmp && uv run --no-sync pytest -q
tests/test_node27_retire_basin_*.py`, plus one apply of the tool against a scratch database seeded with one
basin version, with a temporary `--env-file` holding the scratch `DATABASE_URL` and a stand-in `systemctl`. Local results are not a node-27 PASS. No production `--apply` is part of this change.
