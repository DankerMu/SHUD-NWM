## 1. Implementation

- [x] 1.1 `.github/workflows/ci.yml` backend filter 追加 `- 'scripts/**/*.sh'`
- [x] 1.2 `select_ci_tests.py` PATH_TEST_RULES 补 12 个 wrapper→守卫映射
      （scheduler_file_provider_refresh_once / install_node22_scheduler_file_provider_refresh /
      node27_autopipe_cron / node27_db_export_salvage_once / node27_download_once /
      node27_product_archive_once / node27_storage_inventory_audit_once /
      node27_timeseries_compression_once / node27_timeseries_retention_once /
      run_qhh_backend_smoke / run_qhh_cycle / local_pg），映射目标 =
      实际引用该脚本的 test 文件（以 grep tests/ 为准，不臆测）
- [x] 1.3 未匹配映射的 `scripts/**/*.sh` 纳入 CORE_SMOKE_TESTS 兜底
      （泛化 `_is_backend_python_path` 或单开 shell 判定）

## 2. Tests（三场景 + 红证）

- [x] 2.1 sh-only 改动集：`['scripts/scheduler_file_provider_refresh_once.sh']`
      → 选集含 `tests/test_scheduler_file_provider_refresh.py`（当前 master 为 []，红证）
- [x] 2.2 sh+docs 改动集 → 同上，docs 不注水
- [x] 2.3 sh+py 混合改动集 → 并集含双方守卫
- [x] 2.4 未知新 `.sh`（无映射）→ 选集 ⊇ CORE_SMOKE_TESTS 且非空
- [x] 2.5 既有 py 场景回归：`uv run pytest -q tests/test_select_ci_tests.py` 全绿

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py`
- [x] 3.2 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`（macOS bash3.2 允许 skip）
- [x] 3.3 `uv run ruff check .`
- [x] 3.4 `openspec validate ci-shell-guard-gating --strict --no-interactive`
- [x] 3.5 门控实机证据：本 PR 的 Actions run 中 `Detect changed areas` backend=true 且
      Unit Tests 执行了新增单测（PR 含 .py 改动天然成立；sh-only 实机证据在 issue 验收第 5 条，
      以后续任一 sh-only PR 兑现，记为 known-limit）
