## Why

Issue #1382：#1370 之后治理 receipt 顶层不得再有 `archive_root` 块（ADR 0002 Rev
负空间契约），但现有唯一守卫 `test_governance_config_and_receipt_carry_no_archive_root`
只做属性级反向断言（函数/字段不存在），从不构造 receipt——换名收集器或通用
collector 循环重新注入该键时测试仍全绿。

## What Changes

- `tests/test_node27_resource_governance.py`：扩写该测试——保留属性级断言，
  monkeypatch 打桩三个收集器后真实调用 `build_receipt()`，正向断言
  `"archive_root" not in receipt`；测试名与覆盖面对齐。
- 不改 `scripts/node27_resource_governance.py` 任何生产代码。

## Non-Goals

- 为该 receipt 新建 JSON Schema（issue 备选，规模不符）。
- ADR 0002 文档的陈旧表述（issue 显式另案）。

## Risk triage

- Fixture level: none（test-only，产物级正向 pin；无运行时行为改动）。
- Repair intensity: low。
- Risk packs: test-evidence selected（本 issue 即补 pin）；其余全部 not selected
  （无 IO/权限/DB/发布行为改动）。

## Must preserve

- 现有属性级断言不削弱；13 条既有测试全绿。
