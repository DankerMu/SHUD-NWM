## Risk packs

- Public API / CLI / script entry — **selected**：14+ 生产 importer 与 `workers/model_registry/cli.py` 经 facade 解析 → EF5（import 面 + CLI `--help` 不变）。
- File IO / path safety / overwrite — **selected**：ADR 0009 成员 `_safe_resolve_under_root` / `_resolve_package_path` → EF6（family guard）。
- Legacy compatibility / examples — **selected**：monkeypatch seam、D2 死锁消费者 → EF4、EF3。
- Concurrency / shared state / ordering — **selected**：`_lock_river_network_version` import 路径与 rnv 锁序 → EF7（node-27 `tests/test_river_segment_lock_order_integration.py`）。
- Schema / columns / units / field names — not selected：零 SQL/DDL 改动（D3 钉死写面扫描）。
- Config / project setup — not selected：`.large-file-guard.json` 只删条目，`maxLines`/`enabled` 不变（EF2）。
- Resource limits / large input / discovery — not selected：零行为改动。
- Auth / permissions / secrets — not selected：不触及。
- Error handling / rollback / partial outputs — not selected：零行为改动；错误码/异常类由 facade 同一对象 re-export（EF5 覆盖 `is` 同一性）。
- Release / packaging / dependency compatibility — not selected：无依赖变更。
- Documentation / migration notes — selected（条件性）：ADR 0009 普查表（D5）。

## 0. Baselines（拆前落盘到 `.workplans/split-oversized-surfaces-batch-2/`）

- [ ] 0.1 `pytest --collect-only -q` suffix 集合：`tests/test_node22_refresh_timer_health.py`、`tests/test_direct_grid_display_cutover_flip.py`（排序后落盘，记录计数）
- [ ] 0.2 AST+sha256 逐定义指纹：6 个被拆文件
- [ ] 0.3 facade 公共面：每个被拆生产模块 `sorted(n for n in dir(mod))` 与 `python -m workers.model_registry.cli --help`（若存在）stdout
- [ ] 0.4 monkeypatch seam 清单：对 4 个生产模块的全部 `setattr(<mod>, "<name>"`、`setattr(<mod>.<sub>, ...)`、字符串形式 `setattr("<dotted>.<name>")` / `patch("<dotted>")`（design D1 已知 seam 为下限）

## 1. #2532 — `tests/test_node22_refresh_timer_health.py`（3456 行）

- [x] 1.1 拆为 collectible 分区（各 < 1000）+ 非收集 helper；只手写 docstring 与 import 块
- [x] 1.2 删除 `.large-file-guard.json` 该条 exclude，零替代
- [x] 1.3 `scripts/select_ci_tests.py` 中该路径的全部字面量 rule target，以及 `tests/test_select_ci_tests.py` 中的 `NODE22_UNIT_OWNER_SUITES` / `NODE22_REFRESH_READER_EDGES` / 负向 pin / test-id 锚（design D6），改为显式枚举新分区；`tests/test_select_ci_tests.py` 新增 tracked-tree 守卫；各一次 mutation 红证
- [x] 1.4 Evidence：suffix 集合与 0.1 逐字节相等；指纹零漂移；`uv run pytest -q tests/test_node22_refresh_timer_health*.py tests/test_select_ci_tests.py` 绿

## 2. #2527 — `tests/test_direct_grid_display_cutover_flip.py`（1814 行）

- [x] 2.1 harness/fixtures → 非收集 helper；SUB-1 原子 flip/回滚族、SUB-2 MVT 集合族 → 两个分区，各 < 1000
- [x] 2.2 删除该条 exclude；`docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` 豁免保留
- [x] 2.3 磁盘结构锁 `def _station_source_version` 断言仍读 `apps/api/routes/hydro_display_identity.py`、repo-root 解析不变；反例演练（改断言串一个字符 → 红）
- [x] 2.4 `scripts/select_ci_tests.py` 中 `hydro_display` / `hydro_display_catalog` importer 闭包注册表与字面量 rule target 覆盖全部新分区；`tests/test_select_ci_tests.py` 的 `GUARDED_MODULE_CLOSURES` known-member、mvt 精确集合 pin、反 vacuity 锚点改指（design D6）；新增 tracked-tree 守卫；mutation 红证
- [x] 2.5 Evidence：suffix 集合与 0.1 逐字节相等（记录计数）；指纹零漂移；`uv run pytest -q tests/test_direct_grid_display_cutover_*.py tests/test_select_ci_tests.py` 绿

## 3. #2460 — `basins_discovery.py`（1118）+ `basins_package_source_io.py`（999）

