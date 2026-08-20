# Tasks: journal-durable-attribution-write-boundary

> 坐标为 base `66e1c5e5`（改动前）。实现中行号会漂移，**按函数名索引**。
> 目标文件除非特别说明均为 `services/orchestrator/file_orchestration_journal.py`。

## 1. 红先行 / 现状锁（先写先跑，记录 pre-change 红或现状绿）

- [x] 1.1 J1/J2：`_journal_record_for_write` 直调腿。J1（`[object-uri]`/`[uri]` → `None`）
      **改动前必须红**；J2（`[local-path]`/`[redacted]` 原样保留）改动前应绿（现状锁）。
      两条写在**不同的**测试函数里——J2 是 must-preserve 4 的正向钉，不得与 J1 共用断言。
- [x] 1.2 J3：旁路 A 端到端（`project_forecast_cohort_tasks`）。断言必须读 **durable jsonl
      载荷 + `pipeline-jobs/` direct row 文件**；只断言 `_public_scheduler_row` 返回值的用例
      **无判别力**（返回值本就会再洗一遍），不接受。改动前红。
- [x] 1.3 J4：旁路 B 端到端（`_defer_forecast_cohort_projection_unlocked` →
      `_write_pipeline_job_unlocked`），同样穿透到 durable 层。改动前红。
      **注意 J4 单独是负判别力的**（durable 已有真值时它也判绿），必须与 1.3b 配对。
- [x] 1.3b **J4b（fixture review P1 新增）**：defer 腿的 displacement。第 1 趟 defer 写入
      真实 `log_uri="s3://…"`（行落 `reconcile_unverified`，`:3539` 短路不拦第 2 趟）；
      第 2 趟携带 `log_uri="[object-uri]"` → durable 仍为 `s3://…`。改动前红。
- [x] 1.4 J5（**本单核心**）：投影腿的 displacement——durable 行持有真实 `log_uri="s3://…"`
      且 master 为 `permanently_failed`；一趟携带 `log_uri="[object-uri]"` 的重投影到来 →
      durable 仍为 `s3://…`。改动前红（现状会被字面量顶掉）。
- [x] 1.5 J6：`permanently_failed` master 重投影 → `error_code` **与** `error_message`
      均保持 existing，**两个字段各自独立断言**。改动前红。
      docstring 写明可达几何限定（design "D4 / #1589 的可达性更正"）。
- [x] 1.6 J7（反向钉）：同一趟重投影携带**真值** `finished_at`/`exit_code`/`log_uri` →
      三者确实被刷新。改动前**绿**（现状锁，防 D4 被误实现成整行短路）。
- [x] 1.7 J8（反向钉，**参数化三个派生终态** `succeeded`/`partially_failed`/`failed`）：
      重投影给出不同派生 `error_code` → 照常被覆写。改动前**绿**。
      参数化杀"扩成全 `TERMINAL_PIPELINE_STATUSES`"这类变异（三臂全红）。
      **它抓不到"窄扩到含 `cancelled`"**——三臂里没有 `cancelled`，该变异下 J8 必然全绿。
      那条变异行是**防误伤守卫**（见 5.4 末句），不是杀伤测试。
- [x] 1.8 J9：粘性抑制掉唯一会变的字段时 `cohort_changed` 为 False、零写入
      （断言 journal 序列号不前进）。docstring 标注该几何为**单元构造、生产不可达**。
- [x] 1.9 **J10（P2-1）**：手动重试 round-trip（`_record_manual_retry_submission_success`）后
      durable `log_uri` 为 `None` 而非字面量——把 D8 的翻转钉成有意行为。改动前红。
      **docstring 须标注"单元构造"**（同 J9）：`_create_pending_manual_retry_job:7410` 把
      `log_uri` 置 `None`，完整 `attempt_manual_retry` 流程够不到该点（D8 可达性更正）。
- [x] 1.10 **J11（现状锁，两条既有用例）**：`tests/test_file_orchestration_migration.py:263`
      的子串存活、`tests/test_file_orchestration_journal.py:9068` 的 master frozen 大声拒绝，
      改动后必须仍绿。**若已被既有用例覆盖则直接引用，不重复造。**

## 2. #1592 实现（D1 / D2）

- [x] 2.1 在 `_journal_record_for_write` 内加 `_strip_redaction_placeholders`，
      **置于现有 `_redact_durable_error_message_fields` 之前**——与
      `_append_validated_record_unlocked` 里 `:6280`→`:6281` 的现有顺序一致（must-preserve 6）。
