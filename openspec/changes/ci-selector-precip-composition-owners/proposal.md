# Proposal: ci-selector-precip-composition-owners (#2098)

## Why

降水栅格服务（#2010 / PR #2094）的两个**应用组合 owner** 决定了公开路由可达性与 runtime OpenAPI，
却都选不到降水 oracle。

- `apps/api/route_registry.py:9,22` import `precip_router` 并把它放进 `_BUSINESS_ROUTERS`；
  `register_role_aware_routes` 逐个 `include_router`。这条边一旦断，两个已发布端点
  `/api/v1/precip/{source}/{cycle}/index` 与 `/api/v1/precip/{source}/{cycle}/{valid_time}.png` 整体变 404。
- `apps/api/main.py:334` 在 `_patch_openapi_schema` 里调用 `_patch_precip_openapi(schema)`
  （`:356` 是对 `openapi_patching` 的 re-export facade）。这条边一旦断，runtime schema 与
  `openapi/nhms.v1.yaml` 漂移。

实测基线（`7fc9a43c`，`select_ci_tests.py --changed-file`，单路径输入）：

```text
apps/api/route_registry.py => test_api, test_api_contract, test_monitoring_api,
                              test_node27_connection_attribution, test_node27_connection_attribution_delegated
apps/api/main.py           => test_api, test_api_contract, test_api_errors_logging, test_monitoring_api
```

两者都**不含** `tests/test_precip_overlay.py` 与 `tests/test_openapi_drift.py`。
对照组 `apps/api/routes/precip.py` 与 `services/precip/cache.py` 均选到完整 `PRECIP_SURFACE_TESTS`。

成因：#2010 的 selector 补丁（`4e29818a`）只覆盖服务树与路由模块本身，漏掉了随后决定 reachability
与 runtime schema 的组合 owner。缺口早于 #1903（issue 已用 `git diff --quiet` 证实）。

与 #2195 的沉默方式不同：这里选择集非空且体面（4–5 个 suite），`#1182` 零断言告警不触发，
PR 页面无任何提示，但**改这两个文件时降水回归的直接 oracle 一个都不跑**。

## What Changes

### 1. `scripts/select_ci_tests.py` — 两条 owner 边

**`apps/api/route_registry.py`：从 `CONNECTION_ATTRIBUTION_ROUTE_PATHS`中移出，改为独立 path-exact 行。**

