# Design: pre-guard-permanence-gate (#1313)

行号钉在 master `e7a8fa95`（explorer 重钉 + fixture round-1 评审抽查 12 处
确认）。fixture round-1 评审（P1×3/P2×6）推翻初稿三项裁决，本版为重裁后
形状；初稿错误与重裁理由内嵌各节。

## Risk packs

- Selected: terminal-state-semantics（决策梯 permanence 语义）·
  oracle-integrity（9 条现绿测试受影响，逐条重判、双向红绿）·
  spec-compliance（unknown-default MUST NOT 对齐 + 与 base spec OOM 条款
  的调和）。
- Not selected: concurrency / performance / data-migration / security
  （纯决策函数）。

## D0 — 生产码景观（round-1 P1-1/P1-3 实测，一切裁决的事实基座）

- 真实 writer 产出的失败码：`SLURM_TIMEOUT`/`NODE_FAILURE`/
  `OUT_OF_MEMORY`/`SLURM_JOB_FAILED`（`services/slurm_gateway/real_backend.py:144-152`）、
  `{JOB_TYPE}_{STATUS}` 兜底如 `CONVERT_CANONICAL_FAILED`
  （`chain_forecast_execution.py:696`）、`SLURM_ARRAY_TASK_FAILED`
  （`file_orchestration_journal.py:2972,2982`）、tile/publish 侧
  `OBJECT_STORE_COPYBACK_FAILED`/`QDOWN_PUBLISH_FAILED`/
  `PUBLISH_TILES_FAILED`/`Q_DOWN_DISPLAY_NOT_READY` 等。
- `INVALID_MANIFEST` 唯一 writer 是 mock backend（`mock_backend.py:50`）；
  `MANIFEST_SCHEMA_INVALID`/`MALFORMED_INPUT` 全仓零 writer（仅存在于
  retry.py 判据表）。**任何按"输入修复类码"做 allow-list 的判据在生产判
  别力为零。**
- reader 合成占位码：`_downstream_retry_evidence` 在 state 无 error_code
  时自造 `{FAILED_STAGE}_FAILED` 默认（`scheduler_state_failure.py:246`）；
  `_failure_policy_payload` 无码时给 `UNKNOWN_FAILURE`（`:102`）。
  `PARSE_FAILED`/`PUBLISH_FAILED` 等作为占位符全仓无 writer。spec
  `:166-171` 管的是"a pipeline_job **fails with** an error code"——真实落
  库的码；合成占位符不是该条款的证据。

## D1 — 修代码还是修规格（裁决：修代码；范围=真实记录码）

spec `:166-171` unknown-default MUST NOT 是已批准条款；`OUTPUT_INCOMPLETE`
是 base spec `:148` 显式列举 non-transient 却今天可 resume（比
PARSE_FAILED 更硬的违规）；`WARM_START_CHECKPOINT_RETRY` 已有 anchor 钉
"必须 permanent_failure_guard"（`tests/test_production_scheduler.py:19033-19064`）。
permanence 判据的适用域限定为 **state 携带真实记录码** 的失败；合成占位
码不受该条款约束（D4）。受影响的"需求形"现绿测试（`:17994`/`:18058`）与
spec 条款直接冲突，裁决 spec 胜——它们钉的行为正是本 issue 判为缺陷的洗
白结构（重判逐条见 D4b）。

## D2 — 单一判据源：remedy 类别 × 分类的裁决表（round-1 P1-1 重裁）

新增共享 helper（`scheduler_state_failure.py` 模块级）：

```python
_REMEDY_NON_CAUSAL_CLASSIFIERS = frozenset({
    "resource_configuration",   # OUT_OF_MEMORY：资源病
    "policy_blocked",           # POLICY_BLOCKED / PERMISSION_DENIED / TEMPLATE_NOT_ALLOWED（retry.py:183）
})
_REMEDY_NON_CAUSAL_CODES = frozenset({"OUT_OF_MEMORY"})  # 码臂必须保留，见下
def _remedy_permits_permanent_failure(failure, *, remedy) -> bool
```

