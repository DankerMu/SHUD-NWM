# Design: compression-per-tick-capacity-target（#1237）

## 风险分级（fixture level: compact）

- **风险轴**：doc-procedure-coherence（runbook §4 重写——#1369/#1338 同失败类）、live-ops（node-27 取证，但实机值零改动）、模板注释与测试钉的一致性。无 runner 代码改动、无 schema 改动、无行为面。
- **Repair intensity**: low-medium。
- **Must-preserve**：tests :1570-1590 `test_compression_env_example_documents_the_budget_chain` 钉的三条 timeout-budget 默认值与 catch-up hint `PER_TICK_BOUND=1` 注释行字节不动；timer `OnCalendar=*-*-* 04:25:00 UTC` 钉不动；`per_tick_bound` 保持强制 env（不加代码默认——mandatory 是有意设计，重建时宁可 fail-closed 报 config error 也不静默取数）。

## 关键决策

### D1 — 目标值 = 4（容量推导）

输入（全部 2026-08-14 node-27 read-only 实测，查询输出入 PR 证据）：

1. **到达率**：`timescaledb_information.dimensions` 两热表 chunk 区间均 7 天且边界对齐（census：river 5 chunk / forcing 6 chunk，range 边界同为周三 00:00 UTC 系）→ 稳态每周同一天到期 **2 个终态 chunk**（2026-08-14T04:25 tick receipt 实证：恰好压了这一对）。
2. **backlog 上界（条件性，非"物理"）**：retention 窗口约束每表未压 chunk 存量 ≤3 → 全库可积压 ≤6。**前提三条显式记**：(a) 窗口 21 d 取自 **live** env（committed 模板 `infra/env/node27-timeseries-retention.example` 写 `=14`，漂移已记 runbook :38-40，本 issue 不处理不判定）；(b) retention timer **2026-08-14 才启用**（#1369），census 仍高于 21d/7d 稳态应有的 ~4/表，收敛尚未观测到；(c) timer 停用或窗口调整时该上界消失，本推导须按公式重推。
3. **时间预算（双约束的墙侧）**：#1156 预算链 live 无 override → 代码默认生效：`WRAPPER_WALL_SECONDS=3900`（`scripts/node27_timeseries_compression.py:115-116`），且这是**整 tick 墙不是单 chunk 墙**（`:102-105` 注释、spec :707 末句、runbook §4.5 表格三处同源），runner 循环内无 elapsed 守卫，超墙 = wrapper `timeout` TERM 打断 DDL。按 runbook §4.5 估时配方（6.0 s/GB + ~300 s 开销）复核实测：本周对 chunk（230GB+12.7GB）估 1836 s ≈ 实测 30m36s ✓；稳态 river chunk 268-409 GB 单个 1608-2454 s → **3 river + 1 forcing ≈ 5280-7818 s，远超 3900 s 墙**。
4. **live 现值即 4** → 实机零改动，"统一"只发生在模板侧（5→4）。
5. **结构性事实（AC-5 主论据）**：chunk 按时间维切（7 d），**终态 chunk 个数对 ingest 体量不敏感**——ingest 翻倍只让 chunk 变大不变多，增长压力全部落在 per-chunk timeout 预算（#1156 的领域），不落在 per-tick bound 上。

**容量关系是双约束**，runbook §4 重写必须两条都写并显式交叉引用 §4.5：

- 吞吐约束：`bound × 1 tick/day ≥ 稳态到达 2 chunk/week`（bound=4 → 14× 余量）。
- 墙约束：`Σ(选中 chunk GB × 6.0 s) + ~300 s ≤ WRAPPER_WALL_SECONDS(3900)` —— river 尺寸下**单 tick 实际可兑现 ≤ ~2 chunk**。

**结论改写**：bound=4 是吞吐余量上限，**不是单 tick 可兑现容量**；稳态周对 chunk（1 river + 1 forcing ≈ 1836 s）在墙内一 tick 完成；灾后追赶不依赖 bound 值本身，按 §4.5 配方执行（bound=1 + 抬墙 override），6-chunk 最大 backlog 的真实收敛 ≥3 tick 且须走 §4.5，不写"2 tick 收敛"。