- [x] 3.1 `basins_discovery.py` 拆 owner module（`workers/model_registry/` 下），facade 与每个新模块 < 1000
- [x] 3.2 `basins_package_source_io.py` 抽出一块到新模块，结果 ≤ 900；新模块名不匹配 `basins_package*.py`；`_parse_declared_mapping_rows` 与 `_iter_mapping_snapshot_lines` 的调用关系留原模块（design D1）
- [x] 3.3 D1 seam 裁定（含 `_sha256`）逐条列出 + 突变红证
- [x] 3.4 D5：ADR 0009 成员不搬；若搬则标记随函数体 + 普查表同 commit 更新；ADR 0009「已具名的缺口」段对 `basins_discovery.py` 行数/豁免的描述同步
- [x] 3.5 删除 `workers/model_registry/basins_discovery.py` exclude，零替代
- [x] 3.6 Evidence：`uv run pytest -q tests/test_path_canonicalization_family_guard.py tests/test_basins_discovery.py tests/test_basins_package*.py tests/test_select_ci_tests.py` 绿；指纹零漂移

## 4. #2490（部分）— `qhh_production_bootstrap.py`（2901）+ `basins_registry_import.py`（2481）

- [ ] 4.1 `qhh_production_bootstrap.py` 拆 owner module，`bootstrap_qhh_production` / `_bootstrap_database` 编排入口、`_seed_output_segment_rows`、`_output_segment_order_offset`、`_DYNAMIC_FORCING_COUNT_TEMPLATES` / `_dynamic_forcing_count_sql` / `_dynamic_forcing_counts` 以及 D1 seam 的调用者留原文件；facade 与新模块 < 1000
- [ ] 4.2 `basins_registry_import.py` 拆 owner module，D1 全部 seam 名的调用者、`_backfill_output_segment_geometry`、`_lock_river_network_version`、`_transaction`（唯一 `psycopg2.connect`）留原文件；新模块不引用 `psycopg2.connect` / `create_engine`；facade 与新模块 < 1000
- [ ] 4.3 D1 seam 裁定逐条列出；`tests/basins_registry_import_helpers.py` 的 11 个 spy 各做一次突变红证（或按 spy-count 断言族做代表性突变并说明覆盖）
- [ ] 4.4 D3：`tests/test_river_segment_write_surface_scan.py` 与 selector 镜像零改动且绿
- [ ] 4.5 D4：`uv run pytest -q tests/test_select_ci_tests.py` 绿（#1913/#1948 partition 守卫）；若 oracle 红 → 停止上报
- [ ] 4.6 Evidence：`uv run pytest -q tests/test_qhh_production_bootstrap*.py tests/test_basins_registry_import*.py tests/test_river_segment_write_surface_scan.py tests/test_qhh_scripts_static.py tests/test_forcing_read_path_store_routing.py tests/test_forcing_ts_template_census.py tests/test_node27_connection_attribution*.py tests/test_select_ci_tests.py` 绿；指纹零漂移

## 5. 收口

- [ ] 5.1 `wc -l`：本批产出/修改的每个文件 < 1000（`scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py` 已豁免除外）
- [ ] 5.2 `git diff origin/master -- .large-file-guard.json`：恰好 -3 条（`tests/test_node22_refresh_timer_health.py`、`tests/test_direct_grid_display_cutover_flip.py`、`workers/model_registry/basins_discovery.py`），0 新增，其余逐字节不变
- [ ] 5.3 guard hook 对每组真实 `git commit` 返回 exit 0（commit 本身经 PreToolUse hook）；另手动喂 hook 一次 staged 全量并记录 `rc=0`
- [ ] 5.4 `git diff origin/master --stat` 不含 D2 列出的任何死锁消费者
- [ ] 5.4b orchestrator 按实际分区名写 `specs/ci-contract-baseline/spec.md` MODIFIED delta（三条按路径点名被拆语料的 requirement）
- [ ] 5.5 `uv run ruff check .` 绿；`openspec validate split-oversized-surfaces-batch-2 --strict --no-interactive` 绿
- [ ] 5.6 node-27 frozen-SHA：`TMPDIR=/home/nwm/tmp uv run pytest -q` 全量 receipt（#2490 要求全量）
- [ ] 5.7 PR body：不写 `Closes #2490`，记 #2490 部分交付偏离；声明 CI 定向选择 ≠ 全量

## Evidence Floor

1. 两个被拆测试语料的 collect suffix 集合与 baseline 逐字节相等（贴计数）。
2. `.large-file-guard.json` 恰好删除 3 条、0 新增；`maxLines`/`enabled` 与其余条目逐字节不变。
3. D2 死锁消费者零 diff；本批每个产出文件 `wc -l` < 1000；`basins_package_source_io.py` ≤ 900。
4. 每个 monkeypatch seam 调用者仍在被 patch 模块内，代表性突变转红（贴输出）。
5. facade 公共面：每个被拆生产模块拆后 `dir()` ⊇ 拆前 `dir()`，且同名对象 `is` 同一（或为同一源码段的重导出）；所有 importer 可 import。
6. `tests/test_path_canonicalization_family_guard.py` 绿；ADR 0009 普查表与成员实际位置一致。
7. node-27 全量 `uv run pytest -q` 绿（含 `tests/test_river_segment_lock_order_integration.py`、真实 DB 集成）。
8. 每个新测试语料：显式 `PathTestRule` + tracked-tree 守卫，各一次 mutation 红证；`tests/test_select_ci_tests.py` 绿。
9. `uv run ruff check .` + `openspec validate --strict --no-interactive` 绿。