**Round-1 复审重裁（V1-C1 CONFIRMED）——码臂按 remedy 分表**：上面草绘的
单一共享码集不足。classifier 走私论证对 policy 三码
（POLICY_BLOCKED/PERMISSION_DENIED/TEMPLATE_NOT_ALLOWED）与对 OOM 完全同
构——state 带 `classifier: "unknown_failure"`（或大小写变体
`Policy_Blocked`，classifier 臂不 normalize）即可绕过双臂重获
`automatic_retry_allowed: True`（复审 end-to-end 实测）。但共享码集不能
直接扩：`changed_model_package` 的码臂扩进 policy 三码会违反 scenario 6
"零语义变化"验收线（refresh 通道对 TEMPLATE_NOT_ALLOWED 的合法认领正是
seam 9 第一路径）。**终形：码臂与 classifier 臂同构分表**——
`raw_input_reingestion → {OUT_OF_MEMORY, POLICY_BLOCKED,
PERMISSION_DENIED, TEMPLATE_NOT_ALLOWED}`、`changed_model_package →
{OUT_OF_MEMORY}`（#1161 逐字）。码臂 `.upper()` 比较即同时封死 casing
走私形（classifier 臂保持现比较不动——normalize 会在 refresh 通道引入
零变化线外的行为差）。测试矩阵补 policy 三码 × classifier-override 走私
anchor（raw 几何红、refresh+changed-package 仍绿）。

**判据按 classifier ∪ code 双臂拒**（round-2 NEW-1）：classifier 是
state 可控透传键（`_failure_policy_payload:107-109` 允许覆写 +
identity-filter 白名单 `:183/:598` 保留）——与表现面 3 的 `retryable`
同构的 latent 洗白面。state 带 `classifier: "unknown_failure"` +
`error_code: OUT_OF_MEMORY` 时仅 classifier 臂会放行 OOM；显式码臂
（`_REMEDY_NON_CAUSAL_CODES`）封死该角落。既有
`_model_package_refresh_retry_evidence` 的双臂写法（`:1373-1376`）正是
此防线的先例——其码臂**不得折叠**（初稿"如可折叠则折叠"撤销）。
task 0(e) 矩阵加 state-classifier-override 项。

**初稿错误**：曾按"输入修复类码 allow-list"放行——该集合生产零 writer
（D0），等于退役 raw-manifest 修复 remedy 且全部现有测试（用
INVALID_MANIFEST fixture）看不出来。**重裁**：这些几何的因果证据在几何
本身（manifest 实测缺失 `:1140` / 修复 download 晚于失败 job
`:1203`）——通道结构门已经证明"输入曾是问题"；判据只需拒收**分类上可证
明 remedy 无关**的永久码（deny-list by classifier）。实施时 implementer
须核对 `failure_classifier`（`retry.py:152-185`）对
OOM/TEMPLATE_NOT_ALLOWED/POLICY_BLOCKED/PERMISSION_DENIED 的实际映射，
classifier 覆盖不到的码（若有）补显式码项，并在测试矩阵钉住。

| 通道 | remedy 声明 | 对永久码的裁决 |
|---|---|---|
| `_missing_raw_manifest_repair_evidence`（:1123-1176） | `raw_input_reingestion` | 拒 `_REMEDY_NON_CAUSAL_CLASSIFIERS`（至少 OOM，AC-2）；其余永久码（含 unknown-default 如 `SLURM_JOB_FAILED`、`INVALID_MANIFEST` 类）**保持开放**——几何即因果证据，生产 remedy 不退役。causal 开放路径含 limit_exhausted 豁免在内字节级不变 |
| `_repaired_raw_manifest_downstream_retry_evidence`（:1178-1241） | 同上 | 同上（既有 `SLURM_JOB_FAILED` 测试 `:19299-19387` **保绿不动**——初稿曾误判需翻，重裁撤销） |
| `_downstream_failure_restartable`（:1061-1075，消费者 :234-275） | `same_downstream_rerun` | 见 D4：真实记录码且 permanent（含 unknown-default）或 limit_exhausted → 拒；合成占位码 → 维持现行为；瞬时记录码 → 维持 resume。黑名单整删（五码三 classifier 均被 permanence 蕴含） |
| `_model_package_refresh_retry_evidence`（:1360-1413） | `changed_model_package` | 行为逐字不变（本就 permanence 门控 + #1161 拒 resource_configuration/OOM 双臂）；拒绝名单迁至共享判据源，**classifier 臂与码臂均保留**（NEW-1），语义零变为验收线 |
| `_missing_forecast_output_recompute_evidence`（:289-338） | `remedy="exempt"`（调用点显式声明，round-1 P3） | 不接线：按码门控（显式含 OOM）是既有裁决——产物缺失重算 forecast 不是同配置重跑失败 stage。AC-1 的记录在案偏离（tasks task 9 明写） |

### D2b — pre-guard 通道全清单（round-1 P2-3，AC-1 的可指对象）

