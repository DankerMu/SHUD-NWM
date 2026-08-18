# Proposal: basins-refusal-cause-details (#1432)

## Why

Issue #1432（PR #1429 terminal-state 复审 B2 defer 路由，pre-existing）：basins registry
的 import 与 package 两条硬拒通道丢拒因——

- `workers/model_registry/basins_registry_import.py:218-224`：抛
  `BASINS_REGISTRY_MODEL_NOT_IMPORTABLE` 没传 `details=`（error 类**支持**，能带没带）。
- `workers/model_registry/basins_package.py:538-545`：抛 `BASINS_MODEL_NOT_PUBLISHABLE`，
  但 `BasinsPackageError.__init__`（`:57-72`）**不接受** `details`（想带也带不了）。
- reingest 透传三条腿均无辜：discovery `:94-102`/package `:117-125`（`getattr`）、import `:154-161`（直取 `error.details`），上游修好自动生效。
- 真实踩坑在案：`docs/runbooks/receipts/reach-geom-ingest-20260620.json:181-187`
  （tailanhe）payload 只有泛化文案，runbook `:94` 运维只能写猜测。
- 对照组：`scripts/publish_scheduler_file_registry.py:703-712/:834-844/:966-976` 同语义已带
  `{status, missing_required_files, invalid_required_files}`——两通道口径分叉。

纯 observability：不改任何门控判据。

## What Changes

- `BasinsPackageError` 加 `details: dict[str, Any] | None = None` 形参 +
  `to_payload()` 合并（与 `BasinsRegistryImportError:42-64` / `BasinsReingestError`
  完全同形）。
- 两个 raise 点传拒因 details，键集为**四键**：
  `{"status", "missing_required_files", "invalid_required_files",
  "unreadable_required_files"}`——前三键口径与
  `publish_scheduler_file_registry.py:708-710`（键名真值点）一致（不新造别名）；第四键是 **#1553 口径
  耦合的显式裁决**：PR #1552 已给 discovery payload 加了 `unreadable_required_files`
  且 #1553 明写「后落地者把 key 集合升到三键（cause 键）」，本单落地在后，故一并携带
  （取值 `model.get("unreadable_required_files") or []`，publish 脚本侧的补齐仍归
  #1553，本单不动那三处）。
- 既有 payload 键（`error_code`/`message`/`model_id`/`version`/`path`/`basin_slug`）
  一个不删，纯增量。

## Non-Goals

- 门控判据（哪些 model 被拒）零变化；`status=partial`/`missing=[]` aggregate 语义不变。
- `basins_reingest.py` 透传逻辑不改（自动受益）。
- 同 error 类其他 raise 点（`basins_package.py:521/529/547` 的
  `BASINS_INVENTORY_INVALID`/`BASINS_MODEL_ID_DUPLICATE`/`BASINS_MODEL_NOT_FOUND`）：
  issue 建议只治两条主通道——那些拒因不是 model 健康度（inventory 级/查找级），
  cause 键集对它们无意义，不扩。
- `scripts/publish_scheduler_file_registry.py` 三处与 receipt schema/`_SKIP_CAUSE_LIST_KEYS`
  （#1553 范围）。
- IC 头部形状门本体（#1197/#1429/#1430 已治）。

## Risk triage

- Fixture level: compact（同形 error 类三件套已有两份实现可照抄；纯增量 payload）。
- Repair intensity: low-medium（observability 面，零判据变化）。
- Risk packs: test-evidence selected（payload 增量断言 + 向后兼容锁 + 透传链）；
  spec-compliance selected（键名口径单一来源）；其余 not selected（无版本分岔/无
  path-safety/无 DB）。

## Must preserve

- 两通道的拒收判据、error_code、message 文案逐字不变。
- 既有 payload 键与取值不变（receipt 消费方向后兼容——新键只增）。
- `BasinsPackageError` 既有调用点（不传 details 的）零改动仍合法（默认 None →
  payload 不加键或加空？**裁定：与 `BasinsRegistryImportError` 同形——details 为
  None 时不合并任何键**，既有 raise 点 payload 逐字节不变）。
- reingest 透传：`details=getattr(error, "details", None)` 语义不变。

## Evidence mapping

- 验收 1-4 → tasks 1.1-1.3 + 2.1（payload 断言 + 口径一致锁；import 通道现无任何 `BASINS_REGISTRY_MODEL_NOT_IMPORTABLE` 断言，需从零建 harness——`tests/test_basins_registry_import.py` 已有真 discovery 驱动可复用）；验收 5 → 2.2
  （IC 头部畸形 → 拒因见 `*.cfg.ic` 文件名，**新建 sibling 测试**——既有
  `:1030-1062` 用例是 missing 几何、无 IC 旋钮，不得改造）；验收 6 → 2.3
  （reingest 透传链）；验收 7 → 2.4（旧键完整锁）。
- Verification：`uv run pytest -q tests/test_basins_package_publication.py
  tests/test_basins_registry_import.py tests/test_basins_reingest.py` + ruff（本地）；
  merge 后 node-27 receipt。
