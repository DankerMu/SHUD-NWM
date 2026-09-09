# Design: ci-selector-precip-composition-owners (#2098)

## 决策 1：registry 从共享 tuple 移出，而非新增一条同 pattern 规则

`apps/api/route_registry.py` 目前由 `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 的推导式生成规则
（`PathTestRule(path, CONNECTION_ATTRIBUTION_TESTS) for path in ...`）。要让它再选到降水 suite，
三条路各自的代价：

| 方案 | 结果 |
|---|---|
| 新增一条 `apps/api/route_registry.py` 精确行 | **否**。技术上合法但要把该 pattern 记进 `INTENTIONAL_DUPLICATE_PATTERNS`；守卫失败信息自己给的首选处置就是 "consolidate the entries"，且与 #2078 house precedent 相反 |
| 把 `PRECIP_SURFACE_TESTS` 加进 `CONNECTION_ATTRIBUTION_TESTS` | **否**。该常量还喂着 5 个 route 路径 + 3 个 store 路径，会给 8 个无关 owner 买 4 个降水 suite |
| registry 移出 tuple，改为独立精确行、targets 合并 | **取**。house precedent 明确 |

第三条正是同文件 #2078 注释记录的既有做法：`apps/api/routes/forecast.py` 因为已有精确行，
attribution suites 被 MERGE 进那一行而非在 tuple 里重复；`forecast_store.py` / `state_manager.py`
同理。本 change 只是把同一手法用在 registry 上——区别是 registry 的精确行由本 change 新建，
而非并入既有行。

**代价**：tuple 上方注释「plus the registry itself」被证伪，必须改写。这是 #2195 round 3 的教训
（注释与实际结构脱节），此处提前记在 tasks 里，不是可选项。

## 决策 2：main.py 直接扩展既有行

`apps/api/main.py` 已有精确行，target 只有 `API_ERROR_LOGGING_TEST`。直接把
`PRECIP_SURFACE_TESTS` 并进去即可，无结构变动、无 duplicate pattern 风险。
两条 owner 因此走了不同形状的改法——这不是不一致，而是各自既有结构决定的最小改动。

## 决策 3：元测试不 import 生产常量

验收标准第 3 条禁止「与生产实现共享同一常量而自证」。若元测试写
`== set(CONNECTION_ATTRIBUTION_TESTS) | set(PRECIP_SURFACE_TESTS) | {...}`，
则任何对 `PRECIP_SURFACE_TESTS` 成员的改动会**同时**改变生产与期望，钉永远绿——正是自证。

因此期望集合写成字面字符串。代价是 `PRECIP_SURFACE_TESTS` 未来增删成员时，
本 change 的两条精确钉会红，需要人工同步——**这是想要的行为**，不是维护负担：
降水 oracle 集的变动本就应该经过一次显式复核。#2122 的
`test_precip_route_rule_stays_without_the_prewarm_suite` 是同一取舍的既有先例。

## 决策 4：反向缺失断言的方向

`assert precip_suites <= selected` 在「owner 边被删」时会红，方向正确。
但 #2195 的实测教训是：子集向断言在**派生集为空**时真空为绿。这里期望集是硬编码字面量、
不是派生集，所以子集向不会真空——不过精确相等钉已经覆盖了它。
反向缺失钉的真正价值在于**定位**：精确集钉红时只说「集合不等」，
反向钉直接说「三个降水 suite 没了」。两者都留。

注意反向钉只能点名三个：`tests/test_api_contract.py` 虽在 `PRECIP_SURFACE_TESTS` 里，
但同时是 `apps/api/**` 的 rider，删掉 owner 的 precip targets 后**依然在选择集里**。
把四个一起断言会直接红——这正是「心算并集」的典型翻车点。

## 决策 5：mutation proof 是常驻测试，不是一次性 receipt

issue 边界写的是「增加……构造性 mutation 测试」，且受影响面点名了
`tests/test_precip_overlay.py` 与 `tests/test_openapi_drift.py` 两个文件。
一次性 receipt 贴进 PR body 就随 PR 消失，而 selector 边的**理由**（这些 suite 确实会咬）
需要和边一起长期存在。

隔离性靠 `create_app()` 每次新建 FastAPI 实例 + `monkeypatch` 自动还原：
- router mutation 改的是 `route_registry._BUSINESS_ROUTERS` 模块属性，
  `register_role_aware_routes` 每次调用时读取，因此只影响该测试内新建的 app。
- OpenAPI mutation 改的是 `main._patch_precip_openapi` 模块属性，
  `_patch_openapi_schema` 按模块级名字查找。同文件
  `test_openapi_patch_owner_module_preserves_main_monkeypatch_facade` 已证明该 idiom 有效。
  `FastAPI.__init__` 已把 `openapi_schema` 置 `None` 且 `create_app()` 不调 `.openapi()`，
  所以新建 app 无需清缓存；显式置 `None` 作为防御性写法可以保留，但它不是正确性前提。

两条 mutation 都**不触碰**生产模块的常量本体，测试结束后自动还原。

## 不取的方案

- **import/runtime 自动依赖推导**（issue 的备选）：覆盖面更广，但规则不可解释、
  duplicate-pattern 与 stop-on-match 语义难以维持，且会把 CI 选择集推向不可预测的规模。
  issue 自己已判定它不适合作为最小可合并切片。
- **给 `apps/api/**` 广义规则加降水 suite**：会让 `apps/api/` 下每个文件都买 4 个降水 suite，
  成本与精确性双输，且与 #2079 / #1704 建立的「广义规则只买三个通用 suite」结构冲突。