`scheduler_state_decision.py:77-372` 区间全部 pre-guard 出口及处置：

| 出口 | 处置 |
|---|---|
| `_completed_upstream_stage_retry_evidence`（:145，failure.py:1078-1120） | 豁免：`:1100-1101` 经 `_state_has_failure_signal` 自排除失败态，永不与 permanence 相交；测试矩阵加一条负向钉（失败态候选不走该通道） |
| `_manual_retry_state_evidence`（:269-274） | 豁免：手动重试路径，issue 显式 out-of-scope |
| `_missing_upstream_forecast_artifact_evidence` 三处 guard（:237-249/:277-291/:298-316） | 豁免：demotion guard（收紧方向），不产 retry |
| `_downstream_retry_evidence`（:276） | 接线（D4） |
| `_missing_forecast_output_recompute_evidence`（:293-297） | exempt（D2 表） |
| raw-manifest 双通道（:318-328） | 接线（D3） |
| `_model_package_refresh_retry_evidence`（:356-361） | 已门控，名单迁移（D2 表） |

## D3 — 通道 (a)/(b) 精确形状

结构门后、覆写前：

```python
failure = _failure_policy_payload(state)
if failure.get("permanent") and not _remedy_permits_permanent_failure(
        failure, remedy="raw_input_reingestion"):
    return None
```

拒收后**并非直落 guard**（round-1 P2-2 更正）：梯继续走
`_model_package_refresh_retry_evidence`（:356）——若候选同时有变更的
model package，该通道按 #1161 既有语义合法接手（changed-input remedy 对
unknown 码成立）；否则落 guard `:363-372`。seam 显式钉两条路径。

## D4 — 通道 (c)：按证据来源分域（round-1 P1-3 重裁）

**初稿错误**：一刀切 `not permanent` 会把全部真实生产下游码（D0 表）从
"自动续跑"翻成"人工"，并把 reader 合成占位码当 spec 证据（范畴错误）。
**重裁**：

```python
_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS = frozenset({
    "malformed_input", "policy_blocked", "resource_configuration",
})  # 旧黑名单 classifier 臂逐字保留（round-2 NEW-2）

def _downstream_failure_restartable(failure, *, code_recorded: bool):
    if failure.get("limit_exhausted"):
        return False
    if not code_recorded:
        # 合成占位码：spec :166-171 不适用，维持既有行为——含旧 classifier
        # 臂（state 显式覆写 classifier 为上三类时旧路径本就拒收；无此臂
        # 会在该 latent 角落放宽，违背"严格收紧"不变式）
        return str(failure.get("classifier") or "") not in (
            _DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS)
    return not failure.get("permanent")
```

（占位域旧行为核对：合成七码 `{CONVERT,FORCING,FORECAST,PARSE,
STATE_SAVE_QC,PUBLISH,COPYBACK}_FAILED` 无一命中旧码黑名单，码臂在占位
域本就空转；classifier 臂是占位域唯一有效拒绝面，逐字保留后 spec 场景 4
的 "exactly as before" 字面成立。）

`code_recorded` = state 携带真实 error_code（`_state_error_code:123-146`
扫描面：顶层/hydro_run/pipeline_jobs/events；消费者 `:246` 在合成默认前
可知）。域后果表（design 记录在案，运维可见）：

- 真实记录瞬时码（`SLURM_TIMEOUT`/`NODE_FAILURE`/…）：resume 不变。
- 真实记录永久码/unknown-default（`PARSE_FAILED` 记录形、
  `SLURM_JOB_FAILED`、`OUTPUT_INCOMPLETE`、`Q_DOWN_DISPLAY_NOT_READY`
  等）：拒 resume → 落梯尾（model-package 可能接手，否则 guard →
  人工）。这是 spec MUST NOT 的直接后果，**orchestrator 显式裁决接受**
  （round-2 NEW-4 更正：`map_slurm_error_code:144-152` 仅四路映射、其余
  兜底 `SLURM_JOB_FAILED` 且确定性混入瞬时故障——不存在"瞬时病必写瞬时
  码"的既有约定；丰富该映射/给 `SLURM_JOB_FAILED` 定分类路由 follow-up
  issue，task 9——已落地为 #1419）。
- 无记录码（合成 `{STAGE}_FAILED` 占位）：resume 不变（scope 裁决记录：
  该族不受 :166-171 约束；若未来要求收紧须另立 issue——tasks task 9 记
  录路由）。
