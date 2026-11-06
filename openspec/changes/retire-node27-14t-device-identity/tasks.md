# Tasks: retire-node27-14t-device-identity

## 1. Governance sample point (#3059)

- [x] 1.1 Remove the `"cold": disk_usage(Path("/data/GHDC"))` sample from
  `collect_filesystem` in `packages/common/node27_resource_governance_collection.py`.
  Evidence floor: `tests/test_node27_working_set.py::test_cli_ordinary_receipt_ignores_retired_cold_environment`
  asserts `cold` is absent and `root`/`home` stay present (red before, green after).
- [x] 1.2 Sync working-set fixtures (`/data/GHDC` stat alias dropped; the
  parametrized PGDATA target uses the current `/home/nwm/nhms-primary/pgdata`).

## 2. Retire the #2240 production relocation identity (#3059)

- [x] 2.1 `plan` without `--disposable-root` reports the retirement as its only
  blocker with no container/unit/database command; mutating actions refuse non-zero before the
  lifecycle lock; the `Host.health` production branch refuses; the `/dev/md0`
  and `/data/GHDC` identity checks are removed from `node27_pgdata_host.py`
  and `_validate_paths`.
  Evidence floor: `uv run pytest -q tests/test_node27_pgdata_relocation_retired.py
  tests/test_node27_pgdata_migrate.py` green (new file red before the change).
- [x] 2.2 Register `tests/test_node27_pgdata_relocation_retired.py` in
  `scripts/select_ci_tests_rules.py` and `tests/test_select_ci_tests.py`.

## 3. Docs (#3059, #3060)

- [x] 3.1 ADR 0002 Revision 2026-11-05 records the #3059 follow-up; the tier
  runbook #2240 section states the CLI refusal.
- [x] 3.2 `docs/runbooks/tier-node27-timeseries-storage.md` §4.3.3 `docker run`
  matches `docs/runbooks/node-27-database-container-operations.md` §4 flag for
  flag (`-p 127.0.0.1:55432:5432`, `--shm-size=1g`, `--stop-signal=SIGINT`,
  `--stop-timeout=300`), and §4 is declared the single authority.
  Evidence floor: `npx -y markdownlint-cli2@0.17.2` on both runbooks and the ADR passes.

## 4. Change-level verification floor

- [x] 4.1 `openspec validate retire-node27-14t-device-identity --strict --no-interactive` PASS.
- [x] 4.2 `uv run ruff check .` PASS.
- [x] 4.3 `uv run pytest -q tests/test_node27_resource_governance.py tests/test_node27_working_set.py
  tests/test_node27_pgdata_migrate.py` PASS, plus the selector-chosen tests for the diff.
- [x] 4.4 `git grep -n -e "/data/GHDC" -e "/dev/md0" -- packages scripts` leaves only
  the historical comments in `node27_container_contract.py` and
  `node27_timeseries_retention.py` and the inert frozen `DEFAULTS` identity.
- [ ] 4.5 node-27 manual governance run
  (`systemctl --user start nhms-node27-resource-governance.service`) shows no
  `cold` entry in the report. Post-merge, owned by the orchestrator; not run from this PR.
