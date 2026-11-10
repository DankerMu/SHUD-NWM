# Proposal: 降水 warning_negative_precip 视为 forcing 可用（方案 B，#3112）

Issue type: feature
Fixture level: compact
Upstream suggested level: absent (compact: 只改一个可用集合常量 + readiness evidence 增一个计数字段；无 schema / 文件格式 / 入口变化)
Blast radius: 若误把 `error_precip_accumulation` 也放行，连续负值的坏降水会进 forcing；若集合改漏，IFS 单时次 warning 仍会阻断整条 cycle
Selected risk packs: Error handling / partial outputs（质量门放宽的边界）、Legacy compatibility（flag 产出逐字节不变、既有测试原断言）
Evidence floor: `uv run pytest -q tests/test_canonical_converter.py tests/test_ifs_canonical.py tests/test_forcing_producer.py` + selector 选集 + `uv run ruff check .`
design.md 省略（compact）。

## Why

#3109 只修了容差边界。IFS 降水 delta 一旦真正超出噪声界（如 −3 步），该时次就被标成 `warning_negative_precip`。它不在 `FORCING_USABLE_CANONICAL_QUALITY_FLAGS = {"ok", "warn"}` 中，`evaluate_canonical_readiness` 把它当缺失时次（`missing_canonical_leads`），整条 IFS cycle 的 forcing 被阻断，只能人工恢复（2026102300 卡了约 7 小时）。

取值本身已按 `max(0, delta)` 截为 0；converter 注释把 warning 定位为诊断信息。Owner 2026-11-10 决定采用方案 B。

## What Changes

- `workers/canonical_converter/converter.py`：`FORCING_USABLE_CANONICAL_QUALITY_FLAGS` 加入 `warning_negative_precip`。`error_precip_accumulation`（连续 ≥3 次）以及其它非 ok/warn 标记仍不可用。
- `evaluate_canonical_readiness` 的 evidence 新增 `accepted_warning_quality_flags: {flag: count}`，统计被接受的非 `ok` 标记（`warn` 与 `warning_negative_precip`），让放行可审计、不静默。
- flag 产出逻辑（anomaly、lineage、`quality_flag` 取值）不变。

## Impact

- readiness 主路径（`scheduler_adapters`、`scheduler_file_providers`）经由 `evaluate_canonical_readiness` → `canonical_product_is_forcing_usable` 读这个集合。
- forcing producer 也直接调用 `canonical_product_is_forcing_usable`：`workers/forcing_producer/producer.py:1344`（`_load_canonical_products`）、`:3715`（`_fallback_products_by_required_variable`）。`file_store.list_canonical_products`（`file_store.py:136`）不做质量过滤，所以 node-22 dbfree 主路径上真正挡住 warning 产品的是 producer 这两处。改集合后，它们与 readiness 同步放行。
- 追溯生效：catalog 里因 `warning_negative_precip` 卡在 `canonical_incomplete` 的历史 cycle，下一个 pass 就会变成 ready，可能触发自动补跑。上线前在 node-22 只读列出这类 cycle，写进上线记录（task 2.1，须在 pull 之前）。
- 不在范围（只报告、不修改），以下 DB 路径都只收 `quality_flag = 'ok'`，本来就与 readiness 不一致（连 `warn` 都排除）；node-22 不连 DB，不走这些路径：
  - `workers/forcing_producer/store.py:224`；
  - `services/orchestrator/chain_repository.py:259`（`list_canonical_ready_cycles`，即 `chain_forecast_trigger` 的上游过滤）。
