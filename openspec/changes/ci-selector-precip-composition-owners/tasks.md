# Tasks: ci-selector-precip-composition-owners (#2098)

Fixture level: expanded
Change surface:
- `scripts/select_ci_tests.py`：`apps/api/route_registry.py` 移出 `CONNECTION_ATTRIBUTION_ROUTE_PATHS`，新建独立 path-exact 行（targets = `CONNECTION_ATTRIBUTION_TESTS` + `PRECIP_SURFACE_TESTS`）；`apps/api/main.py` 既有精确行 targets 扩为 `(API_ERROR_LOGGING_TEST, *PRECIP_SURFACE_TESTS)`；两行均无 `stop_on_match` / `only_when_any_changed`；同步改写被证伪的 tuple 注释
- `tests/test_select_ci_tests.py`：共六条——两条 owner 的字面量精确集钉 + 两条反向缺失钉 + 一条 flags 钉 + 一条 attribution 表完整性钉
- `tests/test_precip_overlay.py`：`_BUSINESS_ROUTERS` 去 `precip_router` 的隔离 mutation proof（**路由表成员**，非 404）
- `tests/test_openapi_drift.py`：`_patch_precip_openapi` 变 no-op 的隔离 mutation proof（runtime ≠ static）
Must preserve:
- registry 改后仍选到两个 connection-attribution suites（MERGE 而非 DROP）；main.py 改后仍选到 `tests/test_api_errors_logging.py`；两者仍选到 `apps/api/**` 的三个通用 API suite
- `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 其余 5 个 route 路径与 3 个 store 路径选择结果逐条不变
- `apps/api/routes/precip.py`、`services/precip/cache.py`、`apps/api/openapi_patching.py`、`apps/api/errors.py` 选择结果逐条不变
- duplicate-pattern / stop-on-match / 规则目标存在性三条既有守卫照旧绿
- 生产 `_BUSINESS_ROUTERS` 内容、`_patch_openapi_schema` 调用序列、`openapi/nhms.v1.yaml` 零改动
Must add/change:
- 两条 owner 边 + `#2098` 注释；六条 selector 元测试；两条 mutation proof
Seams under test:
- `select_tests(["apps/api/route_registry.py"], repo_root=Path("."))` 与 `select_tests(["apps/api/main.py"], repo_root=Path("."))` 的精确输出
- 两条 owner 行的两个 flag 均为假
- `route_registry._BUSINESS_ROUTERS` 去 `precip_router` 后两个公开降水路由的 HTTP 状态
- `main._patch_precip_openapi` 变 no-op 后 runtime schema 与静态 YAML 的相等性
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门选择入口；两条精确相等钉 + 反向缺失钉钉住输出
- Legacy compatibility / examples: selected - `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 是共享 tuple，移出 registry 不得影响其余 8 个成员；attribution 表完整性钉覆盖
- Concurrency / shared state: selected - 两条 mutation proof 改模块级属性，必须靠 `monkeypatch` + `create_app()` 保证隔离，不得泄漏到同套其他用例
- Schema / columns / units / field names: not selected - 无数据格式改动；静态 OpenAPI 零改动
- File IO / path safety / overwrite: not selected - 只读规则表
- Other packs: not selected - 无 auth/迁移/发布面
Required evidence:
- 改前基线已实测（`7fc9a43c`）：registry 选 5 个 suite、main.py 选 4 个，均不含 `tests/test_precip_overlay.py` 与 `tests/test_openapi_drift.py`
- 改后逐条实测并贴 PR body（**期望值以实测输出为准，不得以心算并集替代**；预期 registry 8 个、main.py 7 个）：
  ```bash
  printf '%s\n' 'apps/api/route_registry.py' | uv run python scripts/select_ci_tests.py --repo-root .
  printf '%s\n' 'apps/api/main.py' | uv run python scripts/select_ci_tests.py --repo-root .
  printf '%s\n' 'apps/api/routes/precip.py' | uv run python scripts/select_ci_tests.py --repo-root .
  printf '%s\n' 'services/precip/cache.py' | uv run python scripts/select_ci_tests.py --repo-root .
  ```
  后两条为对照组，改前改后必须字节相同
- `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 其余 5 个 route 路径 + 3 个 store 路径 + `apps/api/openapi_patching.py` + `apps/api/errors.py` 共 10 条，改前改后逐行 diff 为空（改前基线用同一循环留档）
- 两条 selector 精确集钉：加规则前红（缺 **3** 个降水 suite——第四个 `tests/test_api_contract.py` 由 `apps/api/**` 供给，改前就在）、加规则后绿，红→绿输出贴 PR body
- 两条反向缺失钉：monkeypatch 去掉该 owner 的 precip targets 后必须红
- 两条 mutation proof 的**构造性证据**：正反两腿都在测试内部，实测该测试绿即证明「未 mutate 时成立、mutate 后不成立」；另需单独实测「把 mutation 腿换成不做 mutation 后该测试变红」，证明反腿非真空，输出贴 PR body
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_precip_overlay.py tests/test_openapi_drift.py` 全绿
- `uv run pytest -q tests/test_api.py tests/test_api_contract.py tests/test_monitoring_api.py tests/test_api_errors_logging.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py tests/test_openapi_31_contract.py` 全绿（两条 owner 改后选择集的**完整并集**减去上一条已列的三个）
- `uv run ruff check .`；`openspec validate ci-selector-precip-composition-owners --strict --no-interactive`
- `git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`tests/test_precip_overlay.py`、`tests/test_openapi_drift.py`、`openspec/**`
Non-goals:
- 不改降水服务/路由功能/静态 OpenAPI；不改生产路由注册与 runtime schema 行为
- 不做全仓 import 依赖自动推导；不给 `apps/api/**` 广义规则加降水 suite
- 不动 `PRECIP_SURFACE_TESTS` 成员与其两处既有用法；不修 `apps/api/openapi_patching.py` 的降水覆盖缺口（报告不修，另立单）
- 不碰 #1903 / #1875

