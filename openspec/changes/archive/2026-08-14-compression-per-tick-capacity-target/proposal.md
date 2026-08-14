# Proposal: compression-per-tick-capacity-target（#1237）

## Why

压缩 per-tick bound 是"压缩车道能否追上 ingest"的唯一节流阀（bound × tick 频率 = 每日最大压缩 chunk 数），但它没有被判定过的目标值：committed 模板 `infra/env/node27-timeseries-compression.example:18` 写 `=5`（自 #851 落地未改），node-27 生产 env 跑 `=4`（7 月追赶期手工 retune，gitignored），双向漂移。env 重建会静默回到 5，按模板做容量推算的人会算错。2026-07-25/26 `/home` 打满事故的两个输入之一正是"未压缩 chunk 热层堆积"。runbook §4 "Per-tick capacity (live state 2026-08-01)" 已如实记录漂移并指向本 issue——现在把值定下来。

## What Changes

1. **定值 4（容量结论，非追认实机手工值）**——推导见 design D1（双约束：吞吐 + wrapper 整 tick 墙 3900s），全部输入为 2026-08-14 node-27 实测：稳态到达 2 终态 chunk/周（两热表 7 天 chunk 区间且边界对齐，同日到期，单 tick 1836s 墙内完成）；retention 窗口（live 21 天，条件性前提见 D1）把可积压 backlog 封顶 ≤6 chunk；bound=4 是吞吐余量上限（14×）**而非单 tick 可兑现容量**——river 尺寸下单 tick 实际 ≤~2 chunk，灾后追赶按 runbook §4.5 配方（bound=1 + 抬墙），不依赖 bound 值。**live 侧现值即 4，实机零改动**。
2. `infra/env/node27-timeseries-compression.example:18` `5`→`4`，注释改写为"容量结论 + 推导指针（runbook §4）"，不再是任意默认。:70-79 的 catch-up hint（`=1`）块与三条 timeout-budget 默认值**字节不动**（tests :1570-1590 钉）。
3. `docs/runbooks/tier-node27-timeseries-storage.md` §4 "Per-tick capacity" (:279-303) 重写：容量公式（bound × 每日 1 tick ≥ 稳态到达 + 追赶余量）、四项实测输入、定值理由、"无需提频"显式结论（14× 余量，不另开 issue——AC-5 不留白）；:249-250 前向指针的 live-state 日期同步更新；"tracked in issue #1237" 指针改为已决记录。
4. **新增一条模板值钉**（tests/test_node27_timeseries_compression.py）：`^NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=4$` MULTILINE 严格断言（承 #1370 C-A 判例——防模板静默回漂；现状模板数值零测试钉）。
5. **OpenSpec delta**：`hypertable-compression` capability ADDED 一条 requirement（per-tick bound 是容量推导目标值，模板/实机/receipt 三面一致）。
6. **node-27 实机（merge 前）**：确认 live env `=4` 不变；直调 runner 产一个 dry-run receipt（RECEIPT_PATH 指 scratch，避免覆盖 scheduled receipt）证 `per_tick_bound=4, outcome=clean`；连同 2026-08-14T04:55:36Z 的 enforce receipt（per_tick_bound=4，压 2 chunk committed）一起入 PR 证据。

## Non-Goals

- 不改超时墙（840s/900s/940s 三层，#1156 已交付且 live 无 override）。
- 不改 chunk 选择算法、不改 LAG_SECONDS（live 已从 issue 时点的 604800 演化为 172800——仅登记，不判定）、不改 timer 频率（结论：无需提频）。
- 不改 runner 代码与 receipt schema（`per_tick_bound` 已在三个 receipt 构建点自记录，改值即自证）。
- RECEIPT_PATH 模板/实机漂移（issue 明文不处理）。
- retention 兄弟 `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND`（唯一存活兄弟，product_archive/db_export_salvage 已随 #1370 退役——issue 兄弟登记表按此更新）。
