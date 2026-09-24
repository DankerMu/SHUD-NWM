## Why

`.large-file-guard.json` 的 1,000 行提交门是整文件绝对值判定（`.claude/hooks/large-file-guard/large-file-guard.sh` 对每个 staged 路径取整文件行数）。当前 master（`5ceeaa1d5`）上有 5 个越线面处于「touch 即死锁」或「已豁免、信号永久关闭」状态，实测 `wc -l`：

| issue | 文件 | 行数 | guard 状态 |
|---|---|---|---|
| #2532 | `tests/test_node22_refresh_timer_health.py` | 3456 | 已豁免（PR #2534 为解锁 #1099 新增） |
| #2527 | `tests/test_direct_grid_display_cutover_flip.py` | 1814 | 已豁免（PR #2526 新增） |
| #2460 | `workers/model_registry/basins_discovery.py` | 1118 | 已豁免（PR #2456 新增） |
| #2460 | `workers/model_registry/basins_package_source_io.py` | 999 | 未豁免，0 行余量 |
| #2490 | `workers/model_registry/qhh_production_bootstrap.py` | 2901 | 未豁免，#1948 契约禁止豁免 → 死锁 |
| #2490 | `workers/model_registry/basins_registry_import.py` | 2481 | 未豁免 → 死锁 |

后两个是下一批（K3：#2491 #1729 #1480）必须修改的文件，不拆则 K3 无法合规提交。

## What Changes

纯物理搬运，零行为变更，同一 PR 不夹带任何 refactor / bug fix。按 4 个验证单元（每 issue 一 commit）拆分，exclude 删除与拆分同 commit：

- **#2532**：`tests/test_node22_refresh_timer_health.py` → 多个 collectible 分区 + 非收集 helper；删除 exclude。
- **#2527**：`tests/test_direct_grid_display_cutover_flip.py` → harness helper + SUB-1 / SUB-2 两个分区；删除 exclude；`docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` 豁免按 issue 推荐**保留**（长 runbook 政策延续，见 #2527 正文）。
- **#2460**：`workers/model_registry/basins_discovery.py` 拆 owner module，原模块保为 facade；`basins_package_source_io.py` 抽出一块降到 ≤ 900 行；删除 exclude；同步 ADR 0009 普查表（若成员搬迁）。
- **#2490（部分）**：`qhh_production_bootstrap.py`、`basins_registry_import.py` 拆 owner module，原模块保为 facade 与编排入口。

**BREAKING**：无。public import、CLI、env、SQL、测试名/参数 ID/marker/断言均不变。

## Impact

- Affected specs: `orchestrator-structural-burndown`（ADDED requirements）；`ci-contract-baseline`（MODIFIED：三条按路径点名被拆语料的 requirement 改指新分区，tasks 5.4b）。
- Affected code: 上表文件及其拆出模块；`.large-file-guard.json`（恰好 -3 条 exclude，0 新增）；`scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`（新测试分区的显式路由与 tracked-tree 守卫）；`docs/adr/0009-path-canonicalization-dereference-doctrine.md`（仅当 ADR 0009 成员搬迁）。
- **#2490 范围偏离（记录在案）**：#2490 原文还包含 3 个 MVT/river_ts 集成测试文件（`tests/test_mvt_national_identity_probe_integration.py` 2370、`tests/test_river_ts_read_path_surrogate_keys_integration.py` 1418、`tests/test_node27_mvt_cache_retention.py` 1927）。立单后它们已被 `4a72f7f13` / `b795e4720` 加入 exclude，不再死锁；用户为本批给定的文件清单只含两个生产文件。本 change 只交付生产侧两半，PR 不写 `Closes #2490`，#2490 保持 open 追踪剩余 3 个测试文件。
- Out of scope（report-only）：其余 exclude 条目与 `maxLines`；`tests/test_basins_discovery.py`(1453)、`tests/test_model_registration.py`(4313)、`tests/test_direct_grid_variant_registration.py`(2871) 等越线未豁免的**消费者**——本批必须保证它们零改动（见 design D2）。

## Triage

```text
Issue type: refactor
Fixture level: expanded
Upstream suggested level: absent (override: n/a — expanded forced by ADR 0009 path-safety member, 14+ production importers, monkeypatch seams, write-surface scan constants)
Blast radius: 静默失效的 monkeypatch seam（负向用例 vacuous pass）、import cycle、selector 路由丢失新分区、ADR 0009 标记脱离成员
Selected risk packs: Public API / CLI / script entry; File IO / path safety; Legacy compatibility; Concurrency / shared state / ordering（rnv 锁序 import 路径）
Evidence floor: 见 tasks.md Evidence Floor
```