## 1. 选择规则

- [x] 1.1 `scripts/select_ci_tests.py` 把 `apps/api/route_registry.py` 从 `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 移出，新增独立 `PathTestRule("apps/api/route_registry.py", CONNECTION_ATTRIBUTION_TESTS + PRECIP_SURFACE_TESTS)`。**不得**新增同 pattern 的第二条规则（duplicate-pattern 守卫会红）；手法照同文件 #2078 对 `apps/api/routes/forecast.py` 的 MERGE 先例。
- [x] 1.2 同步改写该 tuple 上方的注释。现文有**两处**被证伪，必须一并修：(a)「plus the registry itself」——registry 已移出；(b)「each declares a module-level `_APPLICATION_NAME`」——`apps/api/route_registry.py` 全文无 `_APPLICATION_NAME`，该句对 registry 本就为假，移出后剩余成员才真正满足它。按 #2078 的写法说明 registry 为何 deliberately absent（已有精确行、suites 已 MERGE 进去）。
- [x] 1.3 `apps/api/main.py` 既有精确行 targets 由 `(API_ERROR_LOGGING_TEST,)` 改为 `(API_ERROR_LOGGING_TEST, *PRECIP_SURFACE_TESTS)`，带 `#2098` 注释写明这条边管的是 `_patch_precip_openapi(schema)` 调用点决定 runtime OpenAPI。
- [x] 1.4 两条 owner 行均**不加** `stop_on_match`、**不加** `only_when_any_changed`。两个 flag 当下**行为惰性**：`apps/api/**` 是更早的规则，三个通用 API suite 在循环走到这两条 owner 行（当前是全表最后两条）之前已累加完毕；`only_when_any_changed` 对 `PATH_TEST_RULES` 根本不生效（`_rule_activated` 只在 `CHANGED_TEST_FILE_RULES` 循环被调用，死字段见 #2198）。pin 是**结构性**的：防止日后在其后追加规则时被 `stop_on_match` 遮蔽，也防止误加一个在此静默无效的字段。

## 2. selector 元测试

