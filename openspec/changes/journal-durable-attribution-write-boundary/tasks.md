# Tasks: journal-durable-attribution-write-boundary

> 坐标为 base `66e1c5e5`（改动前）。实现中行号会漂移，**按函数名索引**。
> 目标文件除非特别说明均为 `services/orchestrator/file_orchestration_journal.py`。

## 1. 红先行 / 现状锁（先写先跑，记录 pre-change 红或现状绿）

- [ ] 1.1 J1/J2：`_journal_record_for_write` 直调腿。J1（`[object-uri]`/`[uri]` → `None`）
      **改动前必须红**；J2（`[local-path]`/`[redacted]` 原样保留）改动前应绿（现状锁）。
      两条写在**不同的**测试函数里——J2 是 must-preserve 4 的正向钉，不得与 J1 共用断言。
- [ ] 1.2 J3：旁路 A 端到端（`project_forecast_cohort_tasks`）。断言必须读 **durable jsonl
      载荷 + `pipeline-jobs/` direct row 文件**；只断言 `_public_scheduler_row` 返回值的用例
      **无判别力**（返回值本就会再洗一遍），不接受。改动前红。
- [ ] 1.3 J4：旁路 B 端到端（`_defer_forecast_cohort_projection_unlocked` →
      `_write_pipeline_job_unlocked`），同样穿透到 durable 层。改动前红。
      **注意 J4 单独是负判别力的**（durable 已有真值时它也判绿），必须与 1.3b 配对。
- [ ] 1.3b **J4b（fixture review P1 新增）**：defer 腿的 displacement。第 1 趟 defer 写入
      真实 `log_uri="s3://…"`（行落 `reconcile_unverified`，`:3539` 短路不拦第 2 趟）；
      第 2 趟携带 `log_uri="[object-uri]"` → durable 仍为 `s3://…`。改动前红。
- [ ] 1.4 J5（**本单核心**）：投影腿的 displacement——durable 行持有真实 `log_uri="s3://…"`
      且 master 为 `permanently_failed`；一趟携带 `log_uri="[object-uri]"` 的重投影到来 →
      durable 仍为 `s3://…`。改动前红（现状会被字面量顶掉）。
- [ ] 1.5 J6：`permanently_failed` master 重投影 → `error_code` **与** `error_message`
      均保持 existing，**两个字段各自独立断言**。改动前红。
      docstring 写明可达几何限定（design "D4 / #1589 的可达性更正"）。
- [ ] 1.6 J7（反向钉）：同一趟重投影携带**真值** `finished_at`/`exit_code`/`log_uri` →
      三者确实被刷新。改动前**绿**（现状锁，防 D4 被误实现成整行短路）。
- [ ] 1.7 J8（反向钉，**参数化三个派生终态** `succeeded`/`partially_failed`/`failed`）：
      重投影给出不同派生 `error_code` → 照常被覆写。改动前**绿**。
      参数化同时杀"扩成全 `TERMINAL_PIPELINE_STATUSES`"与"窄扩到含 `cancelled`"两类变异。
- [ ] 1.8 J9：粘性抑制掉唯一会变的字段时 `cohort_changed` 为 False、零写入
      （断言 journal 序列号不前进）。docstring 标注该几何为**单元构造、生产不可达**。
- [ ] 1.9 **J10（P2-1）**：手动重试 round-trip（`_record_manual_retry_submission_success`）后
      durable `log_uri` 为 `None` 而非字面量——把 D8 的翻转钉成有意行为。改动前红。
- [ ] 1.10 **J11（现状锁，两条既有用例）**：`tests/test_file_orchestration_migration.py:263`
      的子串存活、`tests/test_file_orchestration_journal.py:9068` 的 master frozen 大声拒绝，
      改动后必须仍绿。**若已被既有用例覆盖则直接引用，不重复造。**

## 2. #1592 实现（D1 / D2）

- [ ] 2.1 在 `_journal_record_for_write` 内加 `_strip_redaction_placeholders`，
      **置于现有 `_redact_durable_error_message_fields` 之前**——与
      `_append_validated_record_unlocked` 里 `:6280`→`:6281` 的现有顺序一致（must-preserve 6）。
- [ ] 2.2 `:6280` 的原调用点**保留不删**（D2）。若实现中发现保留会产生任何语义差，
      **停下来报告**，不得自行改裁定。
- [ ] 2.3 `_write_pipeline_job_unlocked` 返回值未 strip 的已知边界：就地加一行注释指明该值
      非 durable 态、二次回写会在写边界被兜住（D2 尾）。**不改行为。**
