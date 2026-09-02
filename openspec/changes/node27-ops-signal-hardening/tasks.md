## Risk Triage

```text
Issue type: ops hardening (4 bugfixes)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium (two production units + wrappers, the autopipe connection helper, repo pytest config)
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issues; expanded forced by config/CLI/entrypoint triggers)
Why:
- production unit files + wrappers (config trigger); shared _connect helper (shared-helper trigger)
- pyproject pytest config touches every test run (CI/local/node-27)
- #1765 is priority:high with a live root-disk incident behind it
OpenSpec change: node27-ops-signal-hardening
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_node27_timeseries_retention.py tests/test_node27_resource_governance.py tests/test_node27_timeseries_compression.py tests/test_node27_autopipeline_connection_bounds.py (plus the autopipeline handoff suite)
- openspec validate node27-ops-signal-hardening --strict --no-interactive
- node-27 receipts (see T9)
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | wrappers, units, governance CLI exit code, stats-guard flag |
| Config / project setup | selected | `pyproject.toml` pytest policy, unit files, `.gitignore` |
| File IO / path safety / overwrite | selected | lock path move, `tee -a` to the log, `TMPDIR` discipline |
| Schema / columns / units / field names | not selected | receipt schemas untouched (governance `status` stays `completed`) |
| Auth / permissions / secrets | selected | refusal_reason now travels to journal + mail: redaction must hold |
| Concurrency / shared state / ordering | selected | flock under a piped runner; PIPESTATUS semantics; statement timeout vs. long ingest queries |
| Resource limits / large input / discovery | selected | root-disk growth bound; connect/statement timeouts |
| Legacy compatibility / examples | selected | `systemd.err` retirement scoped to the retention unit, sibling units byte-identical; `off` keeps working; DSN `connect_timeout` precedence preserved |
| Error handling / rollback / partial outputs | selected | governance receipt written before non-zero exit; tee failure must not change RC |
| Release / packaging / dependency compatibility | not selected | no dependency changes (pytest already ≥7.3) |
| Documentation / migration notes | selected | §8.6 items 6/7/8, capacity-check discipline in three docs + agents source |
| PostGIS / TimescaleDB 域行为 | selected | chunk identifier fail-closed helper (no interpolating consumer today); statement_timeout now spans ingest/backfill write statements (600 s) as well as catalog reads |
| 其余 domain packs | not selected | no geometry/forcing/SHUD/Slurm/provider/manifest/display-identity surface |

## Tasks

- [x] T1 (#1766) Runbook §8.6 item 7 criterion counts `RETENTION_DROP_FAILED:` diagnostic lines in the same tick bracket as item 5; item 6 wording reconciled; anchor test drives 57014/55P03/40P01 refusals and a clean tick, asserting one counted line per refused tick and zero otherwise, and pins the runbook command pattern byte-for-byte.
- [x] T2 (#1712) Wrapper: `2>&1 | tee -a "$LOG_FILE" >&2`, `RC=${PIPESTATUS[0]}`; shell test with a fake runner (exit 3 + stderr text) asserts wrapper exit 3, `retention.log` contains bracket lines + text, wrapper stderr contains the text. Unit: `StandardError=journal`, `StandardOutput` unchanged, `OnFailure` unchanged; unit-file test pins the three lines.
- [x] T3 (#1712) Retire the retention unit's `systemd.err` lane only (`infra/systemd/nhms-node27-timeseries-retention.service`); sibling units and the historical product-archive receipt README untouched; add the `journalctl --user -u nhms-node27-timeseries-retention.service -n 30 --no-pager` command to §8.6 and rewrite item 8 KNOWN LIMITATION; record in the PR body that no runbook/checklist `tail … systemd.err` reference exists; redaction test: injected DSN-password exception -> diagnostic line has no password.
- [x] T4 (#1765) `pyproject.toml`: `tmp_path_retention_policy = "failed"`; test that parses pyproject and asserts the key; full local `uv run pytest -q` (Phase 2): implementer run #1 = 3 failed / 16050 passed / 219 skipped / 2 errors, both errors introduced by the policy (tmp_path finalizer rmtree vs. two tests' global `os.scandir` stubs) and fixed by scoping the stubs with `monkeypatch.context()`; the 3 failures are the pre-existing `test_real_sacct_process_bounds_*` timing flake (green when run alone); orchestrator full re-run at 0156aefc: 16053 passed / 219 skipped / 0 failed / 0 errors (42:30, concurrent with three other pytest sessions; posted as an Evidence comment on the PR).
- [x] T5 (#1765) Governance: exit 1 on any critical recommendation, `RESOURCE_GOVERNANCE_CRITICAL:<code>` stderr lines, receipt unchanged; tests for critical/no-critical/`--quiet`. Unit: `OnFailure=` + `StandardError=journal`; wrapper tee geometry + `LOCK_PATH` default under `$LOG_ROOT`; unit/wrapper tests pin them.
- [x] T6 (#1765) Docs: `/` in capacity checks (`instructions/agents/shared.md` -> regenerate CLAUDE.md/AGENTS.md; `current-production-ops.md`; `node-27-bringup-checklist.md`); tier runbook records the `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp` host discipline (fail-open caveat + volume note) and the two remaining `/tmp` locks as accepted.
- [x] T7 (#1647) `_connect`: `connect_timeout=10` + `options="-c statement_timeout=600000"` defaults, Python-caller override, DSN `connect_timeout` precedence (kwarg omitted when the DSN carries it), stats guard keeps its own; tests for every `_connect` row of the matrix incl. the cancelled >600 s statement (fake backend) and the `application_name` merge unchanged.
- [x] T8 (#1647) `qualified_chunk` fail-closed regex (module constant, pattern byte-identical to `_STATS_GUARD_IDENT_RE`, identity test) + malformed-name test asserting the property raises directly (no fabricated ANALYZE consumer — the module has none) + module-scan test that no f-string SQL names a chunk; stats-guard falsy set + parametrized test; runbook accepted values.
- [ ] T9 node-27 receipts (queued session): (a) pre-merge: forced refused retention tick through the wrapper from a detached worktree with scratch receipt/lock -> wrapper `RC` = runner rc, `retention.log` complete (brackets + diagnostic line), wrapper stderr carries the line; post-merge (deployed unit): `journalctl --user -u … -n 30` shows the diagnostic line and the received (redacted) `OnFailure` mail body carries the `RETENTION_` code; `systemd-analyze --user verify` on the alert **instances** `nhms-node27-unit-failure-alert@nhms-node27-timeseries-retention.service.service` and `…@nhms-node27-resource-governance.service.service`; confirm the deployed checkout contains `58d69970` (#1766 AC1) and record it in the PR comment; (b) governance run with `--root-free-critical-bytes` raised above current free -> exit 1 and the received (redacted) mail body quoted (`sendmail exit 0` is not evidence); (c) one autopipe dry tick from the worktree: receipt stats-guard section unchanged and the tick's longest statement (receipt timings / `pg_stat_activity`) recorded against the 600 s budget; (d) with `mkdir -p /home/nwm/tmp; export TMPDIR=/home/nwm/tmp`, run a pytest subset (not the 40-minute full suite on the production host — recorded deviation from #1765 AC1; the `pytest-of-nwm` landing assertion is the property the AC tests) and assert `pytest-of-nwm` exists under `/home/nwm/tmp` and not under `/tmp`, plus `df -h /` before/after. Post-merge: unit re-install + `daemon-reload`, profile `TMPDIR`.
- [x] T10 `.gitignore` gains `.worktrees/`.

## Non-goals (explicit)

No change to `lock_timeout`, timers, H5 semantics, sibling units, receipt schemas, or any other script's connection timeouts. `RETENTION_CONCURRENT_INVOCATION` / `RETENTION_UNCAUGHT_ERROR` are not counted by the progress criterion (reasons in design Non-Goals).
