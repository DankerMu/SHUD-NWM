# Proposal: retention-archive-gate-disabled-mode（#1369）

## Why

node-27 retention runner 的 enforce 路径硬性消费两份归档侧 receipt（completeness + drill，`scripts/node27_timeseries_retention.py:552-616,764-861`），而归档 lane 已按 ADR 0002 Revision 2026-08-11 永久退役（`/dev/md0` 双盘故障不重建，#1309/#1310/#1177/#1228 据此关闭）——两份 receipt 永远不再产出，retention 被永久锁死，DB（389 GB+，`/home` 1.7 TB 卷）无删除通道。

ADR revision 已显式修订核心不变式：**"no deletion without archive receipt" 不再成立**，并规定实现形态——"the retention runner keeps its fail-closed default and only skips the completeness/drill gates in an explicit gate-disabled mode whose receipt records the mode and cites this revision. Implementation is tracked in issue #1369."（`docs/adr/0002-node27-timeseries-hot-cold-tiering.md:304-320`）

## What Changes

1. **显式 gate 模式**：新增 env `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`（enum `enabled`|`disabled`，未设 = `enabled` = 现状 fail-closed；非法值 → `RETENTION_CONFIG_INVALID` exit 2）+ CLI `--archive-gate {enabled,disabled}`（CLI 优先，沿 `--enforce` 先例）。`disabled` 时跳过 completeness/drill 两 gate 的加载与判定（run_retention 相位 1a/1b/2b/1c/2c），并带两个**记录在案的语义后果**（completeness 的非 gate 消费点，design D2）：boundary-partial chunk 不再被 bounds-defer（会被删——无归档数据时"部分覆盖"概念本身消失）；enforced receipt 的 `salvage_backed_windows` 恒 `[]`（无归档背书的显式记录）。除此之外全部不变：锁、watermark 参考时间、保留窗、per-tick bound、dry-run/enforce、H3/H4/H5 drop 语义、receipt 原子写。
2. **Receipt 可审计**：schema `1.0`→`1.1`，所有 receipt（dry-run/refused/enforced 三分支）新增必填 `archive_gate` 对象——`mode` enum；`mode=disabled` 时必填 `adr_reference` const 引 ADR 0002 Revision 2026-08-11（ADR 要求逐字落实）。**零新增 wire code**（disabled 不是 refusal，是模式；四面字节同一性测试不动）；13 个归档族 code（`COMPLETENESS_*`×5 + `DRILL_*`×8）在 disabled 下不可达、runner 自有 3 个 refusal code 仍可达，双向钉死。
3. **配置连贯**：`disabled` 时两个归档 receipt 路径 env（`…_COMPLETENESS_RECEIPT_PATH`/`…_DRILL_RECEIPT_PATH`）转为可选不读取（归档 lane 已退役，强制指向永不存在的文件是伪配置）；`enabled` 时仍必填。
4. **文档修订**：runbook §7 头部退役 banner、§8 开篇双 receipt 硬门表述加 disabled 模式 carve-out（引 ADR revision）、§8.1 timer 启用步骤转正、§8.4 前置条件 OR 分支、§8.5 receipt 阅读补 `archive_gate` 字段、Live-state notes；模块 docstring `:50` "never bypassed" 句修订；`infra/env/node27-timeseries-retention.example` 新变量块（强警示）；receipts README schema 1.1 注记。
5. **node-27 落地（用户裁定 2026-08-14，AskUserQuestion）**：**启用 timer，每日 05:15 UTC（现 OnCalendar 不动，测试钉零改动）**。顺序：env 置 `ARCHIVE_GATE=disabled`（ENFORCE 暂 0）→ 手动 dry-run receipt → `ENFORCE=1` 手动 enforce receipt（≤5 chunk）→ `systemctl --user enable --now nhms-node27-timeseries-retention.timer` → `list-timers` 取证；证据全部入 PR 评论。

## Non-Goals

- 归档 lane 退役清理（mover/inventory-audit/drill 脚本与其测试的处置）——#1358 范围。
- 压缩策略、display carve-out 窗口——ADR revision 明文不变。
- timer OnCalendar 值变更——用户裁定沿用每日 05:15 UTC。
- 归档 gate 判定函数（`check_completeness_gate`/`check_drill_gate`）自身语义——enabled 模式下逐字节不变，既有 spec requirement 全部保留。
