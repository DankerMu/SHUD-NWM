# Design: completed-pipeline-model-scope

## Context

`has_completed_pipeline`（`file_orchestration_journal.py:543-568`）是 db-free
完成判定的唯一产生点，四个消费面（trigger / generation gate / discovery /
candidates）把它当"本候选已完成"。`:558-563` 的 job 合取项只有
`_job_matches_candidate`，其 `:8815` 无条件 cycle-run_id 分支让他模型具名
行命中；`:556-557` 的 hydro 守卫拦不住（`_filter_cycle_rows_for_model` 已把
不匹配 hydro_run 置 None，None 不触发 return False；候选自己 hydro 匹配时
同样落到 has_terminal_completion）。DB 对照 `chain_repository.py:97-111`
只查 `hydro.hydro_run` 三元限定，他模型 job 行恒不可见。

## Decisions

### D1: 修复落点 = gate 本地合取，复用 `_is_foreign_model_cycle_scope_job`（采纳 issue 推荐）

`has_completed_pipeline` 的 `has_terminal_completion` 生成器（:558-563）
追加一项：

```
and not _is_foreign_model_cycle_scope_job(
    job, source_id=canonical_source_id, cycle_time=cycle_time, model_id=model_id
)
```

- 该谓词（:8755-8775，#1288 交付）= `model_id` 非空 ∧ ≠ 候选 ∧ run_id
  **恰为** `cycle_<source>_<stamp>`——与 issue 推荐的「run_id == cycle_run_id
  命中时要求 `job.model_id in (None, "", 候选)`」逐位等价：
  - 他模型具名 + **带 suffix** 的 cycle run id 行根本不命中
    `_job_matches_candidate`（:8820-8823 suffix 臂要求 model-less；:8815
    精确臂 run_id 不含 suffix 形；:8816 要求 model 相等）——无需覆盖。
  - 他模型具名 + 候选自己 `fcst_...` run_id 的病态形在两条读路径上都算
    候选行（谓词 docstring :8766-8767 已记载），不属本 issue 域。
- 共享谓词 `_job_matches_candidate` 逐字不动（Non-Goals 第一条；#1288
  design D1 的证伪结论直接复用）。
- 两条返回分支都被单点覆盖：`:564-565` 终态口径开关开（生产
  `forecast_state_save_qc`）时直接 `return has_terminal_completion`；
  关时 `:566-567` hydro 完成优先、`:568` 仍落 has_terminal_completion——
  合取项收窄对两分支同时生效，无需第二处改动。

否决备选（消费侧交叉校验）：同一判定要在 4 个消费面各写一遍、口径易漂移，
且 gate 本身仍返回错误答案（issue 已裁）。
否决反模式（收窄共享谓词）：见 Non-Goals。

### D2: 契约方向 = 向 DB hydro-run 语义靠拢（gate 语义裁定）

`has_completed_pipeline` 回答的是候选作用域问题（"THIS candidate 是否
完成"）。完成证据的合法来源收敛为三类：候选自有行（own run_id 或 own
model_id）、model-less cohort 完成行（cycle-wide，全体候选经它完成——
#841 契约保留，DB 无 job-row 对照但该宽度是有意的）、候选自己的完成
hydro run。他模型具名行是**别人的**完成。journal 与 DB 在该形上的判定
方向从分歧变同向；注释与 spec 显式指向 `chain_repository.py:97-111`。

**写入侧证据（无假阴性风险，fixture review 补强）**：
`chain_runtime_utils.py:65-68` `_cycle_pipeline_job_model_id` 只在
`len(all_basins) == 1` 时给 cycle-run 行具名，多流域 pass 一律返回 None
（即 model-less cohort 行）。所以「具名 cycle-run 行」必然只属于那一个
model——排除它绝不会吞掉多模型 cohort 的真实完成，修复不引入「该完成却
判未完成」的反向风险。

### D3: 解冻链 —— 测试 / 注释 / spec 三处必须同步

#1288 交付把本 gate 行为冻结在三处，全部解冻，缺一即自相矛盾：
1. 测试 `test_foreign_model_cycle_run_row_stays_visible_to_the_completion_gate`
   （`tests/test_file_orchestration_journal.py:1589-1610`）：断言 True→False；
   docstring 改写为「#1288 冻结、#1302 解冻」+ 新对齐方向（DB hydro-run
   三元限定）。测试名随语义改（stays_visible 已不成立），旧名不保留。
2. 投影处注释 `file_orchestration_journal.py:721-729`：
   「`has_completed_pipeline` has no DB job-row counterpart at all …
   Narrowing the shared predicate would …」段落改写——排除逻辑分两层：
   投影面（#1288）与 completion gate 本地合取（#1302），共享谓词仍不收窄，
   duplicate-submission 两 gate 仍宽。
