# Proposal: archive-lane-retirement（#1370）

## Why

归档车道（product mover + storage inventory audit + rebuild drill + db-export salvage）已按 ADR 0002 Revision 2026-08-11 永久退役（`/dev/md0` 双盘故障不重建），但仍以活组件形态留在仓与实机：product-archive timer 每小时 fail-closed 拒绝刷 journal 噪音；治理 receipt 对永久拒绝态 unit 的健康判读失真；~10,400 行脚本 + ~15,400 行测试 + 5 个 schema + 4 组 systemd/env 指向不存在的 `/data/GHDC`，持续派生 #1358 类"修死门"工作。#1369 已将 retention 切到 `disabled` 模式（node-27 实机 2026-08-14 生效），生产已无任何归档 receipt 消费方。

## What Changes

1. **retention enabled 归档 gate 模式一并退役（explorer 关键裁定的 (a)/(c) 路线）**：`load_completeness_receipt`/`load_drill_receipt` 及两 gate 判定函数、13 个归档族 wire code、两 receipt 路径与 max-age env、57 个 enabled-gate 测试全部删除。**env 变量 `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` 保留且必须显式 `disabled`**：unset 或 `enabled` → `RETENTION_CONFIG_INVALID` exit 2 无 receipt，诊断文本引 ADR revision 与本 change——fail-closed 方向不翻转（unset 永不静默删除），死代码归零。receipt schema 1.1 与 `archive_gate` 块**零改动**（mode 枚举照旧，实际只会出现 `disabled`）。
2. **四脚本 + wrapper + 测试删除**：`node27_product_archive.py`(4627L)、`node27_archive_rebuild_drill.py`(2860L)、`node27_db_export_salvage.py`(1137L)、`node27_storage_inventory_audit.py`(1759L，经归属分析**不可分离**——archive/hot 两腿在 `build_receipt`/`run_audit` 无边界交织，整体退役；hot 侧需求若再现单独立项)＋各自 `_once.sh` 与测试文件（153+83+71+161 tests）。git 历史即归档，不入 attic。
3. **schemas 删除**：`archive_completeness_receipt`、`archive_rebuild_drill_receipt`、`product_archive_manifest`、`product_archive_receipt`、`salvage_manifest` 5 个 schema + 7 个 examples + `test_timeseries_storage_schemas.py` 的归档 schema 测试（trim `SCHEMA_BASES` 与 helper import；retention 1.1 测试保留）（注：无独立 db_export_salvage receipt schema，salvage 校验走 salvage_manifest）。
4. **治理面**：`node27_resource_governance.py` `DEFAULT_SERVICES` 移除 4 个归档 unit 条目；`collect_archive_root` 与 `archive_root` receipt 块、`ARCHIVE_FREE_*` 水位退役；`test_node27_resource_governance.py` 对应 trim（含 #1310 三文件水位一致测试——其两个参照 env 文件被删）。
5. **共享库**：`packages/common/storage.py` 归档 helper 块（`ArchiveIdentity`…`resolve_archive_storage_config`，仅两个被删脚本消费）+ `test_storage.py` ~21 归档测试删除（取代 #1358）。
6. **systemd/env**：product-archive、storage-inventory-audit 两组 unit 文件删除；4 个归档 env example 删除；governance example 归档水位块、retention example 两 receipt 路径/max-age 段、`compute.example:32` 陈旧注释清理。
7. **runbook 重构**：`tier-node27-timeseries-storage.md` 开篇定位改写；mover/audit 节（:220-928）、§3 salvage、§7 drill 删除（顶部留一段退役记录指向 ADR 与 git 历史）；§8 约 30 处 §3/§7 交叉引用与 §8.2 wire-code 表随 enabled 退役重写；4 个归档 receipts 目录 README 加 retired 头（历史 receipt 文件保留——证据不删）。
8. **杂项**：`.large-file-guard.json` 4 个点名条目移除；`frontier_stall_alert.py:336` 悬空注释修复；`instructions/agents/shared.md` archive_root 段更新并再生 AGENTS.md/CLAUDE.md；`docker-compose.dev.yml:13` ghdc 注释保留（仍有效）。
9. **node-27 实机落地（merge 前）**：`systemctl --user disable --now` 两 timer + unit 文件清除 + daemon-reload；`list-timers` 无归档 unit 取证；governance live receipt 不再含归档 unit/archive_root 块；retention timer（#1369 已启用）不受影响的旁证。

## Non-Goals

- retention gate `disabled` 语义与 timer 运维（#1369 已交付，本 change 只删 enabled 死路）。
- object store 孤儿 legacy station-index 数据清理（#1355/#1357/#1359）。
- `/data/GHDC` 卷/硬件处置（#1309）与 DB `ghdc` tablespace 叙事（#1290）。
- ADR 0002 本体改写（授权记录保持原样）。
- `openspec/changes/archive/**` 与历史 receipt JSON（证据/历史不删）。
- #855 pending fixture 的归档顺序义务（其 change 归档时处理）。
