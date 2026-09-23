# node-27 ops scripts fail-closed (batch G1)

## Why

Seven node-27 operator-lane defects, one PR at the user's request (batch G1). Each is a place where an ops script either does the destructive/unsafe thing when asked not to, dies without leaving evidence, or stays silent / cries wolf.

- **#2355 (p1)** — `scripts/node27_timeseries_retention.py` H13 resolver never reads `args.dry_run`: with `NODE27_TIMESERIES_RETENTION_ENFORCE` truthy in the sourced env, `--dry-run` resolves to `enforce=True` and enters `drop_chunk`. Hit in production on 2026-09-19 (a chunk dropped on a "dry-run"). `--help` says "dry-run (default)"; the env example documents the inversion as contract.
- **#2309 (p2)** — `scripts/node27_raw_retention.py::_dir_size` iterates `path.rglob("*")` outside any `try`; a non-permission `OSError` (ESTALE/EIO) during traversal escapes through `collect_targets` → `run_retention` → `main`: whole tick dies, zero deletions on all three lanes, no summary.
- **#2284** — `scripts/node27_mvt_cache_retention_once.sh` names its summary with second resolution; two runs in one second (the runbook's plan-only-then-production flow) overwrite each other.
- **#2283** — `scripts/node27_autopipe_cron.sh`: `cmd || echo "[$(ts)] ... rc=$?"` logs `rc=0` because `$(ts)` runs first; two sites (coverage backstop, MVT prewarm).
- **#2529 (p2)** — autopipe parse-stage `OUTPUT_PARSE_DB_ERROR` (57014 statement timeout) bursts on 2026-09-19 were silent: the autopipe unit has no `OnFailure=`, frontier (4h) and coverage (day-scale) alerts cannot reach an hour-scale failure, retries are unbounded and uncounted. A test docstring claims an `OnFailure=` that does not exist.
- **#2504 (p2)** — the #1446 overwrite guard (`display_coverage.py`: `WHERE force OR EXCLUDED.segment_count > 0 OR current = 0`) freezes a populated coverage row forever once its facts legitimately vanish (retention); `--skip-fresh` never rescans it. Display lists the run, the curve is empty. Measured on node-27 today (read-only, bounded): 2616 of 2716 published, populated, >21-day-old runs have zero river facts.
- **#2464 (p2)** — `scripts/node27_coverage_freshness_alert.py` discovers source keys from ingest (open set) but observes the display surface pinned to `Literal["gfs","ifs"]`; a non-display source (ERA5 is wired) would raise a permanent, uncleareable daily `no-covered-cycle` alert. Now live on master (PR #2462 merged).

## What Changes

- **#2355** — explicit precedence in `config_from_args`: `--dry-run` ⇒ `enforce=False`; else `--enforce` ⇒ True; else the env fallback. Help text and `infra/env/node27-timeseries-retention.example` stop claiming the opposite. Regression with a real eligible chunk and a drop spy.
- **#2309** — `_dir_size` traversal failure localises to the target: the target becomes a `skipped[]` entry via `_unavailable_skip` (with `error`/`error_type`), never planned; other targets and lanes proceed; summary always written. Env-example wording corrected.
- **#2284** — summary path unique per run (exclusive create with a numeric suffix on collision); two same-second runs leave two files.
- **#2283** — capture the command's rc before any expansion at both `(non-fatal)` sites.
- **#2529** — decided alert shape (design D5, also in the PR body): a new DB-only **residency** observer `scripts/node27_parse_failure_residency_alert.py` + user unit/timer. It reads `hydro.hydro_run` rows with `status='failed' AND error_code LIKE 'OUTPUT_PARSE_%'`, keeps a small state file of first-observed-failing time per run, and exits non-zero only when a run has stayed failing across its observations for ≥ a threshold (default 2 h), with per-run dedup and a 24 h re-alert. Delivery is the existing `OnFailure=nhms-node27-unit-failure-alert@%n.service` handler, with the unit's stdout on the journal (not `append:`), so the handler's journal excerpt carries the observer's report as a non-empty body. The autopipe unit itself gets **no** `OnFailure=` (every transient rc=1 tick would mail, and its `append:` stdio would leave the body empty). `OUTPUT_PARSE_DB_ERROR` is **not** split (the observer keys on the `OUTPUT_PARSE_` prefix; the retry path does not read the code; splitting changes persisted codes for no behaviour gain). Root-cause receipt from node-27 is committed as a runbook receipt. Stale `OnFailure=` docstrings are corrected.
- **#2504** — decided target state (design D6, also in the PR body): outside the retention window a vanished fact is the expected truth for every cohort, so an ordinary refresh may lower a populated row to 0 there; inside the window the #1446 guard stands unchanged. `--all --skip-fresh` additionally rescans populated rows whose `river_valid_time_end` is older than the retention cutoff, so convergence is automatic. The window has one source of truth shared with the retention runner. A repeatable read-only audit reports populated-but-empty rows (in-window vs out-of-window). Guard header and `_REFUSAL_ADVICE` updated.
- **#2464** — a shared display-source alias (`packages/common/source_identity.py`) used by the three `hydro_display.py` routes and by the alert lane's allowlist; a non-display key reports `not-evaluated` / `unsupported-source` and never enters the exit code; pin test ties the allowlist to the route enum.

