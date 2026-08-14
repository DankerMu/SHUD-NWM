# Tasks: archive-lane-retirement（#1370）

## 1. retention enabled 模式退役（D1/D2）

- [x] 1.1 `_resolve_archive_gate`：仅 `disabled` 通过；unset/`enabled`/其余 → `RETENTION_CONFIG_INVALID` exit 2 无 receipt，诊断含 `archive lane permanently retired (ADR 0002 Revision 2026-08-11)` 与显式设定指引。**CLI 裁定（fixture 复审 P2-6）：`--archive-gate` choices 保留 `{enabled,disabled}` 双值，拒绝在 `_resolve_archive_gate` 统一路径发生**——`choices=["disabled"]` 会走 argparse usage error（无 wire code 无 ADR 文本），违反 delta scenario 3
- [x] 1.2 删除 `load_completeness_receipt`/`load_drill_receipt`/`check_completeness_gate`/`check_drill_gate`、`_COMPLETENESS_SCHEMA_PATH`/`_DRILL_SCHEMA_PATH`、`RetentionConfig` 两路径/两 max-age 字段及其解析；`run_retention` gate 分支坍缩（disabled 短路语义逐字节保留：`covered_eligible=eligible`、`salvage_backed_windows=[]`、boundary-partial 进 candidate）；模块 docstring 与 `:19`（§7.5 锚）/`:1030`（§3.2 锚）注释指向随 runbook 重构对齐
- [x] 1.3 `WIRE_CODES` 移除 13 归档族 code；receipt schema 1.1 与 `archive_gate` 块零改动（D1 弃 (b) 理由在案）。**反向 walk 裁定（P1-2）：`_WIRE_CODE_ALLOWLIST` 扩入 13 个退役 code + 退役注释（#855 pending design 语料含全部 13 token 且不可改）**；正向 walk（:391）4 存活 code 双面在场不动
- [x] 1.4 tests：删 57 个 enabled-gate 测试 + `_completeness_receipt`/`_drill_receipt` helper + 13∪4==17 分区/monkeypatch 钉；**改写（非删除）6 处 enabled-可达钉（P1-3 枚举）**：`:4782` 解析表 unset/enabled/enabled-mixed-case 行改 config-invalid 期望、`:4843-4844` cli-enabled-beats-env-disabled 改拒绝、`:4861` parse_args enabled 仍解析成功但 resolve 拒绝、`:4877` gate-unset/gate-enabled 路径必填行删除、`:5149-5150`/`:5398` enabled 参数化收缩为 disabled-only；新增 D1 解析表（unset→invalid 红证先行）、`WIRE_CODES` 全集==4 且无 `COMPLETENESS_`/`DRILL_` 前缀断言；#1369 disabled-行为测试（~22）零修改通过

## 2. 四脚本 + 治理 + 共享库删除（D2/D3）

- [x] 2.1 删 `scripts/node27_{product_archive,archive_rebuild_drill,db_export_salvage,storage_inventory_audit}.py` + 各 `_once.sh` + 4 个测试文件（153/83/71/161 tests）；`tests/test_node27_wrapper_pythonpath.py` **深度 trim（P2-4）**：`_WRAPPER_CASES` 收缩为 3 存活 lane + 模块级 `_AUDIT_WRAPPER`(:16) 及其 5 个独立测试（:123/:158/:179/:215/:908）+ product-archive 专属分支（:200-201,:223,:227,:240,:610,:737-738,:796-797,:900）全部清除
- [x] 2.2 governance：`DEFAULT_SERVICES` 移除 4 归档 unit；`collect_archive_root`/`AuditConfig.archive_root`/`ARCHIVE_FREE_*` 水位/receipt `archive_root` 字段删除；测试 trim（unit 快照更新 + 否定断言 + `archive_root` 组 + #1310 三文件测试删除）
- [x] 2.3 `packages/common/storage.py` 归档 helper 块删除（`ArchiveIdentity`…`resolve_archive_storage_config`，实施前 grep 复核仅两个被删脚本消费）；`test_storage.py` ~21 归档测试删除；retention 的 `DEFAULT_RETENTION_WINDOW_DAYS` import 不动；`packages/common/state_manager.py:1971` + `tests/test_state_manager.py:2614` mover 注释改历史时态
- [x] 2.4 schemas：删 5 schema + **7** examples；`test_timeseries_storage_schemas.py` trim `SCHEMA_BASES`(:47) 与 4 个 helper import，**retention receipt 1.1 测试保留**（38 是文件总数不是删除数）
- [x] 2.5 systemd/env：删两组 unit 文件 + 4 归档 env example；governance example 水位块、retention example 两 receipt 路径/max-age 段删除；**retention example + 运行时 rollback 文案改写（P2-2）**：`:57-59` "remove this line" 改为 "set ENFORCE=0 / disable the timer"（drop ARCHIVE_GATE 行 = config-invalid 非回滚）、`:65` "enabled (the default)" 措辞退役；`compute.example:32` 注释清理；`node27-timeseries-compression.example:2` 指向两个被删 env 的注释修正（**compression 行为面不动的 fence 例外，仅注释**）；h13 key 元组同步收缩
- [x] 2.6 杂项：`.large-file-guard.json` 4 条目移除；`frontier_stall_alert.py:336` 悬空注释修复；`instructions/agents/shared.md` archive_root 段更新 + 再生 AGENTS.md/CLAUDE.md；`docs/runbooks/current-production-ops.md` archive_root 句对齐；`docs/governance/DOC_STATUS.md:154-158` runbook 定位句更新

