# Design: archive-lane-retirement（#1370）

## 风险分级（fixture level: expanded）

- **风险轴**：生产删除通道的配置语义变更（D1 unset/enabled 行为翻转为 config-invalid）、大规模删除的漏删/误删（~10.4k 行脚本 + ~15.4k 行测试 + 5 schema）、runbook §8 交叉引用重写的文档连贯性（#1369 五轮硬顶的同一失败类）、node-27 实机 unit 退役操作。
- **Repair intensity**: high（删除型 change 的验证重点是"删干净 + 活面不伤"双向）。
- **Must-preserve**：retention `disabled` 模式行为逐字节不变（#1369 全部 disabled 测试零修改通过）；receipt schema 1.1 及 `archive_gate` 块不动；compression（§4）与 raw-retention lane 不受波及；timer OnCalendar 钉不动；`docker-compose.dev.yml` ghdc 挂载守卫注释保留。

## 关键决策

### D1 — retention enabled 模式退役形态（explorer 三选项裁定）

选 **(a/c)：gate loader 全删 + env 显式化**。`NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` 解析改为：strip+lower == `disabled` → 通过；unset、`enabled`、其余任何值 → `RETENTION_CONFIG_INVALID` exit 2 无 receipt，stderr 诊断含 `archive lane permanently retired (ADR 0002 Revision 2026-08-11)` 与 `set NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled`。理由：
- fail-closed 方向保持——unset 在旧世界最终 refuse（缺 receipt），新世界 config-invalid，都不删数据；绝不引入 unset→删除的 fail-open 翻转。
- `enabled` 从"可满足的模式"降为"已退役模式"，比静默移除 env 变量更可诊断（老部署/文档遗留 `enabled` 会得到明确指引而非行为漂移）。
- receipt schema 零改动（KISS）：schema `mode` 枚举仍含 `enabled`，但 runner 永不产出——schema 是历史 receipt 的校验器，历史 1.1 receipt 不重写。
- 弃 (b)（schema 留作测试夹具）：保留 57 个测试钉一条生产永不可达的路径，违背 issue "仓内死代码"目标。

### D2 — 退役删除的四个消费点核对（漏删防线）

- `load_completeness_receipt`/`load_drill_receipt`/`check_completeness_gate`/`check_drill_gate` 及 `_COMPLETENESS_SCHEMA_PATH`/`_DRILL_SCHEMA_PATH`/`_RECEIPT_FORMAT_CHECKER` 中仅归档用途的部分删除；`run_retention` 的 `gate_enabled` 分支坍缩（`archive_gate` 恒 `disabled`，短路语义照旧：`covered_eligible = eligible`、`salvage_backed_windows=[]`、boundary-partial 为 candidate）。
- **13 个归档族 wire code 从 `WIRE_CODES` 移除**：`COMPLETENESS_*`×5 + `DRILL_*`×8。#1369 的分区测试（13∪4==17）与 monkeypatch 不可达钉随 enabled 测试一并删除；存活断言改为 `WIRE_CODES` 恰为 runner 自有 4 code（`RETENTION_CONFIG_INVALID`/`RETENTION_CONCURRENT_INVOCATION`/drop-fail/uncaught 族按现名清点）且不含任何 `COMPLETENESS_`/`DRILL_` 前缀。**反向 walk（tests :433，扫 runbook + #855 pending design 双面）裁定：13 个退役 code 进 `_WIRE_CODE_ALLOWLIST` 带退役注释**——#855 语料冻结不改，walk 机制不阉割（fixture 复审 P1-2）。
- `RetentionConfig` 两路径字段 + 两 max-age 字段删除；`config_from_args` 对应解析删除；env example 对应段删除。
- 四面同源钉（code/runbook/design fixture/tests 的 wire-code 字节一致）在 §8.2 重写后重新对齐——#855 design fixture 是 pending change 语料不改，runbook 表为准。

### D3 — storage_inventory_audit 不可分离裁定