- [x] 2.1b **event lane carve-out（D2b，实现期裁定）**：咽喉 strip 跳过 `pipeline_event`。
      就地注释必须写**原则**（"strip 清除的是从调用方来的占位符，绝不跑在 journal 自己的公共
      渲染下游"），**不得**只写"跳过 event"——后者会诱使下一个人加第二个渲染器时再次踩坏。
- [x] 2.1c **J12（D2b 的 oracle）**：`tests/test_file_orchestration_migration.py:1637`
      （`test_historical_pipeline_event_runtime_roots_are_redacted_but_retry_recoverable`）
      改动后必须**仍绿**——它就是 D2b 的现状锁。变异表新增一行：删掉 carve-out → J12 红。
- [x] 2.2 `:6280` 的原调用点**保留不删**（D2）。它是 event lane **唯一**的反洗白层（D2b），
      删它等于让 event lane 裸奔。若实现中发现保留会产生任何语义差，
      **停下来报告**，不得自行改裁定。
- [x] 2.3 `_write_pipeline_job_unlocked` 返回值未 strip 的已知边界：就地加一行注释指明该值
      非 durable 态、二次回写会在写边界被兜住（D2 尾）。**不改行为。**
- [x] 2.4 确认 `_sync_reconcile_inventory_for_row_unlocked` 仍只写五个身份字段——若实现期间
      发现它已携带其它字段，属充分性前提被推翻，**停下来报告**。

## 3. #1589 实现（D3 / D4 / D5）

- [x] 3.1 D3（**两条腿，共 7 处**）：入参先过 `_strip_redaction_placeholders` 再判 `is not None`。
      - 投影腿 `project_forecast_cohort_tasks` `:3377-3384` 的 4 个条件字段
        （`finished_at`/`exit_code`/`master_error_message`/`log_uri`）
      - **defer 腿 `_defer_forecast_cohort_projection_unlocked` `:3563-3568` 的 3 个条件字段**
        （`finished_at`/`exit_code`/`log_uri`）—— fixture review P1，漏了它本单就在这条腿上
        交付 design 判定为"比现状更糟"的终值
      同腿内字段**一致处理**，不得只改 `log_uri`。
      **两腿的无条件写形状不对称，不可照抄**（第 2 轮复核 P2）：不在本项射程的只有
      投影腿的 `error_code`（`:3373`）与 defer 腿的 `error_code`+`error_message`
      （`:3558-3559`）——它们是无条件写、没有谓词可修，占位符由咽喉兜底。
      **投影腿的 `error_message` 是条件写（`:3381`），在本项的 7 处之内。**
- [x] 3.2 D4：粘性分支触发时（`sticky_master_status != projected_master_status`），
      `error_code`（`:3373` 无条件写）与 `error_message`（`:3381` 条件写）均保持 `existing` 值。
      两者形状不同，实现不可照抄同一段。
      实现须让 J6 的两个字段能被**分别**证伪（design 变异表倒数第三条）——
      即两个字段各自有独立断言，不共用一条。
- [x] 3.3 D5：触发条件保持 `existing.status == "permanently_failed"` 字面比较，
      **不**改成 `in TERMINAL_PIPELINE_STATUSES`、**也不**扩到 `cancelled`。就地注释写明
      收紧后的合取判据："外部显式打上、投影无法自行派生"**且**"今天已受 status 粘性保护"
      （design D5；初版单条判据自相矛盾，见 fixture review P2-4）。
- [x] 3.4 `cohort_changed` 的字段列表（`:3385-3399`）不动——粘性使字段不变时它应自然转 False
      （must-preserve 7）。不得为粘性加特判分支。
- [x] 3.5 更新 `:3347-3353` 的既有注释：说明粘性现在覆盖归因族、观测族仍刷新，并指向 D4/D5。

## 4. 规格与文档

- [x] 4.1 `openspec validate journal-durable-attribution-write-boundary --strict --no-interactive` 通过。
- [x] 4.2 proposal 的 Non-Goals 与实现终态一致（尤其 D2 尾的已知边界、F-b 的边界）。
- [x] 4.3 **spec delta 与代码同步**（round-2 评审 P2；见 design D10）：`openspec validate --strict`
      只查结构不查内容，三处分叉只有人能查——(a)「stripped placeholder SHALL be persisted as
      `None`」漏掉第三种终值（解析成 durable 真值）；(b) withheld-means-keep 被限定在
      「every **cohort** write path」，而 8 条修好的腿里 6 条不是 cohort；(c) round-2 真正交付的
      性质（compared == persisted / 重放收敛 / 取消回执不被丢）**一条 requirement 都没有**。
      归档一份与代码不符的 spec 比不归档更糟，故本条不可 defer。
