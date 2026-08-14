# Tasks: retention-archive-gate-disabled-mode（#1369）

## 1. 实现

- [x] 1.1 `config_from_args` + `RetentionConfig` + `_parser()`：`NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` enum 解析（D1：未设=enabled；strip+lower ∈ {enabled,disabled}；其余含空串 → `RETENTION_CONFIG_INVALID` exit 2）；CLI `--archive-gate` choices 优先于 env；D4 条件必填反转——**disabled 时不调用两路径的 `_resolve_path`**（其 `required=False` 分支是 raise，`:296-297`），config 字段转 `Path | None`
- [x] 1.2 `run_retention()` 模式路由（D2 四处消费点）：disabled 不调用 load/check 两 gate（相位 1a/1b/2b/1c/2c）；`_partition_by_completeness_bounds` 处 `covered_eligible = eligible`（**boundary-partial chunk 在 disabled 下会被删**，代码注释明记）；`derive_salvage_backed_windows` 处恒 `[]`；锁/watermark/窗口/per-tick bound/dry-run/enforce/H4/H5 全不变
- [x] 1.3 `build_receipt()` + schema 1.1（D3）：三分支通吃必填 `archive_gate`；`mode=disabled` 必带 const `adr_reference`；`schemas/timeseries_retention_receipt.schema.json` const bump `1.1`；`schemas/examples/timeseries_retention_receipt.example.json` 同步（1.1 + archive_gate）
- [x] 1.4 模块 docstring `:50` "retention gate IS the archive receipt gate — never bypassed" 修订为双模式表述（enabled fail-closed 默认 + disabled 显式模式引 ADR 0002 Revision 2026-08-11）
- [x] 1.5 文档：runbook §7 头退役 banner、§8 开篇 carve-out、§8.1 timer 启用步骤转正（去注释 + 落地后现状注记）、§8.2 注记（disabled 下 13 归档族 code 不可达，零新增 code）、§8.4 前置条件 OR 分支（逐字引 ADR revision）、§8.5 archive_gate 字段 + deferred/boundary-partial 的 disabled 注记 + §8.7 `salvage_backed_windows=[]` 读法、**Timer cadence order 表与 D6 receipt-新鲜度段（`:310-334`）修订**（audit→retention 新鲜度依赖在 disabled 下不适用）、Live-state notes `:308`；`infra/env/node27-timeseries-retention.example` 新变量块（强警示：删除不再有归档兜底、不可逆）；receipts README schema 1.1 双版本读法。**全文禁止裸 `RETENTION_ARCHIVE_GATE` token（反向 walk 会判 orphan），一律全名**

## 2. 测试（tests/test_node27_timeseries_retention.py + test_timeseries_storage_schemas.py）

