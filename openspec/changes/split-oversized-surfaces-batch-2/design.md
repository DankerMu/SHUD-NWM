## Context

先例是 `openspec/changes/archive/2026-09-21-split-oversized-surfaces-batch/`（PR #2534）。其 D1（patch 面裁定）、D1b（facade `__setattr__` 广播）、D2（collect suffix oracle）、D5（显式 `PathTestRule` + tracked-tree 守卫）、D6（每组一 commit，exclude 与拆分同 commit）本 change **原样沿用**，下文只写本批新增或收紧的决定。行号均取自 `5ceeaa1d5` 实测，仅作线索；实现按符号/字符串定位。

Change surface: 4 个 issue 的 6 个文件（proposal 表）+ 其拆出模块；`.large-file-guard.json`；`scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`（二者已豁免）；ADR 0009 普查表（条件性）。

## Decisions

### D1 — patch seam：调用点留在被 patch 的模块（沿用先例 D1，精确化）

`monkeypatch.setattr(<mod>, name, ...)` 能打到真实路径的条件是：**调用 `name` 的函数**在 `<mod>` 内、经 `<mod>` 的模块全局查找 `name`。被调函数本身可以搬走，只要 facade `from .x import name` 且调用者留在 facade。反之，调用者搬走而 facade 仍 re-export 同名 → patch 成功但 vacuous。每组拆分前由 implementer 枚举 `tests/`、`scripts/` 下对该模块的全部 `setattr(<mod>, "<name>"` / `patch("<dotted>.<name>")`，逐条裁定并在报告中列出；每个 seam 至少做一次树外突变（破坏真实调用点 → 依赖该 seam 的用例转红）。

**实施中收紧的裁定（组 4 实测）**：字面规则「被 patch 名的**所有**调用者留原模块」在 qhh / bri 上不可满足（闭包留存定义 qhh 1535 行、bri 1177 行，facade 无法 < 1000）。本 change 采用的精确规则：**每个现存 patch 所经过的调用路径**上的调用者留在被 patch 模块、经其模块全局解析该名；未被任何现存 patch 经过的其它调用者可外移。例：`stat_no_follow` 唯一的 patch（`tests/test_qhh_production_bootstrap.py:441-442`）直接调 `_bounded_discovery_preflight`，只它留 qhh；bri 的 `_fetch_optional`/`_mesh_uri`/`_source_checksum`/`_resource_profile`/`_json` patch 直接调 `_refresh_parent_version_materialization`，它留 bri；11 个 spy 的调用者 `import_basin_into_registry_core` 留 bri，被 spy 的 `_ensure_*` 本身外移。`setattr(qhh_bootstrap, "execute_values", ..., raising=False)` 在 master 上即为 no-op（函数内局部 `from psycopg2.extras import execute_values`），`_seed_station_rows` 外移不改变其效果。每条 seam 均以突变证据核验（现存测试或树外探针转红）。代价：将来新写的测试若 patch facade 名并经过已外移的调用者，须改 patch owner 模块。

seam 搜索必须覆盖三种形式：`setattr(<mod>, "<name>"`、`setattr(<mod>.<submod>, ...)`（如 `os.path`）、**字符串形式** `setattr("<dotted>.<name>", ...)` / `patch("<dotted>.<name>")`。已知 seam（fixture review 实测，非穷举）：