## Triage

```text
Issue type: bugfix (7) + ops alerting lane
Fixture level: expanded
Upstream suggested level: absent
Blast radius: production retention deletes (#2355), raw-retention tick liveness (#2309), coverage rows the national display lists (#2504), operator mail volume/credibility (#2529, #2464), ops log/summary evidence (#2283, #2284)
Selected risk packs: Public CLI/script entry; Config; File IO/overwrite; Error handling/partial outputs; Schema/field names (receipt/report fields); Legacy compatibility (#1446 guard, exit codes); Concurrency/ordering (retention × parse overlap evidence); Documentation
Evidence floor: focused pytest per lane + ruff + openspec; node-27 isolated oracle (scratch PG) for DB-touching suites; node-27 read-only root-cause receipt (#2529) and audit receipt (#2504); node-27 live receipt of the residency observer through the real OnFailure handler on a scratch DB.
```

## Capabilities

### New Capabilities

- `autopipe-parse-failure-residency`: the parse-stage failure residency observer and its alert contract.

### Modified Capabilities

- `timeseries-db-retention`: explicit `--dry-run` wins over the enforce env toggle.
- `node27-raw-retention`: a traversal failure retires only its target.
- `mvt-tile-cache-lifecycle`: one summary file per run.
- `display-coverage-freshness`: cron non-fatal rc logging; frozen-row convergence outside the retention window + audit; alert source allowlist.

## Impact

- Code: `scripts/node27_timeseries_retention.py`, `scripts/node27_raw_retention.py`, `scripts/node27_mvt_cache_retention_once.sh`, `scripts/node27_autopipe_cron.sh`, new `scripts/node27_parse_failure_residency_alert.py`, `packages/common/display_coverage.py`, `scripts/node27_refresh_coverage.py`, `packages/common/storage.py` (window resolver), `scripts/node27_coverage_freshness_alert.py`, `packages/common/source_identity.py`, `apps/api/routes/hydro_display.py`.
- Units: new `infra/systemd/nhms-node27-parse-failure-residency-alert.{service,timer}` + env example. **Not enabled on node-27 by this PR** (deployment is a separate operator action; the receipt uses a transient unit).
- Tests: the lanes' existing test modules plus a new module for the residency observer (routed in `scripts/select_ci_tests.py` with a routing pin).
- Docs: env examples, `docs/runbooks/` production-ops pages for the new lane / new reason / convergence, a root-cause receipt for #2529.
- Commit gate: add `packages/common/display_coverage.py` (1033) and `tests/test_node27_mvt_cache_retention.py` (1783) and any other staged >1000-line file to `.large-file-guard.json` `exclude`.
- Runtime: no migration. Production convergence of the frozen rows happens only once node-27 is deployed with this code (the autopipe backstop runs `--all --skip-fresh`); that deployment is out of this PR.
