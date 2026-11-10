# Tasks: precip-warning-forcing-usable (#3112)

Fixture level: compact
Must preserve：
- 各 converter 函数产出的 `quality_flag` 与 anomaly 逐字节不变；
- `#3090` / `#3109` 现有测试保持原断言；
- `error_precip_accumulation` 仍判不可用；
- 无 checksum 的产品仍不可用；
- `rejected_quality_flags` 的语义不变：只统计不可用标记；
- `_canonical_readiness_error_message`（converter.py:666）的键列表不变；
- producer 的两处调用点（`producer.py:1344`、`:3715`）不改代码，只随集合变化放行；`warn` 产品进 forcing 的既有行为及其测试（`tests/test_forcing_producer.py:4990`）不变。

## Risk packs
- Error handling / partial outputs — selected：质量门放宽的边界由 1.3 的 error 用例钉住。
- Legacy compatibility — selected：flag 产出不变，由现有 `test_ifs_canonical.py` 原断言保证。
- Schema / columns / field names — not selected：readiness evidence 只新增 `accepted_warning_quality_flags` 键（会经 `redact_payload` 写进 pipeline event）；grep 未发现对 readiness evidence 做整字典相等断言的地方；错误消息键列表不变；canonical flag 只进 manifest/lineage，不进 `met.forcing_quality_flag` 枚举（与 `warn` 同路）。
- 其余 pack — not selected：无入口、配置、IO 或并发变化。

## 1. 实现
- [x] 1.1 `FORCING_USABLE_CANONICAL_QUALITY_FLAGS` 加入 `warning_negative_precip`；就近加中文注释，说明 owner 决定（#3112）以及只有 `error_precip_accumulation` 阻断
- [x] 1.2 `evaluate_canonical_readiness` evidence 新增 `accepted_warning_quality_flags`：统计 `usable_rows` 中 `quality_flag != "ok"` 的行（即可用且有 checksum），按 flag 计数，含 `warn` 和 `warning_negative_precip`，无此类行时为 `{}`。无 checksum 的 warn/warning 行不计入此处，只计入 `checksum_missing_row_count`
- [x] 1.3 测试：
  - `canonical_product_is_forcing_usable`：`warning_negative_precip` 加 checksum 为真；`error_precip_accumulation` 为假；`warning_negative_precip` 无 checksum 为假
  - `evaluate_canonical_readiness`（传入 `forecast_hours`，同生产调用）：某降水时次为 `warning_negative_precip` 的完整 cycle 判 ready，evidence 中 `accepted_warning_quality_flags == {"warning_negative_precip": 1}`
  - 同一 cycle 把该时次改为 `error_precip_accumulation` 后判 not ready，reason 为 `missing_canonical_leads`，`rejected_quality_flags` 含该 flag
  - 同一 cycle 该时次为 `warning_negative_precip` 但 checksum 为空：`ready is False`，`accepted_warning_quality_flags == {}`，`checksum_missing_row_count == 1`
  - producer 级（`tests/test_forcing_producer.py`，仿 `test_warn_precipitation_or_radiation_products_enter_forcing_with_quality_evidence`）：IFS 降水某时次为 `warning_negative_precip` 时，`result.status == "forcing_ready"`，`manifest["quality_flags"]["canonical_products"]` 与 lineage 含 `warning_negative_precip`；改为 `error_precip_accumulation` 时：`pytest.raises(ForcingProductionError, match="prcp_rate_or_amount")`，`repository.forcing_versions == {}`，`repository.timeseries == []`，`repository.cycle_updates[-1]["status"] == "failed_forcing"`（仿 `test_missing_canonical_product_blocks_generation`，`tests/test_forcing_producer.py:2543`）；且 fixture 中不得有其它 cycle 的 fallback 产品补上该时次（`producer.py:3715` 的 fallback 路径同样过滤）

## 2. 上线（合并后）
- [ ] 2.1 **pull 之前**，用 `uv run --no-sync` 在 node-22 旧代码上只读列出 catalog 中含 `warning_negative_precip` 且仍为 `canonical_incomplete` 的 cycle，写进上线记录。必须在 pull 之前做：pull 之后下一个自然 pass 就会追溯放行这些 cycle 并触发补跑
- [ ] 2.2 node-22 先查 `git status --porcelain`，再 `git pull --ff-only`（禁止 `uv sync`、裸 `uv run`）
- [ ] 2.3 对历史 catalog 副本做只读 readiness 评估：把一个 f012 改成 `warning_negative_precip` 后应判 ready，`accepted_warning_quality_flags == {"warning_negative_precip": 1}`，`rejected_quality_flags == {}`；结果贴到 #3112

## Evidence Floor
- `uv run pytest -q tests/test_canonical_converter.py tests/test_ifs_canonical.py tests/test_forcing_producer.py`，加上 selector 选出的测试集
- `uv run ruff check .`
