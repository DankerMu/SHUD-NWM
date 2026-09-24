## Why

`packages/common/model_registry.py` 实测 4058 行（master `8768f5d55`），不在 `.large-file-guard.json` exclude 中，整文件 touch 即被 guard 拒绝。批 K3 的 #1729 读侧过滤必须改其中 `PsycopgModelRegistryStore.list_basins` / `list_basin_versions`（SQL 层，路由层过滤破坏分页）。用户裁定：不加豁免、不绕钩子，先做纯物理拆分（#2617）。

## What Changes

- `packages/common/model_registry.py` → facade + 若干 owner 模块（`packages/common/` 下），每个 < 1000 行。
- `PsycopgModelRegistryStore`（`:591-3425`，`@dataclass(frozen=True)`）的方法按职责搬进 mixin 类（每个 mixin 一个模块），`PsycopgModelRegistryStore` 在 facade 中以 `class PsycopgModelRegistryStore(<mixins>)` 组合；方法源码逐字不变。
- 纯物理搬运：零行为 / SQL / 契约变化；`.large-file-guard.json` 零改动。

**BREAKING**：无。

## Impact

- Affected specs: `orchestrator-structural-burndown`（ADDED 1 requirement）；`ci-contract-baseline`（MODIFIED：real-DB 触发注册表加 `packages/common/model_registry_*.py`）。
- Affected code: `packages/common/model_registry.py` + 新 owner 模块；`scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`.github/workflows/ci.yml`（database filter 追加 glob）、`tests/test_real_basin_discovery_integration.py`、`tests/test_node27_connection_attribution.py`（验证面扩到 owner 模块）。
- Out of scope：#1729 / #2491 / #1480（批 K3）；#2612（selector 覆盖缺口）。

## Triage

```text
Issue type: refactor
Fixture level: expanded
Upstream suggested level: absent (expanded: shared entrypoint, 65 class-attribute patch sites / 15 names incl. from_env string patches, source-pinned validators, CI real-DB routing)
Blast radius: vacuous monkeypatch、MRO/dataclass 语义变化、connect owner 登记漂移、源码钉转红或变 vacuous、K3 改 owner 模块时 CI 跳过 real-DB lane
Selected risk packs: Public API / script entry; Legacy compatibility; Concurrency / shared state / ordering（方法内调用顺序、`_transaction` 留 facade）; CI routing（owner 模块触发 real-DB lane）
Evidence floor: 见 tasks.md
```
