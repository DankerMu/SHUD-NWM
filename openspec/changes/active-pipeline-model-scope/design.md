## Context

`has_active_pipeline` 先用宽口径 `_job_matches_candidate` 收集 candidate jobs，再用其中任一 terminal-success completion 抑制 ACTIVE hydro 占位符。宽口径有意让 exact cycle-run 行进入 duplicate-submission scans，但他模型具名完成行不是本候选的完成证据；把“可见”直接当“可抑制”会令 journal 返回 False，而 `chain_repository.py:57-96` 的 DB gate 由候选三元限定 ACTIVE hydro 与非终态 job 两臂纯 union，返回 True。#1302 已局部收窄 completed gate，并留下 #1472 的显式行为锚。

Risk triage: bugfix；SHUD profile；blast radius medium；fixture level **expanded**（orchestrator、persisted/shared state transition 强制触发）；upstream suggested level absent；repair intensity **high**（共享 file-journal gate，错误削弱重复投递/幂等防线）。

## Goals / Non-Goals

**Goals:**

- 他模型具名 exact cycle-run 完成行不得抑制本候选的 ACTIVE hydro run。
- 候选自身完成与 model-less cohort 完成继续抑制 stale ACTIVE hydro 占位符。
- 他模型 ACTIVE exact cycle-run 行继续被 active-pipeline / active-slurm-jobs 宽口径看见。
- journal 与 DB gate 在“本候选 hydro 仍 ACTIVE + 他模型已完成”形状上同向为 active。

**Non-Goals:**

- 不修改共享 `_job_matches_candidate`，不改变 candidate-state 投影或 `has_completed_pipeline`。
- 不改变 model-less cycle-scope cohort 的 cycle-wide 契约。
- 不处理 `scheduler_candidates.py` 无 state-provider 分支的可达性、#1288 candidate-state seam、backfill 探针 fail-open 或任何 writer/journal schema。
- 不修改 sbatch、Slurm gateway、资源配置或 SHUD runtime。

## Decisions

### D1: 在 active gate 的 suppression 合取中局部排除 foreign-model completion

`has_active_pipeline` 的 `has_terminal_completion` 增加：

```python
and not _is_foreign_model_cycle_scope_job(
    job,
    source_id=canonical_source_id,
    cycle_time=cycle_time,
    model_id=model_id,
)
```

复用 #1288 的单一谓词，不复制身份口径。该谓词对 self 和空 `model_id` 恒 False，因此只移除错误的 foreign suppression；`candidate_jobs` 和尾部 `_job_is_active` 扫描不变。

### D2: 否决删除 suppression 与收窄共享谓词

整条删除 suppression 会让现存“候选自身 terminal completion + stale created/staged hydro”fixture 重新判 active，造成假阳性 `PIPELINE_ALREADY_ACTIVE`。收窄 `_job_matches_candidate` 则会让 foreign ACTIVE cycle-run 行从两个 duplicate-submission scans 消失，重开 #1288 已证伪的去重缺口。局部合取是唯一同时保住两边的最小方案。

### D3: 契约区分 row visibility 与 suppression authority

- Visibility: exact cycle-run foreign rows仍进入 `candidate_jobs`；ACTIVE 行仍可令 `has_active_pipeline=True`，也仍进入 `active_slurm_jobs`。
- Authority: terminal-success foreign row不能成为本候选的 completion proof，因而不能压掉本候选的 ACTIVE hydro 臂。
- Self/model-less: 候选自身与 model-less cohort terminal completion仍有 suppression authority。
- DB alignment: `chain_repository.py:57-96` 没有 terminal suppression；foreign terminal 行不能改变三元限定 ACTIVE hydro 臂的 True。

### D4: 测试使用真实 file-journal 读路径与判别矩阵

批量 red proof 在改生产码前运行。主矩阵覆盖四种 ACTIVE hydro 状态 × 三种 completion stage，并单独覆盖生产 `forecast_state_save_qc` 终态开关。控制臂覆盖 own candidate-run、own named cycle-run、model-less exact/suffix cohort、foreign queued，以及 `has_completed_pipeline` / `active_slurm_jobs` 不变。测试只走公开 repository methods，不直接测私有谓词作为主 oracle。

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — repository gate 被 scheduler、cycle control 和库 API 消费；返回值是共享入口契约。
- Config / project setup: selected — 生产终态 stage 环境开关是必测分支；不改配置格式。
- File IO / path safety / overwrite: not selected — 不新增或修改路径、解析、写入、删除行为。
- Schema / columns / units / field names: not selected — journal/DB schema 与 payload 字段不变。
- Auth / permissions / secrets: not selected — 无信任边界或凭据变化。
- Concurrency / shared state / ordering: selected — gate 保护跨模型共享 cycle 的重复投递/幂等顺序。
- Resource limits / large input / discovery: not selected — 单行纯谓词，无新扫描或无界输入。
- Legacy compatibility / examples: selected — self completion、model-less cohort 与 foreign ACTIVE 宽可见性必须逐位保持。
- Error handling / rollback / partial outputs: selected — journal 读失败的 fail-closed True 及 stale hydro suppression 不变；不产生输出。
- Release / packaging / dependency compatibility: not selected — 无依赖、打包或部署形状变化。
- Documentation / migration notes: selected — 注释、delta 与 #1470 freeze→#1472 unfreeze 同步；无需数据迁移。

