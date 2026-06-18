## Why

`flood.return_period_result` production storage is dominated by index bloat: recent observations showed roughly 138 GB of hypertable chunks with about 119 GB in indexes. After #490 makes historical no-curve empty rows removable, operators need a safe, evidence-driven way to audit, prune, rebuild, and document return-period indexes without breaking summary, ranking, timeline, valid-time discovery, or MVT hot paths.

## What Changes

- Add an operational audit capability for `flood.return_period_result` index inventory, usage statistics, relation size, and core query-plan capture.
- Add a generated maintenance plan/report that classifies each known return-period index as keep, drop, rebuild, replace, or investigate with evidence.
- Add guarded SQL/runbook material for manual maintenance-window execution, including lock-timeout settings, failure recovery, and pre/post evidence capture.
- Update production runbook guidance so DB space recovery is separated from #490 row deletion and does not imply automatic application DDL.
- Add tests for audit SQL generation, index classification, and targeted CI routing.

## Capabilities

### New Capabilities

- `flood-db-operations`: Operational tooling and runbook requirements for safe flood DB index audit and manual maintenance planning.

### Modified Capabilities

- None.

## Impact

- Affected code likely includes new or existing scripts under `scripts/`, targeted CI selection in `scripts/select_ci_tests.py`, and documentation under `docs/runbooks/current-production-ops.md` or adjacent runbook files.
- No public API or frontend contract changes.
- No production DDL is executed by application startup, tests, or migrations in this issue.
- Follow-up production execution remains a manual maintenance-window operation with explicit operator approval.