- **Round-1 复审已知角落（V1-C2 CONFIRMED-DEFER，路由 follow-up）**：
  `code_recorded` 以全扫描面定义意味着"当前失败 job 无码 + state 残留历
  史码（已恢复早期 stage 的 error_code、auto-retry event 的
  `previous_error`）"的候选会被路由进记录域拒 resume（master 旧黑名单会
  resume）。该扫描面语义为本 design 自钦定（上段 + D4b #3/#4 清全扫描面
  指令），故按 fixture 层缺口路由 follow-up issue 裁决"code_recorded 应
  指失败 job 自身的码还是 state 任意处的码"，本 PR 不改——已落地为
  #1420。

### D4b — 九条现绿测试逐条重判（round-1 P1-2；tasks task 5 的完整清单）

| # | 测试（test_production_scheduler.py） | 裁决 |
|---|---|---|
| 1 | `:12333` `test_copyback_source_local_path_inside_allowed_roots_can_resume`（记录 PARSE_FAILED） | 钉现状。改断 `permanent_failure_guard`；allowed-roots 语义由**新增瞬时码平行 anchor 承重**（两个 root 几何各一条：OBJECT_STORE_ROOT 内 + NHMS_OBJECT_STORE_COPYBACK_ROOT）。**Round-1 复审更正（V3-C1 CONFIRMED）**：初版理由"该判定只在 planned_retry 存在时评估、拒收后根本不执行"事实错误——guard 在 blocked 路径经 `_missing_forcing_block()`（decision.py:352-360，传 `_failure_retry()` 非空 planned_retry）同样完整评估 allowed-roots 判定并产出 blocker；5 条既有 copyback 测试（`:9874`/`:12248`×2/`:12296`/`:12483`，PARSE_FAILED/PUBLISH_FAILED fixture）因此静默迁移到 guard 分支后仍绿，resume 路径的 guard 调用点（decision.py:277-289）失去全部钉子。**补救裁决**：新增一条瞬时码负向 anchor（NODE_FAILURE + copyback source 在 allowed roots 外 → `blocked/missing_copyback_source`），钉 resume 路径 guard；并修正 `:12367-12369` 测试注释中复制的同一错误理由 |
| 2 | `:12370` copyback_env_root 同族 | 同 #1 |
| 3 | `:17994` `..._parse_failure_after_shud_success_restarts_at_parse_without_native_rerun`（记录 FAILED_PARSE + 顶层 retryable True，D4+D5 联合命中） | 需求形命名但与 spec :166-171 冲突，spec 胜。改写为合成占位码形保留"restart at parse 不重跑 native"原主题——**清码须覆盖 `_state_error_code` 全扫描面**（顶层 + `pipeline_jobs`（`:18017` job_parse 带码）+ hydro_run/events，round-2 NEW-3），并去顶层 retryable |
| 4 | `:18058` `test_db_shaped_downstream_failure...[state_save_qc-Q_DOWN_DISPLAY_NOT_READY-unknown_failure]` | 需求形命名但直接违反 unknown-default MUST NOT，spec 胜。该参数化半边改断 guard；补合成占位码半边保留"无 retryable flag 也能 restart"原主题（同样清全扫描面：`:18078-18085` 参数化 job 带码） |
| 5 | `:3403` `test_raw_manifest_reuse_overrides_residual_restart_stage`（FAILED_PARSE 仅为 fixture） | 主题无关，fixture 码换瞬时码（NODE_FAILURE）保原主题 |
| 6-8 | `:18307`/`:18346`/`:18442` restart cohort / sibling-active 三条 | 同 #5：fixture 码换瞬时码 |
| 9 | `:19299` `test_repaired_raw_manifest_allows_stale_downstream_failure_retry`（SLURM_JOB_FAILED 经通道 (b)） | **保绿不动**（D2 重裁：unknown 码在 raw-manifest 几何保持开放）——初稿误判需翻，撤销 |
| 10 | `:4655` compat（INVALID_MANIFEST + 耗尽经通道 (a)） | 保绿不动（AC-2） |

除上述与 task 5 清单，其余既有测试零编辑。

## D5 — 表现面 3（round-1 维度 3 CLEAN，形状不变）

`_failure_policy_payload:110-112` 覆写加 `classification["retryable"]` 条
件；`permanent: True` 反向覆写（:113-115）逐字保留。**联锁警示**
（round-1 P2-1）：今天 D4/D5 两条洗白路径互为掩护（`:17994` 只开一面仍
绿），红证必须含"两面联合"组合 seam。

## D6 — 后果不变式