- [x] 2.1 D1 解析表：未设→enabled；`enabled`/`disabled`（大小写/空白变体）→ 对应；`disable`/`true`/`1`/空串 → `RETENTION_CONFIG_INVALID`；CLI 覆盖 env；红证 = 先写断言后实现
- [x] 2.2 D4 双向：enabled+缺任一路径 env → CONFIG_INVALID（现状钉不动）；disabled+两路径全缺 → config 通过且字段为 None；disabled+两路径给定但指向不存在文件 → run_retention 照常成功（给了也不读的直接证明）
- [x] 2.3 D2 行为：disabled+dry-run → outcome=dry-run、candidate/deferred 正常、archive_gate.mode=disabled；disabled+enforce → drop 走通、enforced receipt 带 adr_reference 且 `salvage_backed_windows == []`；**boundary-partial 行**：enabled 下被 bounds-defer 的 chunk，disabled 下进入 candidate；enabled 全量既有测试除 schema_version/示例同步与 `:4676-4700` 两负例修复外零修改通过（默认回归网）
- [x] 2.4 不可达性钉（D2/Seams 3 钉法）：monkeypatch `load_completeness_receipt`/`load_drill_receipt`/`check_completeness_gate`/`check_drill_gate` 为 `raise AssertionError`，disabled 下 dry-run 与 enforce 均正常出 receipt；矩阵断言：disabled 下 refused receipt（并发锁/drop 失败/uncaught 三路）`refusal_reason` 前缀 ∉ **13 个归档族 code**（`COMPLETENESS_*`×5 + `DRILL_*`×8）；并发锁冲突 refused receipt 亦带 archive_gate
- [x] 2.5 D3 schema 负例：缺 archive_gate → invalid；disabled 缺 adr_reference → invalid；adr_reference 非 const 串 → invalid；enabled 带 adr_reference → invalid；schema_version 写 1.0 → invalid；正例：disabled enforced + `salvage_backed_windows: []` → valid；`test_timeseries_storage_schemas` 示例文档同步；**修复 `:4676-4700` 两个 format-checker 负例**（补 1.1 + 合法 archive_gate，使其只因 date-time 违规而红）
- [x] 2.6 文档钉：`test_env_example_lists_all_h13_keys`（`:4360`）key 元组扩入 `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`；runbook §8.4 含 `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` + `Revision 2026-08-11` 逐字锚；timer OnCalendar 钉不动仍绿

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_node27_timeseries_retention.py tests/test_timeseries_storage_schemas.py`
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate retention-archive-gate-disabled-mode --strict --no-interactive`
- [ ] 3.4 node-27 实机（merge 前，用户裁定 2026-08-14：**启用 timer 每日 05:15 UTC**）。**删除不可逆（归档 lane 已退役，无兜底）——回滚仅指停止后续删除（移除 env 变量 + disable timer），已删 chunk 无法恢复**。顺序硬约束：
  1. env 文件置 `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled`、`ENFORCE=0`、**receipt 路径变量留空/注释**（R5-2：固定路径会被每日 tick 原子覆盖；留空则 wrapper 生成 per-tick 时间戳文件；手动直跑 python 时必须显式 `--receipt-path <带时间戳路径>`，否则 config-invalid）→ 手动 tick → dry-run receipt（archive_gate.mode=disabled，candidate 清单落盘）
  2. 跑 §8.1 的逐 chunk 清单查询（round-1 F-E 补入：chunk 名/所属 hypertable/range_start/range_end/`chunks_detailed_size` 字节），覆盖 candidate + deferred_remainder 全量——**backlog 总条数与预估总字节先落 PR 证据**（`enable --now` 授权的是整个 backlog 以 ≤5 chunk/天磨完，不只是首刀）；做非循环交叉核验（清单查询按 receipt 自己的 cutoff 过滤，逐行核 range_end 是循环论证）：核对 receipt 的 `window_days`/`reference_time` 与实机 env 实值（runbook/README 记录现为 21d）及 display watermark 一致——window 配错会静默放宽每一行；确认首刀爆炸半径（≤5 chunk 的表名/时间范围/字节，含原 bounds-deferred 的 boundary-partial chunk）后 → `ENFORCE=1` → 手动 tick → enforced receipt（≤5 chunk，freed_bytes 记录，`salvage_backed_windows=[]`）
  3. `systemctl --user daemon-reload && systemctl --user enable --now nhms-node27-timeseries-retention.timer` → `systemctl --user list-timers` 取证（NEXT 触发时刻在场）。**R5-1 警示：env 常驻 `ENFORCE=1` 后，`--dry-run` flag 不覆盖 env（装饰性）——此后任何手动 dry-run 必须显式前缀 `NODE27_TIMESERIES_RETENTION_ENFORCE=0`**。**R5-2 验证项（R6-1 修订口径）：等到（或手动触发——注意强制 tick 在 ENFORCE=1 常驻下是又一次 ≤5 chunk 的不可逆删除，在已授权 backlog 包络内但须知情）第二个 wrapper tick，`ls` 只看 wrapper 命名形态 `retention-2*.json`（`retention-dryrun-*`/`retention-enforce-*` 是手动 receipt、不算数）确认两个不同时间戳的 wrapper receipt 并存**
  4. 两 receipt + 清单查询输出 + timer 状态贴 PR 评论；**两份落地 receipt 补记进 receipts README `## Receipts` 列表**（round-1 F-C：否则 README `:7` 与 `:168-172` 自相矛盾）；落地若受阻，README 完成时态措辞必须在 merge 前改回"决策已记录、实机状态以 receipt 为准"
- [ ] 3.5 CI 定向测试绿

## 偏离与范围外挂账

- 归档侧脚本（mover/inventory-audit/drill）及 `packages/common/storage.py` 跨读的处置 → #1358，不动。
- ADR 0002 sub-issue 实现表（`:284-294`）无 #1369 行——范围外报告（ADR 本体本 change 不改写）。
- **R5-3 spec 语料冲突（round-2 裁定，已双路处置）**：#855 pending fixture（`openspec/changes/tier-node27-timeseries-storage/specs/timeseries-db-retention/spec.md`）的 "Missing or stale gate receipts → MUST refuse" 与 "boundary-partial MUST remain intact" 两 scenario 被 disabled 模式证伪。本 change delta 已加 supersession 句（disabled 下本 requirement 优先，两 scenario 仅 enabled 下有效）；归档顺序义务：#855 归档时应为该两 scenario 补 `archive_gate=enabled` 前提。MODIFIED 路线经实证会使 `openspec archive` 硬中止（requirement 尚不在 `openspec/specs/`），不可用。
- **R5-4.3（DEFER 报告项）**：#855 design fixture `:1922` 引用的 schema 行号 `:40-68` 在 master 即偏 3 行、本 diff 后再移 19 行；语义结论（三分支 oneOf、无 partial）仍成立；无行号新鲜度钉，不在本 PR 编辑他人 fixture。