- `basins_registry_import`（`bri`）：`tests/basins_registry_import_helpers.py` 对 11 个私有名 spy（`_delete_legacy_seg_rows`、`_refresh_parent_version_materialization`、`_ensure_basin`、`_ensure_basin_version`、`_ensure_river_network`、`_ensure_river_segments`、`_ensure_output_river_segments`、`_ensure_river_segment_crosswalk`、`_ensure_mesh`、`_ensure_model_instance`、`_backfill_output_segment_geometry`）；`tests/test_basins_registry_import.py:333-337` patch `_fetch_optional`、`_mesh_uri`、`_source_checksum`、`_resource_profile`、`_json`；`tests/test_basins_registry_import_auth.py` 字符串 patch `...basins_registry_import._read_json_object` / `._prepare_sources`；`tests/test_basins_registry_import_qhh.py:492` patch `...basins_registry_import.psycopg2`。这些名字的**调用者**全部留在 `bri`；只外移既不被 patch、也不调用任何被 patch 名的纯 helper。
- `qhh_production_bootstrap`：`execute_values`、`_bootstrap_database`、`os.scandir`（经 `qhh_bootstrap.os`，facade 保留 `import os`）、`stat_no_follow`、`_write_reserved_evidence_path`、`_bounded_discovery_preflight`、`discover_basins_inventory`（`tests/test_qhh_production_bootstrap_state.py:316-317`；后者定义在 `basins_discovery`，但其在 qhh 内的调用者留 qhh）。
- `basins_discovery`：`tests/test_basins_package_publication_refusal.py:326` 与冻结的 `tests/test_basins_discovery.py:1285` patch `_sha256`；`tests/test_basins_discovery.py` 10 处 `setattr(basins_discovery.os.path, "realpath")`（全局，但要求 facade 保留 `import os`，并 re-export `_glob_non_sidecar_files`、`_walk_files`，见其 :13/:122 import）。
- `basins_package_source_io`：`tests/test_basins_package_publication_rivseg.py:475` patch `_iter_mapping_snapshot_lines`，其调用者 `_parse_declared_mapping_rows` 必须留在 `basins_package_source_io.py`——因此 mapping-parse 块（约 :216-400）**不可**作为瘦身切口，换其它块。

### D2 — 死锁面零改动（本批新增硬约束）

guard 拒绝任何 staged 的超线未豁免文件。实测以下**消费者**超线且未豁免，本批对它们必须**零改动**，因此 facade 必须 re-export 它们今天 import 的每一个名字、它们 patch 的每一个 seam 必须仍有效：

`tests/test_basins_discovery.py`(1453)、`tests/test_model_registration.py`(4313)、`tests/test_direct_grid_variant_registration.py`(2871)、`tests/test_forcing_domain_handoff_apply.py`(1286)、`tests/test_forcing_read_path_store_routing.py`(1162)、`tests/test_production_scale_validation.py`(1008)、`tests/test_qhh_scripts_static.py`(1059)、`scripts/audit_first_cycle_initial_state.py`(1063)。

若某条静态断言（如 `read_text()` 对源码的字面量断言）因搬迁必然转红且只能改这些文件才能修，则该切口不可取——换切口，不加豁免。已知的源码钉（必须原位）：

- `tests/test_qhh_scripts_static.py:358-360` 读 `qhh_production_bootstrap.__file__` 并要求字面量 `COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'`——所在函数 `_output_segment_order_offset` 留 qhh。
- `tests/test_forcing_read_path_store_routing.py:736-757` 按路径读 reader 源码；`tests/test_forcing_ts_template_census.py`、`tests/forcing_ts_template_registry.py` 与 `scripts/select_ci_tests.py` 按精确路径钉 `_DYNAMIC_FORCING_COUNT_TEMPLATES` / `_dynamic_forcing_count_sql` / `_dynamic_forcing_counts`——三者留 qhh。
- `tests/test_node27_connection_attribution_delegated.py` 把 `basins_registry_import.py` 登记为 autopipeline 静态 import 闭包里唯一持有 `psycopg2.connect` 的模块（`tests/test_node27_connection_attribution.py` 逐文件 AST 检查）——`_transaction` 留 `bri`，**任何新 owner module 不得引用 `psycopg2.connect` 或 `create_engine`**。
- `tests/test_basins_package.py:719-733` 要求 glob `basins_package*.py` 恰好 6 个文件——#2460 从 `basins_package_source_io.py` 抽出的新模块名**不得**匹配 `basins_package*.py`（如用 `basins_source_*.py`）。