3. spec :499-506 契约句 + scenario :540-543 AND 句（delta 已承载）。
   另一处冻结测试
   `test_foreign_model_cycle_run_row_stays_visible_to_the_duplicate_submission_gates`
   （:1559-1586）**不动**——它冻结的是 active 面，仍成立。

### D4: 消费面传导枚举（不改码，行为经返回值传导）

| 消费面 | 位置 | 修前 | 修后 |
|---|---|---|---|
| canonical trigger | `chain_forecast_trigger.py:378-381` → `:247-251` 裸 continue | 他模型完成行 → 候选整趟不触发、零记录 | gate False → 候选正常进入触发评估 |
| §8 warm-start 门 | `scheduler_generation_gate.py:130-160` 经 `scheduler_core.py:743-751` | 判 True → return None，strict 证据整趟不建 | gate False → 证据构造正常走 |
| discovery | `scheduler_discovery.py:234-249`（provider 挂 `scheduler_core.py:519`） | 一条他模型行 → 整 cycle 判 complete，backfill 跳过 | gate False → cycle 不再被误判 complete |
| candidates skip | `scheduler_candidates.py:226`（provider getattr）→ `:378-386`（`completed_duplicate_pipeline` 分支） | journal 仓储下本就不可达（:382-384 合取 `not callable(state_provider)`，`FileOrchestrationJournalRepository` 自带 `candidate_state`） | 不变；不可达性如实记载，不测 |

消费面锚：trigger 面（后果最硬、静默无记录）用真实
`FileOrchestrationJournalRepository` 落一条判别锚（tasks 2.6）；其余面由
gate 级真值表 + 全量套件覆盖，不逐面重复造锚（不可达面明确不测）。

### D5: 真值表（gate 级判别锚的完备形状集）

| 形状 | 修前 | 修后 |
|---|---|---|
| 他模型具名 + cycle run id + succeeded + stage ∈ {state_save_qc, publish, parse}（默认口径） | True | **False** |
| 同上 + `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`，stage=state_save_qc | True | **False** |
| 同上开关下 stage ∈ {parse, publish} | False | False（本就非终态） |
| 他模型行在场 + 候选自身 hydro ∈ {failed, cancelled, created} + 自身 forecast 行 failed | True | **False** |
| model-less cohort 完成行（run_id == cycle run id 及 `_<suffix>` 形；默认口径——生产口径下仅 state_save_qc 形为 True，publish/parse 本就 False） | True | True |
| 候选自身具名 cycle-run 完成行 / 自身 fcst run_id 完成行（终态阶段按当前口径）/ 自身 hydro 完成（**仅默认口径**——生产口径 :564-565 先行返回，不看 hydro 完成臂） | True | True |
| 他模型 stage=forecast succeeded（非终态） | False | False |
| 同 fixture 下 `has_active_pipeline` / `active_slurm_jobs` | — | 逐字不变 |

环境开关测试直接 `monkeypatch.setenv` 即可——
`_compute_state_save_qc_terminal_enabled`（`chain_repository_state.py:71-72`）
每次读 env、无缓存（fixture review 实测）。

## Invariant Matrix

Governing invariant: db-free 完成判定是候选作用域的——他模型具名行永不
构成本候选的完成证据；cohort（model-less）完成与候选自身完成的既有 True
契约逐位保留。
Source-of-truth identity/contract: foreign-model named cycle-run row =
`_is_foreign_model_cycle_scope_job`（#1288 唯一定义，本 change 新增第二个
消费点，不复制口径）。
Surfaces:
- Producers: `has_completed_pipeline`（唯一改动点）；共享谓词
  `_job_matches_candidate` 不动
- Validators/preflight: 无新增
- Storage/cache/query: `_cycle_rows` 行集通道不动；DB 对照
  `chain_repository.py:97-111` 只读参照
- Public routes/entrypoints: 无
- Frontend/downstream consumers: D4 四消费面（不改码，行为传导枚举 +
  trigger 面判别锚）
- Failure paths/rollback: 候选自身失败形不再被他模型行翻成"已完成"——
  修复靶点本身
- Evidence/audit/readiness: §8 warm-start 证据从"整趟丢失"恢复为正常
  构造（行为传导，D4 记载）
Regression rows: 见 D5 真值表后四行 + D3 解冻三处同步。

## Risks / Trade-offs

- 修后他模型完成行不再让候选判完成 → 此前被静默跳过的候选将真实触发
  forecast——这是修复意图；若生产有依赖该误判的"顺带去重"，属用错 gate
  （active 面才是去重闸，其行为不变）。
- `has_active_pipeline` 内部 has_terminal_completion 仍含他模型完成行
  （抑制 hydro-active 分支）——active 面语义，显式 Non-Goal，负向回归钉住。
- 环境开关（`_compute_state_save_qc_terminal_enabled`）无缓存、每次读
  env（实测），测试 `monkeypatch.setenv` 即可，无陷阱。

## Migration

无数据迁移。主 spec 措辞随 merge 后 `openspec archive` 由 delta 回写。
