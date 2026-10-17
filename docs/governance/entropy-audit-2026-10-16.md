# Repo Entropy Audit — 2026-10-16

Scope: full repo at `origin/master` f7be77f22 (2026-10-12) + node-22 live config (read-only SSH).
Method: AST scanner `scan_py.py` (Python, per-module), two read-only explorer passes (frontend TS, semantics/glossary),
repo governance audit `scripts/governance/audit_repo_entropy.py`, and node-22 systemd/env/mount inspection.
Local checkout was 341 commits behind origin and was fast-forwarded before scanning (clean tree, master).

## 0. Out-of-scope incident found on node-22 (report only)

node-22 rebooted 2026-10-13 16:20 CST. `ghdc-data.mount` (`ghdc:/home/ghdc -> /ghdc/data`, fstab line 24) failed after the
reboot, so `/ghdc/data/nwm` does not exist. Every production root that points there is unreachable:

| Consumer | Setting | Effect since 10-13 |
|---|---|---|
| nhms-compute-scheduler.timer (every 5 min) | `NHMS_SCHEDULER_REGISTRY_MANIFEST=/ghdc/data/nwm/object-store/scheduler/registry/manifest-last.json`, 8 `/ghdc` refs in `compute.scheduler-dbfree.env` | every pass ends `execution_boundary=scheduler_root_preflight_blocked`, 0 candidates, ~1 s CPU |
| nhms-scheduler-file-provider-refresh | `NHMS_SCHEDULER_PROVIDER_STORE_ROOT=/ghdc/data/nwm/object-store` | FileNotFoundError 'nwm' from `packages/common/safe_fs.py:825`, failed 10-15 and 10-16 |
| nhms-node22-refresh-timer-health | reads refresh receipt | `manifest_unavailable`, failed |
| nhms-scheduler-journal-retention | allowed roots include `/ghdc/data/nwm/*` | `preflight_blocked: allowed_root_unavailable` |
| nhms-scheduler-state-index-capacity | destination root | `refused: destination_root root_unavailable` |

No OnFailure alerting exists on node-22 (only node-27 has `nhms-node27-unit-failure-alert@`), so four units have been
failing silently for ~3 days. Worse, the designated stall observer `nhms-node22-scheduler-stall-health` (every 15 min, `scripts/node22_scheduler_stall_health.py`) keeps
reporting `verdict=ok` (last 2026-10-17 09:48 CST) while every pass ends `scheduler_root_preflight_blocked` with zero
candidates: the observer's predicate does not cover the state the observed surface is actually in, which is the
observer/observed predicate-parity risk axis named in `openspec/project-profile.md`.
The NFS server side (`ghdc` export, node-27) was not inspected; the fault may be on either end. Fixing the mount needs root
on node-22 and is outside this audit (ops ticket).

## 1. Entropy Heatmap

Scoring rules applied (from the skill references, stated so the table is reproducible):
- Structure 🔴 = offender in an import cycle, ≥3 upward imports, or >3 mutable module-level containers; 🟡 = cycle participant (not offender), 1–2 upward imports, or 1–2 files importing >5 distinct non-stdlib sources.
- Behavior 🔴 = `Any` ≥5 % of annotations **and** silent swallow (`except: pass/continue`) >5 sites or >2 error idioms in one layer; 🟡 = one of those.
- Context 🔴 = critical module (>5 source files) with no scoped AGENTS.md; 🟡 = small module covered only by root AGENTS.md; 🟢 = scoped file present and fresher than 3 months.
- Protocol ❌ = no scoped instruction file to compare against the template (critical modules); 🟡 = small module, root template only.
- Control 🟡 = rules documented but typing/error rules unenforced (ruff selects only E,F,I; no mypy, no bugbear); 🔴 = known backlog with no tracking or deployed config drift with no check.