### D3 — 写面扫描与锁序绑定不动

`tests/test_river_segment_write_surface_scan.py` 钉死 `BACKFILL_MODULE=basins_registry_import.py::_backfill_output_segment_geometry` 与 `UPSERT_MODULE=qhh_production_bootstrap.py::_seed_output_segment_rows`，并断言唯一的 `UPDATE core.river_segment` / `geometry_generation` 写 / upsert 都在这两处；`scripts/select_ci_tests.py` 有镜像（#2185）。本批：这两个函数留在原文件，**任何**含上述 SQL 语句的函数都不外移，扫描常量与 selector 镜像零改动。`_lock_river_network_version` 留在 `bri`，`qhh_production_bootstrap` 对它的 import 路径不变（#2157 rnv 优先锁序）。

### D4 — #1948 契约与 partition oracle

`tests/test_select_ci_tests.py` 的 `_qhh_partitions()` 守卫禁止 qhh 分区路径与含 `qhh_production_bootstrap` 子串的 pattern 进 exclude；本批不加任何 exclude，天然满足。`tests/fixtures/qhh_bootstrap_partition_oracle.json` / `basins_registry_partition_oracle.json` 的生成器已不在（#2183）；生产模块在 `workers/model_registry/` 内拆分预期不影响它们。**若 `uv run pytest -q tests/test_select_ci_tests.py` 因 oracle 转红，停止并上报，不得静默重生成 oracle。**

### D5 — ADR 0009 成员优先不搬

`basins_discovery.py::_safe_resolve_under_root`、`basins_package_source_io.py::_resolve_package_path` 是 ADR 0009 权威成员（调用 `os.path.realpath`），普查表 `docs/adr/0009-path-canonicalization-dereference-doctrine.md` 按 `模块::函数` 取键，`tests/test_path_canonicalization_family_guard.py` 要求 `ADR 0009 clause N` 标记在成员函数体内。优先选**不含 realpath 调用**的切口，使键不变；若成员必须搬，则标记随函数体一起搬、普查表同 commit 更新。`basins_package_source_io.py` 目标 ≤ 900 行。

### D6 — 测试拆分 oracle（沿用 D2/D5 先例）

- `--collect-only -q` 的 `::test_name[param-id]` 后缀集合拆前落盘、拆后逐字节相等（`test_node22_refresh_timer_health` 与 `test_direct_grid_display_cutover_flip` 两个语料）。
- AST+sha256 逐定义指纹：每个 `def`/`class`/顶层赋值的「decorator + 精确源码段」拆前后集合相等，`missing`/`extra`/`changed` 为空（import 块与 module docstring 除外）。生产模块拆分同样适用：搬走的定义源码段不变。
- `scripts/select_ci_tests.py` 为每个新测试语料加显式枚举全部分区的 `PathTestRule`（替换指向旧单体路径的字面量）；`tests/test_select_ci_tests.py` 加 tracked-tree 守卫（恰好 N 分区 + M helper），二者各做一次 mutation 红证。
- #2527 特有：selector pin 还包括 `tests/test_select_ci_tests.py` 的 `GUARDED_MODULE_CLOSURES` known-member 条目（改指仍顶层 import `apps.api.routes.hydro_display` 的分区）、`test_select_tests_maps_mvt_tiles_without_core_smoke_fallback` 的精确集合 pin、反 vacuity 锚点（实测约 :13767），以及 `scripts/select_ci_tests.py` 的字面量 rule target（约 :3261/:3385）。`:1069` 附近的磁盘结构锁 `assert "def _station_source_version" in disk_source` 读 `apps/api/routes/hydro_display_identity.py`，所在分区必须保留 `Path(__file__).resolve().parents[1]` 的 repo-root 解析；反 vacuity 锚点按文件名钉该路径，至少一个分区仍 `from apps.api.routes.hydro_display import _station_source_version`，锚点改指该分区。
- #2532 特有：`NODE22_UNIT_OWNER_SUITES` / `NODE22_REFRESH_READER_EDGES` 在 **`tests/test_select_ci_tests.py`**（约 :434/:459），另有负向 pin（约 :516）与 test-id 锚 `::test_the_production_path_defaults_match_every_file_that_states_them`（约 :3457-3464）；`scripts/select_ci_tests.py` 字面量约在 :577/:1101/:4344/:4558/:4618/:4636-4644——全部按字符串定位改为枚举新分区（test-id 锚改指该用例所在分区）。

