## Risk packs

- Public API / CLI / script entry — **selected**：全部 importer（`apps/`、`services/`、`workers/`、`scripts/`、`tests/`）经 facade 解析 → EF5。
- Legacy compatibility — **selected**：类属性 patch 65 处/14 名 + `from_env` 字符串 patch ×23（共 15 名）、冻结消费者、源码钉 → EF2、EF3、EF9。
- Concurrency / shared state / ordering — **selected**：每个方法内调用顺序不变（EF1 指纹）；`_transaction` 留 facade；node-27 EF6 含 `tests/test_variant_activation_cutover.py` 的并发激活用例。
- CI routing — **selected**：owner 模块改动必须触发 real-DB lane（D4）→ EF8。
- Schema / Config / Auth / Resource / Error handling / Release / File IO — not selected：零行为改动，方法源码逐字搬运（EF1）。
- Documentation — not selected：若 `docs/**` 以 `model_registry.py::Class.method` 指向搬走的方法，同步指针（零语义）。

## 0. Baselines（`.workplans/split-model-registry-store/`）

- [ ] 0.1 AST 指纹：顶层定义 + `PsycopgModelRegistryStore` 每个方法（按方法名取键，与 EF1 一致）
- [ ] 0.2 facade 命名空间 `vars(module)`；`PsycopgModelRegistryStore` 的 `dir()`、每个方法源码 sha、`dataclasses.fields()`、repr/eq/hash 行为（`__qualname__`/`__module__` 为 declared drift，不作 oracle）
- [ ] 0.3 seam 清单：类属性（14 名 / 65 处）+ `from_env` 字符串 ×23 = 15 名、模块级（实测 0，附命令）、字符串
- [ ] 0.4 源码钉清单：每个按路径/`getsource` 读本模块的断言 → 钉住的字面量 → 所在方法

## 1. 拆分

- [ ] 1.1 facade + owner/mixin 模块，每个 < 1000 行
- [ ] 1.2 facade 保留 dataclass 装饰器/字段/`__post_init__`/`from_env`/`_transaction`/`_attribution_connect_kwargs`/`_PsycopgTransaction`；mixin 为普通类；导入单向；新模块命名 `model_registry_*.py`；`list_basins`/`list_basin_versions`/`_lock_basin_version_scope` 所在模块 ≤ 900 行
- [ ] 1.3 selector 等价路由；ci.yml `database:` 保留字面量 + 新增 `packages/common/model_registry_*.py`；`INTEGRATION_TRIGGER_SOURCES` 同步；partition oracle 不重生成
- [ ] 1.3b `tests/test_real_basin_discovery_integration.py` 的 getsource 缺席检查扩到 facade + owner 模块；`tests/test_node27_connection_attribution.py` surface-name 扫描参数化加入新模块；write-surface scan 已核无需同步（D3）
- [ ] 1.4 冻结消费者零 diff

## 2. 收口

- [ ] 2.1 `wc -l` 全部 < 1000；`.large-file-guard.json` 零 diff
- [ ] 2.2 `uv run ruff check .`；`openspec validate split-model-registry-store --strict --no-interactive`
- [ ] 2.2b orchestrator 写 ci-contract-baseline MODIFIED delta（:820 场景 + :928 注册表所在 requirement）
- [ ] 2.3 node-27 全量 `pytest -q`（真实 DB，frozen SHA）receipt

## Evidence Floor

1. AST 指纹零漂移，按方法名取键（单类无重名）；类定义 bases 与 `__qualname__`/`__module__` 为 declared drift。
2. 14 个类属性 patch 名 + `from_env`（共 15 名）各做突变（破坏真实调用 → 用例转红）；模块级 patch 实测为 0（n/a，附测量）。
3. 冻结消费者零 diff；源码钉全部原位且相关测试绿。
4. facade `vars()` ⊇ 拆前且同一对象；`PsycopgModelRegistryStore` 的 `dir()` ⊇ 拆前、每个方法源码 sha 相同、无 MRO 覆盖；`dataclasses.fields()`、repr、eq、hash 不变。
5. 所有 importer 可 import；无 cycle。
6. node-27 全量 pytest（真实 DB）与 master 同环境失败集合 diff 为空。
7. ruff + openspec strict 绿；guard hook 对真实提交 rc=0。
8. `select_tests` 与 ci.yml paths-filter 对每个 owner 模块路径触发 real-DB lane（与 facade 同 target 集合）；partition oracle 守卫绿且未重生成。
9. discovery getsource 缺席检查与 connect-surface 扫描覆盖 owner 模块（反例：在 owner 模块注入被禁字面量 → 转红）。