- [x] 2.1 `apps/api/route_registry.py` 的**字面量精确集**钉：期望集的每个元素写成字面字符串，**不得从 `scripts/select_ci_tests` 读回任何值**——不只是 `PRECIP_SURFACE_TESTS` / `CONNECTION_ATTRIBUTION_TESTS`，也包括 `API_ERROR_LOGGING_TEST` 这类单元素常量和 `PATH_TEST_RULES` 的推导（`next(r.tests for r in PATH_TEST_RULES if r.pattern == ...)` 满足字面、违反意图，同样禁止）。验收标准第 3 条禁的是「生产与期望同步移动」。例外：2.3 的反向缺失钉为构造 mutant 而读 `PATH_TEST_RULES` 是允许的，因为期望值本身仍是字面量。手法照 #2122 的 `test_precip_route_rule_stays_without_the_prewarm_suite`。
- [x] 2.2 `apps/api/main.py` 的字面量精确集钉，同上约束。
- [x] 2.3 两条**反向缺失**钉：monkeypatch 掉对应 owner 行的 precip targets 后，`tests/test_precip_overlay.py`、`tests/test_openapi_drift.py`、`tests/test_openapi_31_contract.py` 三个降水 suite必须从选择集中消失。**只断言这三个**：第四个被路由的 `tests/test_api_contract.py` 同时是 `apps/api/**` 广义规则的 rider，删掉 owner 的 precip targets 后它**依然在**，把四个一起写进断言会直接红。
- [x] 2.4 flags 钉：断言两条 owner 行的 `stop_on_match` 与 `only_when_any_changed` 均为假。二者对这两条路径行为惰性，精确集钉抓不到，spec delta 的「neither flag」SHALL 没有独立钉就是空文。手法照 `test_precip_tree_rule_carries_no_selection_flags`。
- [x] 2.5 attribution 表完整性钉：`CONNECTION_ATTRIBUTION_ROUTE_PATHS` 移出 registry 后，其余 5 个 route 路径的选择结果逐条不变，且 registry 仍选到两个 attribution suites。没有它，「把 registry 从 tuple 删掉却忘了合并 targets」是全绿的。
- [x] 2.6 新测试命名**不得**与既有 `-k` 选择产生歧义；命名确定后实测 `-k` 子串唯一命中预期用例（#2195 round 2 的 P1 就是命名破坏了 `-k` 隔离）。

## 3. mutation proof

- [x] 3.1 `tests/test_precip_overlay.py` 新增隔离 mutation proof，正反两腿写在**同一个测试**里：先 `main.create_app()` 不 mutate，断言其 `.routes` 路径集合**含**两条公开降水路径；再 `monkeypatch.setattr(route_registry, "_BUSINESS_ROUTERS", <去掉 precip_router 的元组>)` 后新建 app，断言其 `.routes` **两条都不含**。**断言路由表成员，不得断言 HTTP 404**：`apps/api/startup_wiring.py` 的 SPA fallback 对任何 `api/` 前缀未匹配路径 `raise HTTPException(404)`，降水路由自身对未镜像 cycle 也返回 404，故 404 在「monkeypatch 未生效」时同样绿，是真空断言。idiom 照同文件 `test_precip_routes_take_no_database_dependency` 的路由表扫描——但注意**该既有用例读的是模块单例 `main.app`，不在本 mutation 射程内**；本测试只看自己新建的 app，**不得**触碰或重建 `main.app`（那会全 session 泄漏）。不得改动生产 `_BUSINESS_ROUTERS` 本体。
- [x] 3.2 `tests/test_openapi_drift.py` 新增隔离 mutation proof，正反两腿同一测试：未 mutate 的 `main.create_app()` schema 在**两处**与 `openapi/nhms.v1.yaml` 一致——`/api/v1/precip/{source}/{cycle}/index` 操作的 response schema，以及**不存在** `PrecipIndexResponse` component；`monkeypatch.setattr(main, "_patch_precip_openapi", <no-op>)` 后新建 app 在**两处都不一致**（`_patch_precip_openapi` 既 pop 掉 `PrecipIndexResponse` 又重写该 operation，而静态 YAML 中 `PrecipIndexResponse` 出现 0 次）。**逐处比较，不做整文档相等**——patch 恰好只动这两处，反腿因此点名 `_patch_precip_openapi` 造成的漂移，不会被无关的整文档差异蒙混过关；整文档相等由同文件既有的 `test_static_openapi_matches_runtime_schema` 覆盖。（注：**不要**写「新建 app 的 runtime-mode env 差异会让整文档腿真空」——实测 `create_app().openapi()` 与静态 YAML 在裸进程与 pytest session 内**都相等**，该理由为假。）同样**不得**触碰 `main.app`。idiom 先例见同文件 `test_openapi_patch_owner_module_preserves_main_monkeypatch_facade`。
- [x] 3.3 实测确认两条 mutation proof 的隔离性：mutation 用例之后，同文件既有用例仍绿（模块属性已还原）。

## 4. 验证

- [x] 4.1 按 Required evidence 逐条产出：四条选择器实测 + 10 条对照 diff + 两条红→绿 + 两条 mutation 双侧证据 + 两组 pytest 全绿 + ruff + openspec validate + diff scope。
