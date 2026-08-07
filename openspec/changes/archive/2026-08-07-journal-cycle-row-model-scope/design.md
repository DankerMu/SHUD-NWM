# Design: journal-cycle-row-model-scope

## D1 seam 选择:candidate_state 投影,不动共享谓词、不动刀谱

三案:

- **(取) candidate_state 投影过滤**:在 `file_orchestration_journal.py` 的
  `candidate_state` 组装处——`candidate_state` 的 `pipeline_jobs=` 推导与
  `pipeline_events=` 推导——排除"他模型具名 + cycle-run_id"
  行及其 `pipeline_job` 事件;判定可复用
  `_is_model_less_cycle_scope_job`。
- (弃) 收窄共享谓词 `_job_matches_candidate`（issue 原推荐落点):fixture
  复审 P1-1 实测证伪——该谓词经 `_filter_cycle_rows_for_model`（由
  `_cycle_rows` 无条件调用）同时驱动 **4 个 gate surface**:

  | 调用点 | 功能 | DB 对照 | 收窄后果(复审实测) |
  |---|---|---|---|
  | `has_active_pipeline` | 去重闸 | `chain_repository.py:74-79` **有意保留**无条件 cycle-run 子句 | True→False:`active_duplicate_pipeline` 去重闸放松（`chain_forecast_trigger.py:136` 不再抛 PIPELINE_ALREADY_ACTIVE） |
  | `has_completed_pipeline` | 完成闸 | **无 job 谓词可对照**——DB 侧 `chain_repository.py:97-109` 只查 `hydro.hydro_run`（`model_id` 限定） | 同向放松;且该面 journal/DB 本就分歧（既有,非本 change 引入,见 D5 注） |
  | `active_slurm_jobs` | 在飞 slurm 扫描 | `chain_repository.py:177-181` 同上 | 非空→空 |
  | `_iter_direct_pipeline_job_records_for_cycle` | 直录 job 读面 | — | 随动 |

  即"与 DB 对齐"的口号在 gate surface 上**反向成立**:DB 的 gate 谓词就是
  宽的,收窄共享谓词制造新的 journal/DB 分歧,且复审确认无既有测试守护。
- (弃) 刀 1 扩到 `entity_type=pipeline_job`（issue 备选):只堵 retry 一个
  出口,他模型行仍污染 `latest_job`/`pipeline_status` 派生,候选态
  journal/DB 分歧永久化。

## D2 目标语义(候选态成员真值表;gate surface 不变)

| 行形 | candidate_state 现行为 | 新行为 | gate surface |
|---|---|---|---|
| `run_id == candidate_run_id`(任意 model) | 属于候选 | 不变 | 不变 |
| `model_id == 候选` (任意 run_id) | 属于候选 | 不变 | 不变 |
| model-less + `run_id == cycle_run_id` 或 `cycle_run_id_<suffix>` | 属于候选(cohort 契约) | 不变 | 不变 |
| **他模型具名 + `run_id == cycle_run_id`** | **属于候选(缺陷)** | **排除(行+事件同批)** | **不变(仍可见)** |
| 他模型具名 + `run_id == fcst_<...>_<他模型>` | 不属于(行/事件同丢,issue 实测) | 不变 | 不变 |

**事件面是显式过滤,不是自动收敛**(复审 P1-1b,must-preserve 级硬约束):
`_event_matches_candidate_rows` 按 job map 成员判定只在
"改共享谓词"seam 下自动随行;seam 落在投影层时,若只滤行不滤事件,落单
marker 的 entity `job_cycle_<source>_<stamp>_<stage>` 恰好命中
`scheduler_state_manual_retry.py:36` `_CYCLE_SCOPE_JOB_ID_RE`,经
`_unresolvable_marker_entity_pins_attempt` 的**可解析 cycle-scope 语法臂**
在 cycle 同、`failed_stage="forecast"` 时返回 True 照钉 5——缺陷经 #1287
交付的通道原样复活。因此投影处必须行/事件同一步排除,且 tasks 2.2b 用
"只滤行"的 mutant 证明测试对该形状红。

## D3 生产行形可达性(issue 已核,引用其定位)

