# Design: fix-backfill-geometry-rnv-scope

## Context

- 缺陷点唯一：`workers/model_registry/basins_registry_import.py:1252-1274`。全仓只有这一处 `UPDATE core.river_segment`
  （`tests/test_river_segment_write_surface_scan.py:126-143` 钉住）。读侧 `:1167-1179` / `:1195-1210` 已按网络限定，
  `updates` 列表（`:1225-1245`）里的 `river_segment_id` 因而全部来自目标网络——只是到了 UPDATE 阶段脱离了那层作用域。
- 调用方四处，均在调用方事务内、同一 cursor：`basins_registry_import.py:439-444`（`only_missing=True`）、
  `qhh_production_bootstrap.py:694`（返回值进 `_qhh_output_segment_geometry_counts` 与 receipt）、`:1076`、
  `scripts/node27_autopipeline.py:1361-1382`（`only_missing=True`；返回值进 tick receipt 字段 `output_geometry_backfilled`，`:1356`——
  无人值守 timer 路径，是多计问题最直接的落点；`tests/test_node27_connection_attribution_delegated.py:96-103` 钉住其 cursor 交接契约，本修复不动）。
- `execute_values`（`psycopg2/extras.py:_split_sql`）要求 SQL 里恰有一个 `%s`；`template` 决定每行元组的展开形态；
  `fetch=True` 拼接每页 `RETURNING`（`:1247-1251` 注释解释分页计数）。
- 既有 unit fake（`tests/test_hhe_mvt_binding.py:58-66`）按下标读 `rows[0][0]`（id）与 `rows[0][3]`（provenance）。
- 真实 DB 测试约定：`@pytest.mark.integration` + `integration_database_url`（session 级 `nhms_it_<uuid>` 库，结束 drop）+
  `apply_migrations_from_zero`（`tests/conftest.py:164-178`、`tests/integration_helpers.py:43-71`）；行级 seed 走直接 INSERT
  （`seed_issue_126_data` `:147-230` 是模板：`geom` 用 `ST_Multi(ST_GeomFromText(%s, 4490))`，`properties_json` 用 `Json(...)`）。
- CI：`real-db-integration`（`.github/workflows/ci.yml:228-270`，`timescale/timescaledb-ha:pg15-latest` 含 PostGIS，
  `pytest -m "integration and not timescaledb_210"`）由 `database` 过滤器触发，`db/**`（`:86`）与 `workers/model_registry/**`（`:101`）在内；
  PR 须非 draft。定向 `unit-test-targeted` 会收集本文件但按 marker 跳过。

## Goals / Non-Goals

**Goals**
- 一次回填只写目标 `river_network_version_id` 的行；返回值只数目标网络；他网 `geometry_generation` 不动。
- 用例在真实 PostGIS 上修复前红、修复后绿，钉住"跨网不越界"而非恒真。

**Non-Goals**：见 `proposal.md`（入口收窄、#2154/#2155/#2156/#2157、部署）。

## Decisions

### D1. 网络 id 走 VALUES 第 5 列，而不是第二个 bind
`execute_values` 只允许一个 `%s`，独立 bind 会抛 `ValueError`。备选"把 rnv id 用 `psycopg2.sql.Literal` 拼进 SQL 字符串"引入
字面量拼接面，被否决。因此每条 `updates` 元组变成 `(reach_id, geom_wkt, total_length, provenance, river_network_version_id)`，
`template="(%s, %s, %s, %s, %s)"`，子查询列表 `AS value(river_segment_id, wkt, length_m, provenance, river_network_version_id)`，
SELECT 列增加 `value.river_network_version_id::text AS river_network_version_id`，WHERE 增加
`AND target.river_network_version_id = source.river_network_version_id`。**追加在末尾**，既有 fake 按下标 0/3 读取不受影响。
类型注解 `updates: list[tuple[str, str, float | None, str, str]]`。

### D2. 计数与 bump 语义不变，只是现在真的等于目标网络
`RETURNING target.river_segment_id` + `fetch=True` + `len(updated_rows)` 保持；谓词收窄后返回值恒 ≤ 目标网络候选行数。
`geometry_generation + 1` 仍 gated on `updated_rows`，且 `WHERE river_network_version_id = %s` 只打目标网络——修复后
"他网被改写却不轮转"这一半自然消失，因为他网根本不再被改写。

### D3. 用例放进新文件 `tests/test_backfill_geometry_network_scope_integration.py`，直接 INSERT，不走 CLI
- 不能放进 `tests/test_basins_registry_import_db.py`：#1913 把 7 个 `tests/test_basins_registry_import*.py` partition 的**整个 test 定义清单**冻结在
  `tests/fixtures/basins_registry_partition_oracle.json`，`tests/test_select_ci_tests.py:14486/14497/14512/14588` 四条守卫对 node 数、integration 数、
  逐定义 AST 摘要与执行语义计数做相等断言——实测加一条 test 即 4 红（fixture 第一版的前提错误，实现者报告纠正）。守卫自述禁止
  用被检对象回填基线（`:13957-13970`），且生成器已随 `.workplans/issue-1913/` 消失，故不改 oracle。