不能新增一条同 pattern 的规则——`test_path_rule_duplicate_patterns_are_allowlisted_decisions`
会红。house precedent 是同文件 `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 上方的 #2078 注释：`apps/api/routes/forecast.py`
因为已有精确行，attribution suites 被 **MERGE 进那一行**而非在 tuple 里重复列出；
`forecast_store.py` / `state_manager.py` 同理。本 change 对 registry 做同一手法：

```text
apps/api/route_registry.py -> CONNECTION_ATTRIBUTION_TESTS + PRECIP_SURFACE_TESTS
```

该 tuple 上方的注释有两处被证伪（「plus the registry itself」；以及「each declares a module-level
`_APPLICATION_NAME`」——registry 全文无此名），**必须同步改写**，
并按 #2078 的写法说明 registry 为何 deliberately absent。

**`apps/api/main.py`：扩展既有 `apps/api/main.py` 精确行的 target 元组**，由 `(API_ERROR_LOGGING_TEST,)` 改为
`(API_ERROR_LOGGING_TEST, *PRECIP_SURFACE_TESTS)`。该行已存在，直接合并，无 duplicate pattern 风险。

两条边均**不加** `stop_on_match`、**不加** `only_when_any_changed`：`apps/api/**` 规则在其后仍需累加
三个通用 API suite，任一 flag 都会破坏既有 riders。

**不复用 `PRECIP_SURFACE_TESTS` 之外的构造**：该常量已被 `services/precip/**` 规则（另加 prewarm）与 precip 路由行共享，语义正是「降水表面 oracle 集」，两条 owner 边指向同一集合是正确的；
但**元测试侧不得 import 它**（见下）。

### 2. `tests/test_select_ci_tests.py` — 显式字面量钉 + 反向缺失断言

验收标准第 3 条明确禁止「只通过与生产实现共享同一常量而自证」。因此两条 owner 的期望集合
**写成字面字符串的精确集**，不写 `set(PRECIP_SURFACE_TESTS)`；手法照 #2122 的
`test_precip_route_rule_stays_without_the_prewarm_suite`。

- 每条 owner 一条**精确相等**钉（字面集合，改后实测值为准）。
- 每条 owner 一条**反向缺失**钉：monkeypatch 掉该 owner 的 precip targets 后，`tests/test_precip_overlay.py`、`tests/test_openapi_drift.py`、`tests/test_openapi_31_contract.py` 三个降水 suite必须
  从选择集中消失（red leg）。**不含** `tests/test_api_contract.py`——它同时是 `apps/api/**` 的 rider，
  删掉 owner 的 precip targets 后依然在，写进断言会直接红。
- 一条 flags 钉：两条 owner 行的 `stop_on_match` 与 `only_when_any_changed` 均为假。
  二者对这两条路径**行为惰性**（`apps/api/**` 在后累加，精确集钉抓不到 flag 本身），
  spec delta 里「neither flag」那条 SHALL 需要独立钉。手法照 `test_precip_tree_rule_carries_no_selection_flags`。
- 一条 attribution 表完整性钉：`CONNECTION_ATTRIBUTION_ROUTE_PATHS` 移出 registry 后，
  其余 5 个 route 路径的选择结果逐条不变，且 registry 仍选到两个 attribution suites
  （即「MERGE 而非 DROP」）。没有它，把 registry 从 tuple 里删掉却忘了合并 targets 是全绿的。

### 3. `tests/test_precip_overlay.py` / `tests/test_openapi_drift.py` — 两条构造性 mutation proof

验收标准第 4、5 条要求的是**测试**（issue 边界原文：「增加防止该两条边消失的 selector 测试与构造性
mutation 测试」），不是一次性 receipt。两条都在隔离 app fixture 内 monkeypatch，
`create_app()` 每次新建 app，**不改动生产路由与 OpenAPI 行为**：

- `tests/test_precip_overlay.py`：monkeypatch `route_registry._BUSINESS_ROUTERS` 为去掉 `precip_router`
  的元组 → `main.create_app()` → 断言新 app 的**路由表**不含两条公开降水路径（未 mutate 的正控腿含）。
  **不断言 HTTP 404**：SPA fallback 与降水路由自身都会对这两条路径产生 404，404 断言在 monkeypatch
  未生效时同样绿。
- `tests/test_openapi_drift.py`：monkeypatch `main._patch_precip_openapi` 为 no-op →
  新建 app 的 schema 在 `/api/v1/precip/{source}/{cycle}/index` 操作与 `PrecipIndexResponse` component
  **两处**都与 `openapi/nhms.v1.yaml` 不一致（未 mutate 的正控腿两处都一致）。逐处比较，不做整文档相等。
  `_patch_openapi_schema` 读的是模块级名字，monkeypatch 模块属性即生效；
  同文件 `test_openapi_patch_owner_module_preserves_main_monkeypatch_facade` 是该 idiom 的既有 precedent。

这两条钉住的是「oracle 确实会咬」——正是两条 selector 边存在的理由。缺了它们，
selector 边只是把 suite 拉进车道，却没有任何东西证明这些 suite 能抓住对应的回归。

## Non-Goals

- 不改降水服务、路由功能、静态 OpenAPI 内容（`openapi/nhms.v1.yaml` 零改动）。
- 不改生产路由注册与 runtime schema 行为——mutation 全部在隔离 fixture 内。
- 不做 import/runtime 关系的全仓自动依赖推导（issue 的备选方案，显式不取：规则可解释性、
  成本与 duplicate-pattern / stop-on-match 约束都更难维持）。
- 不动 `apps/api/**`、`apps/api/openapi_patching.py`、`apps/api/errors.py` 三条既有规则行。
- 不动 `PRECIP_SURFACE_TESTS` 的成员，不动 `services/precip/**` 与 precip 路由两条既有用法。
- 不碰 #1903 / #1875 的 Basins/rivseg 映射。

## 已知残留（本 change 有意不收）

`apps/api/openapi_patching.py` 自己有精确规则行，但其 target 里**没有** `tests/test_precip_overlay.py`——
`_patch_precip_openapi` 的真实实现体在该模块内，改它同样只跑 drift/31-contract 而不跑降水行为 suite。
issue 的 in-scope 只点名 `route_registry.py` 与 `main.py` 两个 composition owner，本 change 遵守该范围；
该行的降水覆盖缺口按「报告不修」处理，落地后另立单跟踪。

同族更广的问题——「应用组合 owner 与被组合能力的 oracle 之间无通用链接」——本 change 只解降水一支。

## Risk triage

- Fixture level: **expanded**。Upstream suggested level: absent（issue 无 `Suggested fixture level` 字段，
  `预估规模 S`）。取 expanded 而非 compact 的两条理由：(1) 本 change 触及**两个面**——
  selector 规则表 + 两个 runtime 测试套里的 app 组合 fixture，后者要 monkeypatch
  `_BUSINESS_ROUTERS` 与 OpenAPI patch 管线；(2) `CONNECTION_ATTRIBUTION_ROUTE_PATHS`
  是共享 tuple，把 registry 移出去会同时改变 duplicate-pattern 不变式的输入，
  blast radius 大于 #2195 的「新增一行」。
- Repair intensity: low-medium。
- Risk packs 与 evidence 见 `tasks.md`；设计取舍见 `design.md`。

## Must preserve

- `apps/api/route_registry.py` 改后仍选到两个 connection-attribution suites（MERGE 而非 DROP）。
- `apps/api/main.py` 改后仍选到 `tests/test_api_errors_logging.py`。
- 两者改后仍选到 `apps/api/**` 的三个通用 API suite。
- `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 其余 5 个 route 路径与 3 个 store 路径的选择结果逐条不变。
- `apps/api/routes/precip.py`、`services/precip/cache.py`、`apps/api/openapi_patching.py`、
  `apps/api/errors.py` 的选择结果逐条不变。
- duplicate-pattern / stop-on-match / 规则目标存在性三条既有守卫照旧绿。
- 生产 `_BUSINESS_ROUTERS` 内容与 `_patch_openapi_schema` 调用序列零改动；`openapi/nhms.v1.yaml` 零改动。

## Seams under test

- `select_tests(["apps/api/route_registry.py"], repo_root=Path("."))` 的精确输出。
- `select_tests(["apps/api/main.py"], repo_root=Path("."))` 的精确输出。
- 两条 owner 行的 `stop_on_match` / `only_when_any_changed` 均为假。
- `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 其余成员的选择结果。
- `route_registry._BUSINESS_ROUTERS` 去掉 `precip_router` 后两个公开降水路由的 HTTP 状态。
- `main._patch_precip_openapi` 变 no-op 后 runtime schema 与静态 YAML 的相等性。

## Evidence mapping

见 `tasks.md` Required evidence；本地即可闭环（issue `验证` 段全部为本地命令，无 node-22 / node-27 oracle 需求）。