他模型具名 + cycle run_id 行在生产可产出:单 basin pass 时
`_cycle_pipeline_job_model_id` 返回 basin model_id
（`chain_runtime_utils.py:65-68`）,而 basins 无共享 orchestration_run_id 时
`context.run_id` 回落 `cycle_<source>_<stamp>`（`chain_runtime_utils.py:71-83`）;
`accepted_submit_pipeline_job_model_id`（`accepted_submit_identity.py:317-328`）
只把 forecast cohort master 强制 model-less,download/forcing/convert/publish
保留 model_id。marker 由 `retry.py:515-532` 写 `entity_type="pipeline_job"`。
跨模型形状需同一 cycle 内两次不同单流域 pass——p2"待爆非在爆"定级成立
（db-free manual retry 尚缺运维入口 #1186）。

## D4 must-preserve 与 seams

- model-less cohort cycle-wide 可见性（#841/`22103181`,负向回归 2.3),含
  `cycle_run_id_<suffix>` 的 journal-only 加宽（`_job_matches_candidate` cycle-run 臂的 `startswith` 加宽;DB
  候选态谓词 `chain_repository_state.py:514` 是精确等值,该分歧既有且本
  change 不消除——delta scenario 如实记载,不虚称"全形状同判定"）。
- 候选自身行:own run_id 行、own model 具名 + cycle run_id 行(负向回归
  2.4;DB 侧经 `chain_repository_state.py:512` `model_id = %s` 同样成立);
  既有 `tests/test_file_orchestration_journal.py` 的 `_active_job` 夹具默认
  `model_id="model_a"`（候选==行 model),既有用例除
  `test_file_orchestration_journal_resource_limits_fail_closed` 里那条
  `model_b` latest 视图外全部取默认值,而该处经
  `query_pipeline_jobs_by_cycle` 读取、不经候选谓词——故排除逻辑对既有
  用例无影响,保持绿。本 change 新增的 `_cycle_run_id_job` /
  `_own_failed_forecast_job` 两个夹具以非默认 `model_id` 构造行并**故意**
  经候选投影,那正是判别对本身,不属"既有形保持绿"的论据范围。
- **4 个 gate surface 行为逐字不变**（负向回归 2.6):`has_active_pipeline` /
  `has_completed_pipeline` / `active_slurm_jobs` / 直录读面对他模型具名
  cycle-run 行的可见性保持现状。
- #1205 两把刀语义不动;#1287 arm-2 域与 floor 不动。
- Seam under test:candidate_state 投影(行+事件)+
  `_manual_retry_new_attempt`/`_manual_retry_payload` 消费端 + gate
  surface 三函数。

## D5 DB 口径的交付边界

DB 侧全部结论(候选态谓词 `:510-515`、gate 谓词 `chain_repository.py:74-79`/
`:177-181`)以 code-reading 证据交付:仓内
`tests/test_real_database_integration.py` 无 `PsycopgOrchestratorRepository.
candidate_state` 覆盖,本地/CI 不执行该 SQL(复审核实)。AC 5 的口径对齐
落在代码注释引用;不声称有自动化 DB 断言;若需实机以 node-27 real-DB 手动
receipt 另行补充,不作为本 change 的 evidence floor 项。

注（复审 escalation → **#1302**）:db-free `has_completed_pipeline` 对
"他模型具名 + cycle-run + succeeded + 完成 stage"行判 True(候选自身失败
态下仍 True),DB 侧只查 hydro_run 判 False——同根缺陷的第二出口,后果是
候选静默跳过。本 change **冻结**该行为(2.6(b) 负向回归断言现状),不修;
修复归 #1302,且 #1302 已把"整体收窄共享谓词"列为明确排除的反模式。
2.6(b) 的"行为不变"是冻结,不是背书正确性。

## D6 测试 fixture 入口纪律(复审 Note,防假红)

他模型行**不能**写进 `latest/gfs/<stamp>/model_b.json`——`_cycle_rows`
按候选自身 latest 文件取数,该形状根本不进 model_a 的行集,断言会空过。
必须写 `pipeline-jobs/<job_id>.json` 直录记录,且 `_journal_record` 信封
`model_id` 与 payload 一致(否则 `file_journal_model_mismatch` 拦读)。
判别对必须先在 pre-change 源上证红(2.5)。