SHUD domain packs considered:

- Slurm production lifecycle / mock-vs-real parity: selected — duplicate-submission verdict必须保持宽 ACTIVE 行可见；确定性 repository fixture 是本改动 oracle。
- Run manifest / QC provenance: not selected — 不改 manifest、QC 或 evidence payload。
- Geospatial / CRS / basin geometry; hydro-met time series / forcing windows; numerical / conservation / NaN; solver/threading; PostGIS/Timescale; external providers; published artifacts/display identity: not selected — 均不在此次 gate 读判定面。

## Invariant Matrix

Governing invariant: 只有候选自身或 model-less cohort 的 terminal completion 可抑制该候选的 ACTIVE hydro 占位符；foreign-model named completion 不可抑制，但 foreign ACTIVE exact cycle-run row 在 duplicate-submission scans 中仍保持宽可见。

Source-of-truth identity/contract: `_is_foreign_model_cycle_scope_job`（non-empty foreign `model_id` + exact `cycle_<source>_<stamp>` run id）是唯一 foreign identity predicate。

Surfaces:

- Producers: journal pipeline/hydro rows不变；无 producer 修改。
- Validators/preflight: `has_active_pipeline` 本地 terminal-completion 合取是唯一改动点。
- Storage/cache/query: `_cycle_rows`、`_job_matches_candidate` 与 DB query不变；DB gate作为方向对照。
- Public routes/entrypoints: repository `has_active_pipeline`；无 HTTP/CLI 变化。
- Frontend/downstream consumers: scheduler candidate gate、cycle-level conflict gate、trigger facade、backfill predecessor probe均只接收修正后的 bool。
- Failure paths/rollback/stale state: self/cohort completion继续压制 stale hydro；journal read failure继续 fail-closed active。
- Evidence/audit/readiness: 参数化真实 journal tests、focused/full regression、OpenSpec strict validation。

Regression rows:

- ACTIVE hydro `{created, staged, submitted, running}` + foreign named terminal completion at `{state_save_qc, publish, parse}` -> active True；生产终态开关下 `state_save_qc` 仍 True。
- stale ACTIVE hydro + self candidate-run or self named cycle-run terminal completion -> active False。
- stale ACTIVE hydro + model-less exact or suffix cycle-scope terminal completion -> active False。
- foreign queued exact cycle-run row -> active True and active-slurm-jobs visibility unchanged；completed gate unchanged False。
- journal read failure -> fail-closed active True unchanged。

## Boundary-Surface Checklist

- Shared helper roots: `_is_foreign_model_cycle_scope_job` reused unchanged；`_job_matches_candidate` unchanged。
- Public entrypoints/read surfaces: `has_active_pipeline` changed；listed consumers inspected for bool-only compatibility。
- Write/delete/overwrite and staging/publish/rollback: none — no code path touched。
- Producer/consumer evidence boundaries: journal latest/direct job fixtures exercise the real aggregation path；no payload changes。
- Stale-state/idempotency: self/cohort stale hydro suppression and foreign ACTIVE duplicate detection have explicit controls。
- Unchanged downstream consumers: `has_completed_pipeline`, `active_slurm_jobs`, candidate-state projection and DB repository remain unchanged and covered.

## Risks / Trade-offs

- [Risk] 误把 model-less cohort 当 foreign 会重开 stale hydro 假活跃。 → 复用 null-aware helper并测试 exact/suffix model-less rows。
- [Risk] 收窄共享 row visibility 会放松去重。 → 禁改 `_job_matches_candidate`，用 foreign queued + active-slurm-jobs oracle钉住。
- [Risk] 默认 terminal stage 与生产开关行为漂移。 → 两种 contract分别测试，且不缓存 env。
- [Risk] 只翻 #1470 单断言形成 happy-path coverage。 → 参数化完整 hydro×stage 矩阵并保留 self/cohort controls。

## Migration Plan

无数据迁移。单谓词修改可通过回滚本 PR 恢复；无新持久化格式。OpenSpec delta在 merge 后归档进主规格。

## Open Questions

None. 删除 suppression 的备选已被现存 self-completion fixture证伪。
