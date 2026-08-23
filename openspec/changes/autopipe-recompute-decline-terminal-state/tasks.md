# Tasks

## Evidence Floor

- **E1** `uv run pytest -q tests/test_node27_autopipeline_handoff.py` 全绿，含新增的
  declined-outcome 测试；既有的
  `test_declared_handoff_apply_exception_isolated_without_node22_db_fallback`
  （瞬态必须重试）保持绿且未被修改。
- **E2** `uv run pytest -q tests/test_river_identity_normalization_integration.py`
  在 node-27 真实 DB 上全绿，含新增的"新产物重开 decline"测试；既有的
  `test_already_ingested_recompute_detection_compares_product_mtime_to_parsed_at`
  与 `test_already_ingested_run_id_drift_costs_a_published_run_its_recompute_detection`
  保持绿。
- **E3** fail-closed 路径有测试：键不全（mtime 缺失 / init_state 缺失）与
  decline 写入抛异常两种情形均产出 `outcome="failed"` + `rc=1`。
- **E4** 迁移 `000055` 在 node-27 实机 apply 成功，`\d ops.ingest_recompute_decline`
  显示三列主键且 `product_mtime` 为 `double precision`。
- **E5** **live 双 tick 验收（本变更的核心证据）**。断言一律按**不变量**写，
  **不得钉任何绝对条数**——被挡集合在两个 tick 之间会增长（部署前实测已从
  88 涨到 116 = 60 `published` + 56 `succeeded`），写成全局的"零新增 decline 行"
  会在功能完全正常时误红。留档两次 tick 的 JSON 汇总与
  `SELECT count(*) FROM ops.ingest_recompute_decline`。
  - tick 1：`len(runs.declined_runs)` == 本 tick 被挡数 == 新增 decline 行数；
    至少一行的 `detail` 指名具体 chunk（如 `_hyper_1_52_chunk`），而不是复述
    reason code——这是可诊断性修复的 live 证据。
  - tick 2，**只针对 tick 1 已 decline 的那批 run_id**：零新 handoff 尝试、
    零新 decline 行、且 `published` 与 `succeeded` 两个总体**分别**核对都被抑制。
  - tick 2，全局：`declines_active` 等于**读取时刻**的表行数（它可能因新首见 run
    被 decline 而增长——这是正确行为且对 rc 中性）。
  - 只看 `rc==0` 不算通过（`rc` 会因 outcome 改名而变绿，即使环还在转）；
    且先查部署前那个 tick 有无**非被挡族**的失败，一个无关瞬态就会让 rc 保持 1。
- **E6** 计数器不被污染：测试钉死 declined run 不计入 `runs.ingested` /
  `runs.failed`，且不改变 `publish_eligible` 输入。
- **E7** decline 查询为批量，且 object store 读取只对有 decline 记录的 run 发生：
      测试钉死单次查询 + 读取次数与 decline 行数同阶（不与 pending 规模同阶）。
- **E10** `declines_active` 有自动化断言（不只靠 E5 的人工看 live 产物）：
      单测钉死每个 tick 汇总都带该字段且其值等于 `ops.ingest_recompute_decline`
      当前行数。这是防"静默持续"复发的那一条，必须自动化。
- **E8** `uv run ruff check .` 通过；`openspec validate autopipe-recompute-decline-terminal-state
  --strict --no-interactive` 通过，且 `tier-node27-timeseries-storage` 仍 strict 通过。
- **E9** runbook 清单可执行：其中的 SQL 在 node-27 上实际跑通并留档输出。

## Steps

- [x] T1 迁移 `db/migrations/000055_ops_ingest_recompute_decline.sql`：按 D1 建表
      （三列 NOT NULL 主键、`product_mtime DOUBLE PRECISION`、`IF NOT EXISTS`）。
- [x] T2 `_process_run`：forcing 失败分支识别 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
      reason code，写 decline（`ON CONFLICT DO NOTHING`）并返回 `outcome="declined"`；
      键不全或写入异常时保持 `outcome="failed"`（D2 fail-closed）。
- [x] T3 `_already_ingested_runs`：单次 `WHERE run_id = ANY(%s)` 取回 decline 行，
      只对这些 run 读 manifest/mtime，三分量全等者并入返回集（与 `retired` 并列）。
      **`_ingested_run_is_current` 不改动**（D2/D3）。
- [x] T4 （已取消）初版的 `_ingested_run_is_current` 上提 + 单点判定被 fixture 审查
      P1 推翻——它对停在 `status='succeeded'` 的那批 run 是死代码（fixture 审查时
      实测 28 个，部署前已增至 56 个）。见 design.md D2。
- [x] T5 `main`：汇总新增 `runs.declined`、`runs.declined_runs`、`declines_active`。
      **不要**给 `rc` / `publish_eligible` / `stats_guard` 加冗余排除条件——
      它们已按字符串精确匹配，新 outcome 天然不落入（D5）。
- [x] T6 单测：declined 分流、瞬态仍 rc=1、两条 fail-closed 路径（写入侧键不全、
      写入抛异常）、读取侧键不匹配不抑制、计数器不污染、`declines_active` 出现在
      汇总且等于表行数、object-store 读取次数与 decline 行数同阶
      （E1/E3/E6/E7/E10）。
- [x] T7 真实 DB 测试：decline 命中 → 不进 pending；产物 mtime 变新 → 重开（E2）。
- [x] T8 runbook：`docs/runbooks/tier-node27-timeseries-storage.md` §4.1 之后新增
      压缩前置检查清单（可执行 SQL）；§4.3 解压小节补一句部分解压会打开 parse
      侧守卫路径（D6/D7）。
- [x] T9 本地验证：ruff + 单测 + openspec validate（两个 change 各自 strict）。
- [ ] T10 node-27 实机：apply 迁移、跑双 tick、跑真实 DB pytest、跑 runbook 清单 SQL，
      全部留档（E2/E4/E5/E9）。