- [ ] 2.4 确认 `_sync_reconcile_inventory_for_row_unlocked` 仍只写五个身份字段——若实现期间
      发现它已携带其它字段，属充分性前提被推翻，**停下来报告**。

## 3. #1589 实现（D3 / D4 / D5）

- [ ] 3.1 D3（**两条腿，共 7 处**）：入参先过 `_strip_redaction_placeholders` 再判 `is not None`。
      - 投影腿 `project_forecast_cohort_tasks` `:3377-3384` 的 4 个条件字段
        （`finished_at`/`exit_code`/`master_error_message`/`log_uri`）
      - **defer 腿 `_defer_forecast_cohort_projection_unlocked` `:3563-3568` 的 3 个条件字段**
        （`finished_at`/`exit_code`/`log_uri`）—— fixture review P1，漏了它本单就在这条腿上
        交付 design 判定为"比现状更糟"的终值
      同腿内字段**一致处理**，不得只改 `log_uri`。两条腿的 `error_code`/`error_message` 是
      **无条件**写、没有谓词可修，不在本项射程（占位符由咽喉兜底）。
- [ ] 3.2 D4：粘性分支触发时（`sticky_master_status != projected_master_status`），
      `error_code`（`:3373` 无条件写）与 `error_message`（`:3381` 条件写）均保持 `existing` 值。
      两者形状不同，实现不可照抄同一段。
      实现须让 J6 的两个字段能被**分别**证伪（design 变异表倒数第三条）——
      即两个字段各自有独立断言，不共用一条。
- [ ] 3.3 D5：触发条件保持 `existing.status == "permanently_failed"` 字面比较，
      **不**改成 `in TERMINAL_PIPELINE_STATUSES`、**也不**扩到 `cancelled`。就地注释写明
      收紧后的合取判据："外部显式打上、投影无法自行派生"**且**"今天已受 status 粘性保护"
      （design D5；初版单条判据自相矛盾，见 fixture review P2-4）。
- [ ] 3.4 `cohort_changed` 的字段列表（`:3385-3399`）不动——粘性使字段不变时它应自然转 False
      （must-preserve 7）。不得为粘性加特判分支。
- [ ] 3.5 更新 `:3347-3353` 的既有注释：说明粘性现在覆盖归因族、观测族仍刷新，并指向 D4/D5。

## 4. 规格与文档

- [ ] 4.1 `openspec validate journal-durable-attribution-write-boundary --strict --no-interactive` 通过。
- [ ] 4.2 proposal 的 Non-Goals 与实现终态一致（尤其 D2 尾的已知边界、F-b 的边界）。

## 5. 验证与证据（档位见 design D6：本地闭环，不买 node-27，不碰 node-22）

- [ ] 5.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile.py
      tests/test_file_orchestration_journal_read_cache.py`
- [ ] 5.2 `uv run ruff check $(git ls-files '*.py')`
- [ ] 5.3 **变异证死**（design 表 **11 条**，逐条单独施加、跑完恢复）。恢复须
      **sha256 快照 + 拷回核验**；修复未提交时**禁止** `git checkout --`。
      每条记录「变异 → 转红的用例名 → 恢复后绿」。
- [ ] 5.4 重点核对**四条判别力关键**变异：D3 投影腿入参 strip 删除 → **J5** 红；
      D3 **defer 腿**入参 strip 删除 → **J4b** 红；粘性扩到全 TERMINAL → **J8** 三臂全红；
      观测族被粘住 → **J7** 红。**任一存活即判本单判别力不足**，须补 oracle 而非放行。
      另核"窄扩到 `{permanently_failed, cancelled}`"变异下 **J8 必须仍绿**（不得误伤既存缺口）。
- [ ] 5.5 **结构性 oracle 的取舍**：design 声明"新增写路径不能重新引入 bypass"无结构性 oracle。
      若 AST 守卫（断言无第二处构造含
      `{schema_version, sequence, record_type, source_id, cycle_time}` 的 record dict）
      实现起来平凡，则加；否则**记为已知边界并在 PR 里说明**——
      **不得造一个不真正约束该性质的假守卫。**
- [ ] 5.6 **`latest/` 落盘对照**（must-preserve 12 / 评审"验证缺口 2"）：改动前后各导出一次
      `latest/` 物化产物，差异必须全部可由"journal / direct row 已被 strip"解释，
      不得出现第三种差异。
- [ ] 5.7 全量兜底：`uv run pytest -q -m "not e2e and not grib and not integration"`
      —— 与合并前 master 基线比对，声明零新增红。

## 6. 偏离记录

- [ ] 6.1 每一处与本 tasks/design 的departure 写入 PR 的 `偏离记录` 段（what/why/impact）；
      无偏离须**显式写"无偏离"**。