## 3. runbook 重构（D5，doc-procedure-coherence 风险包）

- [x] 3.1 开篇定位改写 + 退役记录段（指 ADR 与 git 历史）；mover/audit 节（:220-928）、§3、§7 删除
- [x] 3.2 §8 交叉引用重写（~30 处 §3/§7 链）；§8.2 code 表收缩为 4 + 历史注记；§8.4 BRANCH A 删除、BRANCH B 转正（ADR 逐字锚保留）；§8.5/§8.7 恒态化；**§8.1 rollback 句（:3099-3101）改写（P2-2）**："drop the env line" → "ENFORCE=0 / disable timer"
- [x] 3.3 **`## Rollback` 节（:3768-3796）重写（P2-3）**：删两条归档 timer disable 命令、"consumed byte-for-byte by #855 retention" 句、§3 salvage 恢复指针；只保留 compression/retention 存活 lane 的回滚语义
- [x] 3.4 #1369 文档钉逐一存活核对（ADR 锚、OnCalendar、`retention-2*.json` glob、锚定 grep、bare-token walk——含 P1-2 的 allowlist 路线下反向 walk 仍绿）；4 归档 receipts README 加 retired 头（历史 receipt JSON 不删）
- [x] 3.5 **收尾轮方法继承**：实现与每次 fix pass 结束前对自己新增/改动行跑 occurrence-audit grep 自查表（regime = disabled 恒态 + 归档车道不存在）

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q`（全量，删除后无 import 残链；真实 DB 侧以 node-27 为 oracle）；`uv run ruff check .`
- [x] 4.2 删除完整性 grep（P2-5 加宽）：`grep -rn "node27_product_archive\|archive_rebuild_drill\|db_export_salvage\|storage_inventory_audit\|archive_completeness\|collect_archive_root\|NHMS_ARCHIVE_ROOT\|NHMS_ARCHIVE_FREE_SPACE\|nhms-node27-product-archive" scripts/ packages/ tests/ infra/ docs/ .github/ --exclude-dir=receipts` 仅剩历史注记/退役记录行（逐行分类入 PR 证据）；issue Verification 三命令全过
- [x] 4.3 `openspec validate archive-lane-retirement --strict --no-interactive` + **`openspec validate --all --strict --no-interactive`** + **归档演练（P1-1）**：在临时副本跑 `openspec archive archive-lane-retirement --yes` 证明 tombstone 路线可完成（两 capability 归档后各余 1 requirement）
- [x] 4.4 node-27 实机（merge 前）：**预检——`grep -n '^NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=' env文件` 确认 `disabled` 是设定态**（D1 生产 no-op 的前提；2026-08-14 #1369 落地时已设，仍须复核）→ `systemctl --user disable --now nhms-node27-product-archive.timer nhms-node27-storage-inventory-audit.timer` → unit 文件清除（`~/.config/systemd/user/`）→ `daemon-reload` + **`reset-failed`**（product-archive 每小时拒绝态残留）→ **`list-timers` + `list-unit-files | grep -i archive`** 双取证（防独立 enable 的 .service 漏网）→ governance runner 一跑，live receipt 无归档 unit/`archive_root` 块（box 残留 `NHMS_ARCHIVE_*` env key 被 `os.getenv` 静默忽略，无需清理——一句话入证据防误追）→ retention timer NEXT 仍在场旁证 → 证据入 PR 评论
- [x] 4.5 CI 定向测试绿（run 31825925400：Unit Tests 18m03s pass 实跑非 collect-only；lint/schema 门全绿；database filter 未命中按设计 skip）——2026-08-14 完成；4.4 证据见 PR #1381 评论（预检 disabled 设定态、4 unit 删净、双取证零归档条目、governance live receipt 顶层无 archive_root 键、retention NEXT 05:15 UTC 在场、NHMS_ARCHIVE_* 残留 0）

## 偏离与范围外挂账

- hot 侧库存审计需求（若再现）单独立项——D3 不可分离裁定的报告项。
- `docs/adr/0002-…md:288/292` 两个指向被删 schema 的 markdown 链接**保留不改**（Non-Goals 冻结 ADR；历史文档指向 git 历史可达对象，记录为接受的死链——deviation 报告项）。
- `openspec/specs/timeseries-db-retention` :171/:233/:314 REMOVED、:371 MODIFIED；两 capability spec 走 tombstone（P1-1 裁定）；#855 pending fixture 语料不动（其 13 token 由 allowlist 吸收，P1-2）。
- `/data/GHDC` 硬件（#1309）、DB ghdc tablespace 叙事（#1290）、object store 孤儿数据（#1355/#1357/#1359）不动。
