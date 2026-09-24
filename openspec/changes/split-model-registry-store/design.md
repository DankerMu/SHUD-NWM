## Context

沿用 `openspec/changes/archive/2026-09-24-split-oversized-surfaces-batch-2/design.md` 的 D2（超线未豁免消费者零改动）、D6（AST 指纹 / 命名空间 oracle）。行号取自 `8768f5d55`，仅作线索。

实测结构：模块级代码约 1220 行（`:1-590` 异常/常量/dataclass/helper，`:3427-4058` 模块级 helper + `_PsycopgTransaction`）；`PsycopgModelRegistryStore` 为 `@dataclass(frozen=True)`（`:590-616` 装饰器、4 个字段、`__post_init__`），类体 `:591-3425` 约 2835 行 → 必须拆成至少 3 个 mixin。

## Decisions

### D1 — 类拆分：mixin 组合

- facade 保留 `@dataclass(frozen=True) class PsycopgModelRegistryStore(<mixins>)`、4 个字段、`__post_init__`、`from_env`（23 处字符串 patch `"services.orchestrator.scheduler.PsycopgModelRegistryStore.from_env"` 落在类对象上；其 `-> PsycopgModelRegistryStore` 注解放 mixin 会 F821）、`_transaction`（`:3424`）、`_attribution_connect_kwargs`，以及模块级 `_PsycopgTransaction`（`:4011`）。
- mixin 必须是**普通类**：无带注解的类属性/字段、无 `__init__`/`__eq__`/`__hash__`/`__setattr__`/`__slots__`；`register_*` 方法内的 `object.__setattr__` 继续有效。
- 导入单向：leaf 模块（异常、dataclass、常量、helper）→ mixin 模块 → facade。mixin 不 import facade。facade 按同一对象 re-export 全部 leaf 名（含 `QHH_LATEST_READY_RUN_STATUSES`，`tests/test_real_basin_discovery_integration.py:207` 以 `model_registry_module.QHH_LATEST_READY_RUN_STATUSES` 读取）。
- **所有新模块命名为 `packages/common/model_registry_*.py`**（CI 路由前缀，见 D4）。
- **K3 余量**：`list_basins` / `list_basin_versions`（`:824`/`:869`，#1729 目标）与 `_lock_basin_version_scope`（`:2707`，#2491 确认点）所在 owner 模块须 ≤ 900 行，并在 implementer 报告中点名。
- MRO：拆前单类不可能重名，搬运后用脚本证明各 mixin 与 facade 类的**非 dunder** `__dict__` 名两两无交集；另证明任何 mixin 除 `__module__`/`__qualname__`/`__doc__`/`__dict__`/`__weakref__` 外不定义 dunder。

### D1b — patch seam（实测）

- **类属性 patch**：65 处、14 个类属性名——`_transaction` ×25、`_fetch_active_model_for_scope` ×5、`_fetch_model_lifecycle_row` ×5、`_fetch_trustworthy_rollback_history` ×5、`model_lifecycle_operation`、`_update_model_lifecycle_state`、`_lock_basin_version_scope`、`_insert_model_lifecycle_audit` ×3、`_fetch_direct_grid_activation_history` ×3、`_json`、`_execute_values` ×2、`_build_model_operation_preflight` ×2、`_fetch_idempotent_rollback_retry_history` ×2、`_dispatch_pre_activation_hooks`；另加 `from_env`（字符串形式 ×23），合计 15 名。`setattr` 设在子类上，`self.<m>` 先命中子类 → 对 mixin 方法仍生效。undo 语义：pytest 对类目标记录 `target.__dict__.get(name, notset)`，名字只在 mixin 上时记为 `notset`，undo 走 `delattr`，查找回落 mixin → 与拆前等价。
- **模块级 patch：0 处**（无 `setattr(model_registry_module, ...)`、无 `"packages.common.model_registry.<x>"` 字符串 patch），因此先例「被 patch 名的调用者留在被 patch 模块」在本 change 不产生约束；EF2 的模块级半边为 n/a（附测量命令）。
- `psycopg2` / `execute_values` 在 `_json`（`:2583`）、`_execute_values`（`:2598`）、`_PsycopgTransaction.__init__`（`:4019`）**函数内**导入，`"psycopg2.extras.execute_values"` 与 `sys.modules` 替换与代码所在文件无关。

### D2 — 冻结消费者零改动

超线未豁免、必须零 diff：`tests/test_model_registration.py`(4313)、`tests/test_variant_activation_cutover.py`(1770)、`tests/test_state_clone_index_publish.py`(1258)、`tests/test_source_scoped_dispatch.py`(1164)、`tests/test_legacy_reactivation_guard.py`(1115)、`tests/test_model_activation_audit_integration.py`(1098)、`services/orchestrator/scheduler_core.py`(1019，`:127` 调 `_scheduler.PsycopgModelRegistryStore.from_env()`)、`tests/test_replay_lineage.py`(1015)。

### D3 — 源码 / 路径钉