explorer 实证：`build_receipt`(:829-901)/`run_audit`(:990-1027) 归档腿与 hot 腿逐 subject 交织、无 CLI/env/函数边界，覆盖优先级四态混于单 schema 的 `windows[]`。**整体退役**；hot 侧库存审计需求若再现，以新 issue 全新设计（本 change 在 tasks 挂账一行报告项，不预建）。

### D4 — 治理 receipt 兼容性

`collect_archive_root` 删除后 governance receipt 不再有 `archive_root` 块（原 unset 时已是 `skipped` 三态之一，消费方——runbook 治理章、`current-production-ops.md`、agents 指令——同步删句）。receipt schema 若有 governance schema 钉 `archive_root` 必填则同步放开（实施期核实；explorer 未见独立 governance receipt schema，receipt 为自由 JSON + 测试钉）。

### D5 — runbook §8 重写边界

§8.2 wire-code 表只剩 4 runner 自有 code + "13 归档族 code 已随 enabled 模式退役（本 change）"一行历史注记；§8.4 BRANCH A（enabled 前置条件全文）删除、BRANCH B 转正为唯一路径（保留 ADR 逐字引锚——测试钉 `docs/adr/0002-...md` + `Revision 2026-08-11` 不动）；§8.5 `salvage_backed_windows`/boundary-partial 读法改为恒态描述；§8.7 空列表 caveat 简化。**#1369 的文档钉测试逐个核对**：ADR 锚、OnCalendar、`retention-2*.json` glob、锚定 grep、bare-token walk 均须存活（它们钉的是 §8.1/§8.4B/§8.5 存活文本）。

### Seams under test（上游声明，消费不重谈）

1. D1 解析表（unset/enabled/disabled/非法值 × env/CLI）——红证先行。
2. `WIRE_CODES` 收缩后的全集断言 + 前缀否定断言。
3. disabled 行为回归网：#1369 的 disabled-**行为**测试零修改通过（gate-switch/解析类中编码 enabled 可达性的 6 处钉按 tasks 1.4 枚举改写，非"零修改"范围）；`_completeness_receipt`/`_drill_receipt` helper 与 57 个 enabled 测试删除后文件仍 import 干净。
4. 治理：`DEFAULT_SERVICES` 快照测试更新后，对"不含归档 unit"做否定断言；#1310 三文件测试删除。
5. 删除完整性 grep 钉：`grep -rn "node27_product_archive\|archive_rebuild_drill\|db_export_salvage\|storage_inventory_audit" --include="*.py" scripts/ services/ packages/ workers/ apps/` 归零（tests/ 与 docs 允许历史注记）；`_WRAPPER_CASES` 收缩为 3 条存活 lane。

### D6 — rollback 语义随 D1 更新（fixture 复审 P2-2/P2-3）

D1 之后 "移除 `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` 行" 不再是回滚而是 config-invalid 常败态（unit 每 tick exit 2 无 receipt——正是本 issue 在退役的病理）。全部操作面的回滚文案统一为：**`NODE27_TIMESERIES_RETENTION_ENFORCE=0` 和/或 disable timer**。触达面：retention env example :57-59/:65、runbook §8.1 :3099-3101、runbook `## Rollback` 节 :3768-3796（该节同时清除两条归档 timer 命令与 §3 salvage 指针）。

### 风险包选择

- 选：**deletion-completeness**（双向 grep 钉 + import 面验证）、**config-semantics**（D1 行为翻转矩阵）、**doc-procedure-coherence**（#1369 同类，§8 重写 + 稳态 regime 连贯——收尾轮 self-audit 表方法直接继承）、**live-ops**（node-27 unit 退役 receipt）。
- 不选：schema-migration（receipt schema 零改动）、perf（纯删除）、frontend/API（不涉及）。

### Evidence mapping

- 删除完整性 → Verification grep 归零 + `uv run pytest -q` 全量绿。
- D1 语义 → 解析表测试 + env example/runbook 文本钉。
- 实机 → node-27 `list-timers` 无归档 unit + governance live receipt + retention timer 存活旁证，全部入 PR 评论。