选 4 弃 5（诚实版）：backlog 上界 ≤6 内 `ceil(6/5) == ceil(6/4)`，收敛 tick 数相同（且按墙约束两者实际都不由 bound 决定）；第 5 格的边际收益 ≈ 一个 forcing chunk（12.7 GB ≈ 76 s 压缩量），低于改动 live 值一次引入的运维风险；live 现值即 4，选 4 = 实机零改动。弃"提频代替提 bound"：输入 5 的结构性事实（chunk 个数与 ingest 解耦）+ 14× 吞吐余量，提频无收益——此结论显式写入 runbook §4（AC-5）。

**失效条件清单**（写入 runbook §4 尾部）：`chunk_time_interval` 变更；新增第三张热表；retention timer 停用或窗口调整（输入 2 前提）；wrapper wall override 改变墙约束；chunk 边界去对齐（该方向使单 tick 负载**下降**，属安全方向，记明防误判）。

### D2 — 模板值加严格钉（承 #1370 C-A 判例）

现状：模板 :18 的数值**零测试钉**（explorer 全文件核实；:1590 钉的是注释块里的 catch-up hint `=1`，不是赋值行）。本 change 新增一条 MULTILINE 断言 `^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4$`（加入现有 `test_compression_env_example_documents_the_budget_chain` 或紧邻新测试，实施者按文件风格取舍并报告）。红证义务：先临时改模板值证断言咬合再复原。

### D3 — live 证据形态（不覆盖 scheduled receipt；程序按复审 P2-4 修正）

live receipt 路径是单文件覆盖式（`.nhms-issue1069-live/scheduled-receipt.json`），且 env 由 **wrapper** source（`set -a; . ENV_FILE` 会把环境预设的 RECEIPT_PATH 覆写回生产路径）——不 source 则强制 env 缺失 fail-closed 在写 receipt 之前。取证程序显式三步：

1. `set -a; . /home/nwm/NWM/infra/env/node27-timeseries-compression.env; set +a`（拿到全部强制 env）；
2. `.venv/bin/python scripts/node27_timeseries_compression.py --receipt-path <scratch>`——用 **CLI flag** 压过 env（优先级 `:255`），不受 source 覆写影响；
3. **不加 `--enforce`**（dry-run）；lock 沿用生产 `LOCK_PATH`（与 04:25 timer 互斥是期望行为，不 scratch 化）。

判据拆两句（dry-run 的 `outcome` 是硬编码 `clean`（`:809-811`），恒真项不入判据）：(a) scratch receipt（任意 mode）echo 的 `per_tick_bound == 4 == 部署值`；(b) **clean 语义由 2026-08-14T04:55:36Z enforce receipt 承载**（per_tick_bound=4、本周对 chunk 双 committed、outcome=clean），双证一起入 PR。凭据纪律照旧：receipt 入证前 grep 核无 DSN/密码。

### Seams under test

1. D2 模板值钉（红证先行）。
2. 既有 :1570-1590 budget-chain 钉零修改通过（must-preserve 回归网）。
3. runbook §4 重写后 #1156 区段（§4.5 大 chunk 追赶）与 :1590 hint 的交叉引用仍连贯。

### 风险包选择

- 选：**doc-procedure-coherence**（§4 重写 + 稳态 regime 表述——occurrence-audit 自查表方法继承，tasks 3.x）、**live-ops**（node-27 取证，read-only + scratch 写）。
- 不选：deletion-completeness（无删除）、config-semantics 矩阵（无解析行为变更）、schema-migration/perf/frontend（不涉及）。

### Evidence mapping

- D1 推导 → SQL census/receipt/日志三类原始输出贴 PR（AC-1）。
- 模板统一 → diff + D2 钉测试绿（AC-2）。
- live 一致 → 预检 grep `=4` + scratch dry-run receipt + 08-14 enforce receipt（AC-3）。
- runbook → §4 重写段 + 无需提频显式结论（AC-4/AC-5）。
- `uv run ruff check .`（AC-6：本 change 触碰 tests/*.py，非"仅模板+文档"声明路径）。