| Module | Files | Structure | Semantics | Behavior | Context | Protocol | Control | Priority |
|---|---:|---|---|---|---|---|---|---|
| apps/api | 43 | 🟡 pipeline.py imports 8 sources | 🔴 pipeline.py owns its own terminal set (no `reservation_lost`), cycle-status set and `_STAGE_ORDER`, all diverging from orchestrator/schema | 🔴 Any 12.7 %, 23 `except Exception`, 7 swallow | 🟢 | 🟢 | 🟡 | ⚠️ |
| apps/frontend | 115 | 🔴 pages/hydroMet/bootstrap.ts imported upward by stores+components → 2 dir cycles; 9 files >5 sources | 🔴 status types in 4 places, `failedStatuses` copied | 🔴 3 transports, 6 error normalizers, `any`=0 | 🟢 | 🟢 | 🟡 no ESLint; `check:api-types` not in CI job | ⚠️ |
| packages/common | 79 | 🔴 11 upward imports into workers/* → 5-module SCC | 🔴 lifecycle fallback copied 6×, 13 local source normalizers beside `normalize_source_id`, retired `river_timeseries_legacy` still executed in `node27_pgdata_workload_plan.py:69` | 🔴 Any 23 %, 61 `except Exception`, 35 swallow | 🔴 | ❌ | 🟡 | ❌ |
| packages/scheduler | 3 | 🟢 | 🟢 | 🟡 | 🟡 | 🟡 | 🟡 | ⚠️ |
| services/orchestrator | 113 | 🔴 file_orchestration_journal.py 17 506 lines (103 commits/60 d), 13 mutable module containers, 57 files >500, 9 `globals().update` facades | 🔴 terminal set in 8 files (3 with `reservation_lost`, 5 without), success set copied 6×, `complete/completed/done` all live | 🔴 Any 29.8 %, 104 `except Exception`, 60 swallow, 737 `return None` | 🟢 | 🟢 | 🟡 | ⚠️ |
| services/production_closure | 45 | 🟡 readiness_validation.py 92 alias rebinds | 🟡 QC verdict `passed`/`pass`/`PASS` folded by hand | 🔴 Any 32 %, 15 swallow | 🟢 | 🟢 | 🟡 | ⚠️ |
| services/slurm_gateway | 12 | 🟢 | 🟢 | 🟡 Any 17.9 % | 🔴 | ❌ | 🟡 | ❌ |
| services/tile_publisher | 3 | 🟢 (one 3 211-line file) | 🟢 | 🔴 Any 19 %, 8 swallow | 🟡 | 🟡 | 🟡 | ⚠️ |
| services/tiles | 2 | 🟢 (one 3 197-line file) | 🟢 | 🟡 | 🟡 | 🟡 | 🟡 | ⚠️ |
| services/precip | 7 | 🟢 | 🟢 | 🟢 Any 2.2 % | 🔴 | ❌ | 🟡 | ❌ |
| services/artifacts | 2 | 🟢 | 🟢 | 🟢 | 🟡 | 🟡 | 🟡 | ⚠️ |
| services/m24_live | 2 | 🟢 | 🟡 `PASS/BLOCKED` verdict variant | 🟡 Any 31 % | 🟡 | 🟡 | 🟡 | ⚠️ |
| workers/data_adapters | 8 | 🟡 SCC participant | 🟢 | 🟡 | 🔴 | ❌ | 🟡 | ❌ |
| workers/forcing_producer | 6 | 🟡 SCC participant | 🟢 | 🟡 | 🔴 | ❌ | 🟡 | ❌ |
| workers/mapping_builder | 8 | 🟡 SCC participant; `__init__` re-imports 183 names | 🟡 | 🟡 7 swallow | 🔴 | ❌ | 🟡 | ❌ |
| workers/canonical_converter | 3 | 🟡 SCC participant | 🟢 | 🟡 | 🟡 | 🟡 | 🟡 | ⚠️ |
| workers/model_registry | 28 | 🟢 | 🟡 model/model_instance/model_asset | 🔴 Any 15 %, 19 swallow | 🔴 | ❌ | 🟡 | ❌ |
| workers/grid_registry | 7 | 🟢 | 🟢 | 🟢 Any 0.4 % | 🔴 | ❌ | 🟡 | ❌ |
| workers/shud_runtime | 3 | 🟡 imports upward into services.orchestrator.time_consistency | 🟢 | 🔴 Any 17 %, 9 swallow | 🟡 | 🟡 | 🟡 | ⚠️ |
| workers/output_parser | 3 | 🟢 | 🟡 display-ready set `{succeeded,parsed,failed}` vs `{succeeded,parsed,published}` elsewhere | 🟡 | 🟡 | 🟡 | 🟡 | ⚠️ |
| scripts | 140 | 🟡 82 k lines, select_ci_tests.py 7 623 lines | 🔴 `already_done` run status, Slurm raw-state set missing `REVOKED/SPECIAL_EXIT`, `"GFS,IFS"` vs `"gfs,IFS"` defaults | 🔴 Any 15 %, 129 `except Exception`, 69 swallow | 🔴 | ❌ | 🟡 | ❌ |
| db | 4 | 🟢 | 🟢 | 🟡 | 🟡 | 🟡 | 🟢 CI SQL dry run | ⚠️ |
| infra (env + systemd + sbatch) | 14 examples / 39 units | 🟢 | 🟡 three overlapping compute env families | 🟢 | 🔴 docs still list `compute.env`/compose lane as the node-22 entry without a supersession marker | 🔴 deployed `nhms-compute-api/scheduler` units untracked; deployed `nhms-slurm-gateway.service` ≠ tracked | 🔴 57 env backups, no drift check, no failure alerting | ⚠️ |
| tests (informational) | 576 | 🟡 test_production_scheduler.py 61 249 lines / 1 187 tests | — | — | — | — | — | — |

Semantics pass summary (backend, non-test): 48 naming variants across 9 core concepts (run identity 6, cycle time 8, valid
time/lead 8, river 7, basin 5, source 4, readiness 4, model 3, forcing 3); 6 state entities defined in ≥3 files with
disagreeing value sets; glossary compliance 6/12 (synonyms win for narrow/legacy *table*, source of truth, current
entrypoint); 30 utc-now helpers, 28 ISO parsers, 52 time formatters, 13 local source normalizers.

## 2. Trend vs previous baseline (2026-06-12, commit d5cacfed)

The tracked `.entropy-baseline/latest.json` was produced by `scripts/governance/write_entropy_baseline.py` from the governance
report (token/placeholder checks, docs-centric modules, hard-coded orchestrator/production_closure hotspot overlays). This
audit measures code with a different method, so only the governance-report numbers are directly comparable:

| Metric (governance report) | 2026-06-12 | 2026-10-16 | Trend |
|---|---:|---:|---|
| findings total | 356 | 834 | ⬆ worsening (665 of 834 sit in `openspec/changes/archive`, 518 archived changes) |
| budget-counted (unallowlisted) | 169 | 354 | ⬆ worsening |
| gate-eligible | 4 | 0 | ⬇ improving |
| stale-display-route-token | 230 | 709 | ⬆ |
| placeholder-path-token | 117 | 109 | ⬇ |
| broad-e2e-api-mock | 6 | 14 | ⬆ |
| instruction files | 3 (writer's count) | 4 scoped + root AGENTS.md/CLAUDE.md | ⬆ improving |
| modules with high entropy (writer) | 2 | this audit: 13 of 24 rows have a 🔴/❌ | method-incompatible |
| docs >6 months stale | n/a (not tracked by the writer) | 0 | new metric |
| dead markdown links (docs + instruction files) | n/a | 13 in 7 docs, 0 in instruction files | new metric |

Module score changes where both methods overlap:
- `services/orchestrator` structure/behavior: 🔴 → 🔴 (hotspots moved: the writer names `scheduler.py`/`chain.py`; the live
  hotspot is now `file_orchestration_journal.py`, which did not exist as a hotspot in June).
- `services/production_closure` structure: 🔴 → 🟡 (two_node_e2e_evidence.py 4 512 lines but no cycle or upward import).
- `apps/frontend` behavior: 🟡 → 🔴 (error-handling split now measured directly).

## 3. High-spread patterns (ranked by occurrence; recency = replicated in last 30 days)

| # | Pattern | Occurrences | Modules | Actively spreading |
|---|---|---:|---|---|
| 1 | `Any` in signatures with no type checker | 10–32 % of annotations in 15 of 22 Python modules | all backend | yes: 756 added lines containing `Any` in 30 d |
| 2 | `except Exception` + silent `pass/continue` | 569 broad excepts, 269 swallows (non-test) | orchestrator 104/60, scripts 129/69, common 61/35 | yes: 31 new broad excepts in 30 d |
| 3 | `packages/common` importing `workers.*` (direct_grid_contract, data_adapters.base, mapping_builder.rewrite) | 11 import sites, 7 files | packages/common ↔ 4 workers | yes: `model_registry_contracts.py` added 2026-09-24 |
| 4 | Giant single-file modules (>2 000 lines) | orchestrator 3, common 3, scripts 3, production_closure 3, workers 5, tests 3 | 12 modules | yes: `river_ts_render.py` 2 435 lines created in last 60 d; journal file +103 commits |
| 5 | Backend status vocabularies hand-copied per file and diverging: pipeline terminal set in 8 files (`reservation_lost` only in 3; absent from `schemas/pipeline_job.schema.json` and `apps/api/routes/pipeline.py:61`), success set `{succeeded,complete,published}` copied 6×, run status schema ≠ DB enum ≠ code | 8 files | orchestrator, apps/api, packages/common, scripts | yes: journal file churn |
| 5b | Duplicated helpers: 30 utc-now defs, 28 ISO parsers, 52 `_format_time`, 13 local source normalizers next to shared `normalize_source_id` (68 importers) | 123 defs | all backend | likely |
| 5c | Frontend status/state defined per-file (`PipelineStatus`/`JobStatus` hand-typed vs generated; `failedStatuses` duplicated; `PASS/FAIL/BLOCKED` ×4) | 4 families | apps/frontend | unknown |
| 6 | Frontend API error handling: 3 transports, 6 normalizers, 2 silent-null sites | 11 sites | apps/frontend | unknown |
| 7 | stale-display-route-token in archived specs | 709 | openspec/changes/archive (665) | yes: archive grows every post-merge |
| 8 | node-22 env-file backup copies next to live files | 57 files (`compute*.env.bak-*`, `.pre-*`, `.backup-*`) | infra/env on node-22 | yes: newest 2026-09-23 |

## 4. node-22 stale-config inventory (read-only; nothing changed)

| Item | Evidence | Classification |
|---|---|---|
| NFS `/ghdc/data` mount failed after 10-13 reboot | `ghdc-data.mount failed`, `/ghdc/data` empty | **incident**, see §0 |
| checkout 160 commits behind origin/master | HEAD 7ac79243 (10-08) | lag (ff-only pull per runbook, not done here) |
| `infra/env/compute.env` | June docker-e2e file: `DATABASE_URL=postgresql://nhms:***@nhms-22-e2e-db:5432/nhms`, `SHUD_EXECUTABLE=/bin/true`, gateway `:8000`, image tag `22-e2e-...20260601` | retired-lane artifact; no unit loads it; still named as "本地真实环境文件" in `docs/runbooks/two-node-deployment-overview.md:213` without a supersession marker |
| `compute.host.env` vs `compute.scheduler-dbfree.env` | API+gateway load host.env (`LOOKBACK_HOURS=48`), scheduler loads dbfree.env (`=32`); dbfree.env lacks 7 keys the tracked example has (`LEASE_GUARD_MODE`, `RECONCILE_ABSENCE_SECONDS`, `IDENTITY_BLOCKED_STREAK_LIMIT`, `NO_PROGRESS_CIRCUIT_PASSES`, `TIMING_LEVEL`, `EVIDENCE_RETENTION_DAYS`, `EVIDENCE_MAX_MB`) and carries 46 keys the example does not | two live env families drifting from each other and from the example |
| `compute.replay.env` | last touched 2026-08-03, `EXCLUDE_NODES=cn11,cn24` (live has `cn11,cn21,cn24`) | stale replay profile |
| 57 backup env files | `compute.scheduler-dbfree.env.bak-*` ×38 etc., 2026-06-07 … 2026-09-23 | unmanaged backups beside live config |
| deployed units not in `infra/systemd/` | `nhms-compute-api.service` (2026-06-12), `nhms-compute-scheduler.service` (06-29) + `.timer` | production units exist only on the host |
| deployed `nhms-slurm-gateway.service` ≠ tracked | plus drop-in `10-node22-live.conf` overriding EnvironmentFile/ExecStart | unit drift |
| stale drop-ins / backups under `~/.config/systemd/user/` | `nhms-compute-scheduler.service.d/disabled/issue-874-concurrency.conf.20260705…`, `nhms-scheduler-file-provider-refresh.service.pre-no-private-tmp-20260718…`, dirs `gateway-rollout-backup`, `gateway-rollout-backups`, `issue-1684-deploy-backups`, `retired-20260630`, `runtime-path-recovery` | leftover rollout artifacts |
| `.venv` = anaconda 3.12.7 vs `.python-version` 3.11 | symlink to `/opt/anaconda3/2024.10/bin/python3` | known, tracked by #1831; not a new finding |
| 6 git stashes (May–July) | `presync-node22-tracked-dirty-20260704…`, `pre-m0`, … | stale; runbook forbids auto-pop, so they need a manual decision |
| `.nhms-work/` untracked (25 files) | not in `.gitignore` (local or 22) while `.nhms-runs/`, `.nhms-workspace/`, `.nhms-object-store/` are | gitignore gap; pollutes `git status --porcelain` gate before ff pulls |
| `.nhms-runs` 14 GB, both entries >60 days (`qhh-continuous`, `qhh-smoke`) | /scratch 63 % used | retention candidate |
| foreign crontab entry `yd-producer` hourly | unrelated project on same account | note only |
| 25 tracked `nhms-node27-*` units are not deployed on 22 | expected (27-only) | none |

## 5. Local housekeeping

- Stale worktree `.claude/worktrees/subagent-workflow-merge-997bfd` (detached at f7be77f22, own `.venv`, duplicates AGENTS.md matches).
- Untracked `openspec/config.yaml` (`schema: spec-driven`) — decide track or ignore.
- `tests/test_entropy_audit_baseline_writer_summary.py:76-77` pins the tracked baseline's `total_test_files == 247` and
  `total_instruction_files == 3`; any rewrite of `.entropy-baseline/latest.json` must update that test in the same commit.

## 6. Cleanup priorities (impact × inverse effort)

| # | Target | Impact | Effort | Axis | Route |
|---|---|---|---|---|---|
| 1 | node-22 control gap: add `OnFailure=` unit-failure alerting (mirror `nhms-node27-unit-failure-alert@`) and make the stall probe treat `scheduler_root_preflight_blocked` with zero candidates as unhealthy | high | low | control | `gh-create-issue` (the mount itself is an ops ticket, see §0) |
| 2 | Track the node-22 production units (`nhms-compute-api/scheduler` + timer + gateway drop-in) in `infra/systemd/`, add a deployed-vs-tracked diff probe to `node22_*_health.py` | high | low | protocol/control | `gh-create-issue` |
| 3 | Retire `infra/env/compute.env` + 57 backups into `~/NWM-presync-backup-<date>/`, mark the compose lane sections in `two-node-deployment-overview.md` superseded, reconcile dbfree.env with its example (7 missing keys) | high | low | context/control | `project-documentation` + ops |
| 4 | gitignore `.nhms-work/`; drop the stale local worktree; decide `openspec/config.yaml` | medium | trivial | control | direct PR |
| 5 | Frontend: move `pages/hydroMet/bootstrap.ts` to `stores/` or `lib/`, delete hand-typed `PipelineStatus`/`JobStatus` in favour of generated aliases, single `failedStatuses`, one error normalizer, add ESLint (`no-explicit-any`, import/no-cycle) and wire `check:api-types` into the frontend CI job | high | medium | structure/semantics/behavior | `implementation-planning` → issues |
| 6 | Break the `packages/common → workers` cycle: move `workers/forcing_producer/direct_grid_contract.py`, `workers/data_adapters/base.py` (+`region`), `workers/mapping_builder/rewrite.py` contracts into `packages/common` (or a new `packages/contracts`) and enforce with an import-linter contract in CI | high | medium | structure | `implementation-planning` |
| 7 | Error-model hygiene: enable ruff `B` (bugbear: B001/B902 bare/broad except), `BLE001`, and `ANN401` (`Any`) in warn-then-fail stages; start with scripts/ and services/orchestrator | high | medium | behavior | `entropy-review` + staged ruff rollout |
| 8 | Decompose `services/orchestrator/file_orchestration_journal.py` (17.5 k lines, 103 commits/60 d) and `scripts/select_ci_tests.py` (7.6 k) | high | high | structure | `implementation-planning` |
| 8a | Centralize state vocabularies (pipeline terminal/success sets incl. `reservation_lost`, hydro run status, cycle status, QC verdict, Slurm raw states) in `packages/common` with schema/DB-enum parity tests | high | medium | semantics | `grill-with-docs` (glossary) → `implementation-planning` |
| 8b | Consolidate utc-now / ISO-parse / format / source-normalizer helpers onto the shared ones | medium | low | semantics | search-replace refactor |
| 8c | Drop `LEGACY_HYPERTABLE` (`hydro.river_timeseries_legacy`) from `packages/common/node27_pgdata_workload_plan.py:34,69,397`; purge retired-store wording from `tier-node27-timeseries-storage.md`, `service-bringup.md:69`, 5 active specs | medium | low | semantics/context | `project-documentation` |
| 9 | Scoped AGENTS.md for the 9 critical modules without one (packages/common, workers/model_registry, slurm_gateway, data_adapters, mapping_builder, grid_registry, forcing_producer, precip, scripts) using the existing 4-section template | medium | low each | context/protocol | `project-documentation` |
| 10 | Governance report noise: exclude `openspec/changes/archive` from `stale-display-route-token` or allowlist archived changes wholesale (665 of 834 findings) | medium | low | control | `stage-change-pipeline` (governance rule change) |