- 新文件名**不以** `test_basins_registry_import` 开头（`:14470` corpus 断言）、**不含** `qhh_production_bootstrap`（`:12572`），文件名带 `integration`
  使其同时命中 `.github/workflows/ci.yml:108` 的 `tests/*integration*.py` 触发路径；`real-db-integration` 跑的是全树 `-m "integration and not timescaledb_210"`（`:270`），
  不依赖 selector 选中。不为它加 `PathTestRule`：定向 CI 只会把 integration 用例收集后按 marker 跳过，选中与否零断言差异。
- 文件自带 `pytestmark = pytest.mark.integration`，导入 `apply_migrations_from_zero`/`psycopg_connection`（`tests/integration_helpers.py`）与 `Json`。
- 直接 INSERT 而不走 `import-basins-registry` CLI：CLI 路径经 `_ensure_model_instance`（`:1508-1533`）的 checksum guard 造不出
  同名 id 跨网碰撞（issue "可达性"段已证）；直接 seed 是最小可控形态，且正是 `create_river_network` 放行的那种数据。
- 用 `integration_database_url`（与文件内其余用例一致）+ 唯一前缀 `it2158_` + 开头按前缀 DELETE，不用 `throwaway_database_url`
  （不改 schema，不值一次建库）。
- 最小 seed 链：`core.basin` → `core.basin_version`（`geom` 用 `ST_Multi(ST_MakeEnvelope(...))`）→ 两条 `core.river_network_version`
  （`segment_count` 2）→ 每网两行 `core.river_segment`。`core.model_instance` / `core.mesh_version` 本函数不读，不 seed。
- A/B 的源 reach 几何、`length_m`、`Type` 全部不同（A：`LINESTRING(110.0 30.0, 110.6 30.6)`, 1200.0, Type 3；
  B：`LINESTRING(116.0 36.0, 116.6 36.6)`, 1800.0, Type 4），这样 (b) 的"逐字相等"能区分"没写"与"写成了 A 的值"。

### D4. 红→绿证据
- CI：`real-db-integration` 在 PR 上真实执行该用例（绿）。
- node-27：throwaway worktree（`git worktree add /home/nwm/tmp/wt-2158 <head>`，不动活动树）+ 生产解释器
  `/home/nwm/NWM/.venv/bin/python -m pytest`，`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<从 infra/env 读取，不回显>`，
  `TMPDIR=/home/nwm/tmp`；先 `python -c "import workers.model_registry.basins_registry_import as m; print(m.__file__)"` 确认解析到
  throwaway worktree 而非活动树（editable install 陷阱）。先跑绿；再 `git show origin/master:workers/model_registry/basins_registry_import.py > 该文件` 跑红
  （预期 (b)/(d) 失败：B 被改成 A 的几何、返回 2）；`git checkout -- 该文件` 再跑绿。三段输出进 receipt，DSN 脱敏。
  throwaway 库 `nhms_it_<uuid>` 由 fixture 建/删，不触碰 `nhms` 库数据。

### D5. STORED `stream_type` 作为 (a)/(b) 的一部分
`000048_river_segment_stream_type.sql:4-15` 的生成列由 `properties_json->>'Type'` 派生并钳制到 `[1.0, 5.0]`（`:9-12`，所以 B 的源 reach 取 `Type` 4 而不是会被钳到 5.0 的 7）；(a) 断言 A 输出行 `stream_type == 3.0`，
(b) 断言 B 输出行 `stream_type IS NULL` 不变——这是 issue 里"跨网改写 `stream_type`"后果链的直接观测。

## Risks / Trade-offs

- 元组多一列，`execute_values` 每页 100 行的 SQL 体积略增；忽略不计。
- 若未来 `river_segment_id` 真的跨网复用且**本意**是同步几何，本修复会让 B 保持旧几何——那是另一个功能，需要显式的跨网同步设计而非漏谓词。
- `openspec/specs/basins-registry-import/spec.md:200-222` 仍声称本函数已删除（#2155）；本 delta 与之并存直到 #2155 处理，
  `openspec validate --strict` 不做语义一致性检查。

## Boundary-surface checklist

- 唯一生产写点 `:1252-1274`；无兄弟副本（write-surface scan 钉住）。
- 本函数可达的 `execute_values` fake 只有 `tests/test_hhe_mvt_binding.py:58-65` 与 `:104-113`（位置参数取 `rows`，只按下标 `[0][0]`/`[0][3]` 读），
  追加末列不破坏。`tests/basins_registry_import_helpers.py:441` 的 `_FakeRiverSegmentCursor.insert_rows` 按 4 值解包，5 元组会抛 `ValueError`——
  但它只绑定在 `_ensure_output_river_segments` 用例（`tests/test_basins_registry_import.py:107,134`），那里 backfill 是 `MagicMock`
  （`basins_registry_import_helpers.py:656`，`:277/:302` 断言 call_count），不可达；`tests/test_qhh_production_bootstrap*.py` 的 fake
  （`:720-727`、`_state.py:35-42`）声明 keyword-only `template`/`page_size`，今天就会在 backfill 的 `fetch=True`/无 `page_size` 调用上 `TypeError`，同样不可达。
- `tests/test_qhh_production_bootstrap_state.py:112-145` 的 provenance 结构（`geometry_source_segment_count`）不变。