- 梯序不动；guard 仍按 return 点消谒（`:330-332` 注释补一句 pre-guard 通
  道经 shared refusal 咨询）。
- 瞬时记录码 / 预算内：全通道行为不变。
- 健康/运行中候选零新计算（判据在通道结构门之后）。
- 手动重试路径**决策不变**（`decision`/`reason`/`retry_policy` 硬编码不
  动）；精化（round-1 V1-C3）：manual evidence payload 里的
  `failure.retryable` 随 D5 收窄同步变化（非瞬时码 + 顶层
  `retryable: True` 的 state 由 True 变 False）——该字段无消费者读取，
  属记录在案的证据字段变化，不是路径行为变化。另：D5 新条件在
  `classify_failure` 的不变量（retryable ⟹ ¬permanent，retry.py:139-143）
  下是防御性恒真分支——保留它防的是未来分类器演化，非当前可达路径。
- raw-manifest 修复 remedy 在生产**不退役**（D2 重裁的核心约束）。

## D7 — seams under test

1. 通道 (a) OOM 拒收：OOM + raw-manifest 缺失几何 →
   `permanent_failure_guard`（红转绿）。
2. 通道 (a) 开放域保绿：INVALID_MANIFEST（compat `:4655` 既有）+
   `SLURM_JOB_FAILED`（新，unknown 码开放为 D2 重裁的判别 anchor）。
3. 通道 (b) 双向：OOM 拒收（新红转绿）/ `SLURM_JOB_FAILED` 保绿
   （`:19299` 既有）。
4. 通道 (c) 记录永久码拒收：记录 PARSE_FAILED → guard（D4b#1 改写）；
   参数化补 `OUTPUT_INCOMPLETE`（spec 显式列举）+ `SLURM_JOB_FAILED` +
   `Q_DOWN_DISPLAY_NOT_READY`（round-1 P2-6）。
5. 通道 (c) 原黑名单五码回归参数化（黑名单删除后由 permanence 蕴含）。
6. 通道 (c) 合成占位码域：state 无 error_code → resume 不变（D4 域裁决
   anchor）；瞬时记录码 resume 保绿（allowed-roots 双几何平行 anchor，
   D4b#1 承重件）。
7. 表现面 3 双向：OOM + 顶层 retryable True → guard（红转绿）；瞬时码 +
   顶层 retryable True → 行为同前。
8. **联合 seam**（P2-1）：记录 FAILED_PARSE + 顶层 retryable True →
   guard（仅两面同修才绿，判别 D4/D5 联锁）。
9. 拒收后梯尾两路径（P2-2）：非因果永久码 + 变更 model package → 该通道
   合法接手；无 package 变更 → guard。
10. row-4 recompute 逐字 anchor + model-package 双向回归（名单迁移零语义
    变化）+ `_completed_upstream_stage_retry_evidence` 失败态负向钉
    （D2b）。
11. 耗尽域：通道 (c) 耗尽拒；通道 (a) causal 开放域耗尽仍 repair
    （compat）。
12. 判据单元矩阵：`_remedy_permits_permanent_failure` 全 remedy × 代表
    classifier/码。

## D8 — 红证口径（round-1 P2-1 修正）

- 仅回退通道 (a)/(b) 判据 → seams 1/3(OOM 半边) 红，2/3(开放半边) 绿。
- 仅回退通道 (c) 记录码判据 → seam 4 红（判别红证：旧黑名单不拒
  PARSE_FAILED）。
- 仅回退 D5 → seam 7 红。
- **两面联锁**：seam 8 在仅回退 D4 或仅回退 D5 时都必须红（联合判别）。
- 每面独立回退 + 联锁组合，`git stash list` 空核验，输出留存。

## Evidence mapping

- terminal-state-semantics：seams 1-9 + 11。
- oracle-integrity：D4b 十行重判表（8 需改写 + 2 钉绿）逐条落 PR body +
  seams 双向红绿 + D8。
- spec-compliance：spec delta 六场景 ↔ seams 映射（场景 1↔seam 1、场景
  2↔seam 2、场景 3↔seam 4、场景 4↔seam 6、场景 5↔seam 7、场景 6↔seam
  10）+ OOM 条款显式例外句（round-2 P2-4）+ task 9 AC 对照。

## Non-goals

- DB 平面（#1161）、file-journal 平面（#1312）、`auto_retry_skipped`
  payload（#1314）、手动重试路径、row-4 recompute 码表内容重裁、合成占
  位码族的进一步收紧（记录路由，本 change 维持现行为）。