- [x] 4.4 **类守卫完备性断言**（orchestrator 自查）：`_LOG_URI_WRITE_LEGS` 是硬编码 6-tuple，
      而签名内省显示实际有 7 个公共方法接受 `log_uri`。补内省断言（`log_uri` 参数集合 ==
      表内腿名 ∪ 显式排除表），并证明它会咬。

## 5. 验证与证据（档位见 design D6：本地闭环，不买 node-27，不碰 node-22）

- [x] 5.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile.py
      tests/test_file_orchestration_journal_read_cache.py`
- [x] 5.2 `uv run ruff check $(git ls-files '*.py')`
- [x] 5.3 **变异证死**（design 表 **12 条**（D2b 后新增 M12），逐条单独施加、跑完恢复）。恢复须
      **sha256 快照 + 拷回核验**；修复未提交时**禁止** `git checkout --`。
      每条记录「变异 → 转红的用例名 → 恢复后绿」。
- [x] 5.4 重点核对**四条判别力关键**变异：D3 投影腿入参 strip 删除 → **J5** 红；
      D3 **defer 腿**入参 strip 删除 → **J4b** 红；粘性扩到全 TERMINAL → **J8** 三臂全红；
      观测族被粘住 → **J7** 红。**任一存活即判本单判别力不足**，须补 oracle 而非放行。
      另核"窄扩到 `{permanently_failed, cancelled}`"变异下 **J8 必须仍绿**（不得误伤既存缺口）。
- [x] 5.5 **结构性 oracle 的取舍**：design 声明"新增写路径不能重新引入 bypass"无结构性 oracle。
      若 AST 守卫（断言无第二处构造含
      `{schema_version, sequence, record_type, source_id, cycle_time}` 的 record dict）
      实现起来平凡，则加；否则**记为已知边界并在 PR 里说明**——
      **不得造一个不真正约束该性质的假守卫。**
- [x] 5.6 **`latest/` 落盘对照**（must-preserve 12 / 评审"验证缺口 2"）：改动前后各导出一次
      `latest/` 物化产物，差异必须全部可由"journal / direct row 已被 strip"解释，
      不得出现第三种差异。
- [x] 5.7 全量兜底：`uv run pytest -q -m "not e2e and not grib and not integration"`
      —— 与合并前 master 基线比对，声明零新增红。

## 6. 偏离记录

- [ ] 6.1 每一处与本 tasks/design 的departure 写入 PR 的 `偏离记录` 段（what/why/impact）；
      无偏离须**显式写"无偏离"**。

## 7. Round-2 修复轮（D3 射程被证伪后的补齐；裁定见 design D9）

- [x] 7.1 **全类普查**：枚举全部 16 个 `_write_pipeline_job_unlocked` 调用方 + 3 条不经它的
      durable 写腿 + hydro_run 侧，逐腿记录「收不收调用方证据 / 有无谓词 / 有无无条件写 /
      有无相等闸门 / 裁定」。表见 design D9.2。**普查结果无论是否要改都必须留表。**
- [x] 7.2 唯一具名裁决点 `_resolved_caller_evidence(value, *, durable=None)`，
      「占位符 = withheld，withheld 意味着保留」只写一遍。
- [x] 7.3 解析**跑在覆写谓词与相等闸门两者之前**（round-1 只做到前者，是本轮缺陷的成因）。
- [x] 7.4 无条件写的腿传 `durable=`，且**比较器与落盘共用同一个表达式**
      （`complete_pipeline_job_cancellation` 的 `desired` 与 `row.update`）。
- [x] 7.5 `update_pipeline_job_status` 的单独裁定已写明（只解析、不加 `durable=`、不加闸门）。
- [x] 7.6 每条被修的腿各有 idempotency-replay + displacement 一对用例（J13-J19），
      并加一条参数化的**行为**类守卫（J20，六条腿）。
- [x] 7.7 三条次要发现各有裁定并落档：`init_state_uri: null`（声明不修，实测双证）、
      `_journal_record_for_write` 注释过诺（已改，纯注释）、AST 守卫按名索引（已收紧）。
- [x] 7.8 D8/J10 的翻案已在 design D8 尾显式记录为**取代**，不是静默改写。