## Governing invariant

对每个被拆面：拆后所有调用者（生产 importer、CLI、测试、monkeypatch）观察到的名字、行为与 patch 效果与拆前完全相同，且每个产出文件 < 1000 行、guard exclude 只减不增。

## Sibling surfaces

- Producers/entrypoints：`workers/model_registry/cli.py`、`scripts/node27_autopipeline.py`、`scripts/reingest_all_basins_receipt.py`、`services/tiles/mvt.py`、`services/production_closure/object_store_validation*.py`、`scripts/geo/build_national_*_geo.py` 等 importer——零改动，import 必须仍解析。
- Validators：`tests/test_path_canonicalization_family_guard.py`（ADR 0009）、`tests/test_river_segment_write_surface_scan.py`（写面）、`tests/test_select_ci_tests.py`（selector/partition/#1948）、`tests/test_qhh_scripts_static.py`、`tests/test_forcing_read_path_store_routing.py` + `tests/test_forcing_ts_template_census.py`（forcing 模板 census）、`tests/test_node27_connection_attribution*.py`（connect owner）、`tests/test_basins_package.py`（glob 恰好 6）、`.claude/hooks/large-file-guard/test-large-file-guard.sh`（hook 自测）。
- Importers of `basins_package_source_io`：`workers/model_registry/basins_package.py`（19 处 `from .basins_package_source_io import`）及 inventory / manifest / object_store 相关 importer——零改动。
- Main specs：`openspec/specs/ci-contract-baseline/spec.md` 在 "Guarded-module selector rules MUST cover their non-gated importer closure"（mvt one-hop 列表含 `tests/test_direct_grid_display_cutover_flip.py`）、"display and scheduler unit files with a content-asserting owner suite MUST select that suite"、"the scheduler refresh env template MUST select its content-asserting owner suite"（含精确 selector 输出场景）按路径点名两个被拆语料——本 change 同 PR 以 MODIFIED delta 改指实际分区名（拆分落地后由 orchestrator 写入）。
- ADR 0009：普查表键（D5），以及「已具名的缺口」段对 `basins_discovery.py` 行数/豁免的描述（exclude 移除后需同步）。
- Storage/SQL：无（零 SQL 改动）。
- Failure paths：import cycle（新模块反向 import facade 在 `python -m` 下造第二份模块副本——禁止，沿用先例 D1b 理由）。

## Seams under test

模块 import 面（facade re-export 集合）、monkeypatch seam、pytest 收集集合、selector 路由、guard hook 本身。

## Required evidence

见 `tasks.md` Evidence Floor。

## Non-goals

- 任何行为 / SQL / 契约改动；#2491 #1729 #1480（批 K3）的锁序或数据修复。
- #2490 的 3 个 MVT/river_ts 测试文件（已豁免，#2490 保持 open）。
- `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` 豁免（#2527 推荐保留）。
- D2 列出的超线消费者本身的拆分。

## Review focus

1. 每个 monkeypatch seam 的调用者是否仍在被 patch 模块内（D1），突变证据是否齐。
2. D2 死锁面是否零 diff；facade re-export 是否覆盖它们的 import。
3. 写面扫描 / `_lock_river_network_version` / ADR 0009 标记是否原位（D3/D5）。
4. `.large-file-guard.json` diff 恰好 -3、0 新增；每个产出文件 < 1000。
5. 新测试分区的 `PathTestRule` 显式枚举 + tracked-tree 守卫及其 mutation 红证。