断言级钉（必须保持非 vacuous 且绿）：
- `tests/test_real_basin_discovery_integration.py:204-209`：`inspect.getsource(model_registry_module)` 的缺席断言。`list_basins` 搬出 facade 后该检查会**平凡变绿（vacuous）**——本 change 须把它扩为 facade + 全部 `model_registry_*` owner 模块的源码（该文件 231 行，可改）。
- `tests/test_node27_connection_attribution.py`：`:883` closure 行、`:1218` `test_attributed_display_unit_module_attributes_every_connect_site`（`sites>=1`）、`:1278` store-factory 检查、`:1311-1330` `test_shared_store_module_hard_codes_no_surface_name` 只参数化 `model_registry.py` → 加入全部新 owner 模块（该文件已豁免）。`_owns_connect_surface`（`:1005`）匹配裸 `psycopg2.connect` / `create_engine` **属性引用**（不止调用）；closure walk 须能到达每个 mixin 模块。新模块不得出现这两个引用。
- `scripts/select_ci_tests.py:2362`、`tests/test_select_ci_tests.py:13032`、`:14023`（`INTEGRATION_TRIGGER_SOURCES`）、`.github/workflows/ci.yml:103`、`openspec/specs/ci-contract-baseline/spec.md:820`/`:928`、两个 partition oracle（见 D4）。
- river-segment 写面：已核——`tests/test_river_segment_write_surface_scan.py` 只钉 `workers/model_registry/` 下的 `BACKFILL_MODULE`/`UPSERT_MODULE`，对 `PRODUCTION_DIRS` 全量扫描、无按路径白名单；本模块唯一 river-segment SQL 是 `create_river_network` 内的普通 `INSERT INTO core.river_segment`（`:964`，无 ON CONFLICT DO UPDATE），搬家不改变扫描结果；spec `:820` 场景因 facade 路径仍在、`packages/**` 为扫描根而继续成立。无需同步。

仅注释 / docstring（无断言，不受影响，可选同步）：`tests/test_real_database_integration.py:1044`、`tests/test_role_boundary_static.py:309`、`tests/test_openapi_response_conformance.py:83/:131`、`tests/test_variant_activation_cutover.py` 与 `tests/test_state_clone_index_publish.py` 的 docstring（冻结，不改）、`packages/common/state_clone_hook.py:15`、`packages/common/station_set_flip.py:21`、`apps/api/openapi_restored_schemas.py` 的 `:source:` 行。

### D4 — CI real-DB lane 与 partition oracle

`.github/workflows/ci.yml` 的 `database:` filter、`tests/test_select_ci_tests.py` 的 `INTEGRATION_TRIGGER_SOURCES`、`openspec/specs/ci-contract-baseline/spec.md`「Integration-owned production sources MUST trigger real-database CI」的注册表只匹配精确路径 `packages/common/model_registry.py`——K3 若只改 owner 模块会跳过 `real-db-integration`。本 change：
- ci.yml **保留**原字面量并在其旁**新增** `packages/common/model_registry_*.py`（先例 `apps/api/routes/hydro_display_*.py`）；
- `INTEGRATION_TRIGGER_SOURCES` 同步；
- ci-contract-baseline MODIFIED delta（orchestrator 在实现后写入）。
- `tests/fixtures/basins_registry_partition_oracle.json`（`/database_authority/baseline_patterns[6]`）与 `tests/fixtures/qhh_bootstrap_partition_oracle.json`（`/ci_database_authority/baseline_patterns[6]`、`patterns[6]`）记录了字面量 `packages/common/model_registry.py`，`_registry_database_contract` / `_qhh_database_contract` 断言 baseline pattern 不被删除 → 只能加不能换；**oracle JSON 不得重生成**。

### D5 — selector

`scripts/select_ci_tests.py` 中以精确路径 `packages/common/model_registry.py` 为 key 的规则（如 `CONNECTION_ATTRIBUTION_STORE_PATHS`、`:2362`），新 owner 模块获得等价 target 集合。#2612 的既有缺口不在本 change 修。

## Governing invariant

拆后所有调用者（importer、类属性 patch、源码钉、connect 归属、CI 路由）观察到的名字、行为、patch 效果与拆前相同；每个产出文件 < 1000 行；guard exclude 不变。

## Declared drift

移动的方法与顶层类的 `__qualname__` / `__module__` 按构造改变（`_XxxMixin.m`）；`PsycopgModelRegistryStore.__bases__` 改变。全仓无消费者读取这些（grep `__qualname__|__module__|__dict__|vars(|pickle|dataclasses.fields|replace` 已核）。

## Sibling surfaces

- Importers：全部引用文件零改动（除 D3/D4 列出的可改验证面）。
- Validators：connection attribution、real basin discovery integration、write-surface scan、select_ci_tests、ci.yml filter、partition oracles。
- Failure paths：import cycle；dataclass 语义（fields/repr/eq/hash）。

## Required evidence

见 tasks.md Evidence Floor。

## Non-goals

#1729 / #2491 / #1480 行为修改；#2612；其余超线文件。

## Review focus

1. 15 个类属性 seam 仍命中（突变证据）；from_env 字符串 patch。
2. dataclass 语义不变；mixin 为普通类；无 MRO 覆盖；导入单向。
3. 源码钉（尤其 discovery getsource 与 connect-surface 扫描）不 vacuous。
4. CI real-DB 路由对 owner 模块生效，oracle 不重生成。
5. K3 目标方法所在模块 ≤ 900 行。
