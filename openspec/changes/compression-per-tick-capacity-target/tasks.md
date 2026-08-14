# Tasks: compression-per-tick-capacity-target（#1237）

## 1. 模板与测试钉

- [ ] 1.1 `infra/env/node27-timeseries-compression.example:18` `5`→`4`；行上注释改写为容量结论表述（指 runbook §4 推导），删除"任意默认"语气；:70-79 catch-up hint 块与三条 timeout-budget 默认值字节不动
- [ ] 1.2 D2 严格钉：`^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4$` MULTILINE 断言落 `tests/test_node27_timeseries_compression.py`（并入 :1570-1590 既有 env-example 测试或紧邻新测试，按文件风格）；红证：临时改模板值证咬合后复原
- [ ] 1.3 既有 budget-chain 钉（:1570-1590）与全部压缩测试零修改通过

## 2. runbook §4 重写（doc-procedure-coherence 风险包）

- [ ] 2.1 §4 "Per-tick capacity (live state 2026-08-01)" (:279-303) 重写：新 live-state 日期（2026-08-14）、**双约束容量公式（吞吐 + wrapper 墙，缺一不可）**、D1 五项实测输入、定值 4 理由（"吞吐余量上限，非单 tick 可兑现容量"）、**显式交叉引用 §4.5 追赶配方**（bound=1 + 抬墙——灾后追赶不依赖 bound 值）、失效条件清单、**"无需提频"显式结论**（主论据 = chunk 个数与 ingest 体量解耦，辅证 14× 余量）；"which number is right is tracked in issue #1237" 改为已决记录（保留 #1237 作为决策出处引用）
- [ ] 2.2 :249-250 Live-state notes 前向指针日期/措辞同步；LAG_SECONDS 实机演化（604800→172800，2026-08-14 观察）仅登记为"观察值、非目标值、out of scope"一句，不判定
- [ ] 2.3 occurrence-audit 自查（继承方法）：对自己新增/改动行 grep 核——无"tracked in #1156"类错误指针、无与 §4.5/:1590 catch-up hint 矛盾表述、无把 lag/RECEIPT_PATH 漂移误写为已统一

## 3. Evidence Floor

- [ ] 3.1 D1 容量推导原始数据入 PR（AC-1）：chunk census（dimensions/chunks/compression_stats 查询输出）、2026-08-14 enforce receipt 摘要、2026-07-26 人工压缩时长日志行；**注明 AC-1 字面的"近 30 天每日新增 chunk 数"由 chunk-width 结构性论证（7 d 时间维切分 → 到达率恒 2/周）有意替代，属更强证据而非缺证**
- [ ] 3.2 `uv run pytest -q tests/test_node27_timeseries_compression.py` + `uv run ruff check .` 绿（AC-6）
- [ ] 3.3 `openspec validate compression-per-tick-capacity-target --strict --no-interactive` 过
- [ ] 3.4 node-27 实机（merge 前）：预检 `grep -n '^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=' env` == 4（AC-3 前提，实机零改动）→ **D3 修正版三步取证**（source env 后用 CLI `--receipt-path <scratch>` 压过 env、不加 `--enforce`、lock 沿用生产路径）→ scratch receipt 证 `per_tick_bound==4`，clean 语义由 2026-08-14 enforce receipt 承载（dry-run outcome 恒 clean 不入判据）→ 凭据 grep 核后双证入 PR 评论
- [ ] 3.5 CI 定向测试绿

## 偏离与范围外挂账

- issue 兄弟登记表更新：唯一存活兄弟为 retention per-tick bound；product_archive/db_export_salvage 已随 #1370 退役。
- issue 两处陈旧前提（explorer 核实）：runbook 指针早已是 #1237（非 #1156）；PR #1236 已合并——fixture 按现状执行，不按 issue 原文。
- LAG_SECONDS / RECEIPT_PATH 实机漂移：登记不处理（Non-Goals）；LAG 604800→172800 的**行为面后果**（7 d chunk 配 2 d lag 使回填窗口实质压缩 5 天，write guard 拒绝与已压缩 range 重叠的写入）超出登记价值，交 issue-scribe 独立立案（复审越界升级 2）。
- **DOC_STATUS 分歧登记（复审 P2-5）**：active change `openspec/changes/tier-node27-timeseries-storage/design.md:546` 写 `PER_TICK_BOUND (default 5)`，模板改 4 后该行陈旧——照 #1352 判例不改写他变更设计文档，在此登记分歧；该 change 归档时随其处理。
- **capability spec 超时默认值失配**（复审越界升级 1，pre-existing）：`openspec/specs/hypertable-compression/spec.md:707,712` 仍写 840000/900/940，代码已是 3600000/3900/3940（#1352 改，从未落 OpenSpec 制品）——交 issue-scribe 独立立案，本 change 的 spec delta 不顺手改（避免混入他人 requirement 语义）。
