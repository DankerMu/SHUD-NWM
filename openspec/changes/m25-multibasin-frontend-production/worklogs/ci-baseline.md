# M25 CI no-regression 基线（master b9d0da2）

## 背景（2026-06-07 决策记录）
CI `unit-test` job 跑全量 `pytest tests/`（命令自始如此，非 M25/CI-重设引入），master
本身预存 **18 个失败**——均为 e2e/网络/grib/.venv-PATH/真实数据/既有 SQL-漂移测试，
按项目 oracle 路由本应在 node-22 真实环境跑、不该纯 CI 门控。node-22 当前正跑业务化
生产（Slurm 6307/6310 + 连续 daemon），按硬纪律不可在其上 git pull/checkout 验证。

**决策（用户授权、导向业务化）**：M25 PR 合并门 = ① 评审 clean ② 可绿 job
（OpenAPI Validate / SQL Migration Dry Run / Frontend Build）绿 ③ unit-test 失败集 ⊆
本基线（**无新增**）。逐 PR 比对失败集，新增即 block。预存红另建 issue 追踪，不在 M25 PR 内修。

## master 基线失败集（18，verified 部分本地在 b9d0da2 同样 fail）
- tests/test_e2e.py::test_m1_forecast_cycle_data_flow_and_api_response  (network download)
- tests/test_e2e.py::test_m2_analysis_warm_start_spliced_curve_and_selection_e2e  (network)
- tests/test_e2e_ifs.py::test_ifs_adapter_canonical_forcing_run_parse_e2e  (cfgrib grib fixture)
- tests/test_e2e_ifs.py::test_ifs_06z_144h_manifest_context_and_forcing_limit  (cfgrib)
- tests/test_forcing_producer.py::test_warn_precipitation_or_radiation_products_do_not_enter_ok_forcing
- tests/test_migrations.py::test_qhh_latest_display_product_migration_matches_candidate_and_window_queries  (SQL 串漂移, 本地 fail)
- tests/test_orchestration_chain.py::test_template_export_lines_omits_grib_env_when_unset  (.venv PATH, 仅 CI fail)
- tests/test_production_met_validation.py::test_validate_met_default_lane_writes_required_evidence_and_redacts
- tests/test_production_met_validation.py::test_validate_met_manifest_bound_counts_actual_deterministic_sources
- tests/test_production_met_validation.py::test_validate_met_same_run_requires_force_and_force_replaces_bundle
- tests/test_production_met_validation.py::test_validate_met_disabled_sources_record_skipped_without_success
- tests/test_production_met_validation.py::test_validate_met_cached_only_policy_uses_cached_fixture
- tests/test_production_met_validation.py::test_argparse_validate_met_fallback  (cfgrib, 本地 fail)
- tests/test_production_scheduler.py::test_non_ok_canonical_readiness_blocks_forcing_candidate_submission
- tests/test_real_slurm_gateway.py::test_safe_slurm_env_reaches_rendered_non_array_template_and_secret_is_rejected  (.venv PATH)
- tests/test_scheduler_backfill.py::test_backfill_budget_cap_defers_excess_gaps  (本地 fail)
- tests/test_scheduler_backfill.py::test_backfill_without_completion_provider_treats_all_as_gap
- tests/test_scheduler_backfill.py::test_backfill_seven_day_window_spans_multiple_days

合计 18。#310（PR#319）CI unit-test = 18 failed，全部 ⊆ 基线 → **0 新增回归 → 可合并**。

## 基线漂移 18 → 20（2026-06-07，#321 归因）
非 M25 commit `d2b8258`「Run published tile stage on control node」给基线新增 2 个
publish 测试失败（业务逻辑漂移，**非 M25 改动**）：
- tests/test_orchestration_chain.py::test_direct_submit_success_with_immediate_terminal_publish_root_advertises_readable_uri
- tests/test_pipeline_logs_artifacts.py::test_compute_pipeline_emits_published_log_uri_and_writes_published_log

故 master 实际预存失败 = **20**。

## #321 A 层 PR 后：预期 CI 残留 = 8
A 层（本 PR）动作：
1. 给 e2e/grib 环境耦合测试打 marker，纯 CI 用 `-m "not e2e and not grib and not integration"`
   排除（10 个：test_e2e ×2、test_e2e_ifs ×2、test_production_met_validation ×6），改 node-22 跑。
2. 修 2 个 .venv-PATH 纯测试 bug（环境无关化，用 `NHMS_PYTHON_VENV_BIN` 显式指定 tmp venv-bin）：
   - test_orchestration_chain::test_template_export_lines_omits_grib_env_when_unset
   - test_real_slurm_gateway::test_safe_slurm_env_reaches_rendered_non_array_template_and_secret_is_rejected

20 − 10（marker 排除）− 2（venv-path 修复）= **8** 残留真实漂移，交 #321 B 层子 issue：
- test_forcing_producer::test_warn_precipitation_or_radiation_products_do_not_enter_ok_forcing ×1
- test_migrations::test_qhh_latest_display_product_migration_matches_candidate_and_window_queries ×1（SQL 串漂移）
- test_production_scheduler::test_non_ok_canonical_readiness_blocks_forcing_candidate_submission ×1
- test_scheduler_backfill ×3（budget_cap / without_completion_provider / seven_day_window）
- d2b8258 publish 漂移 ×2（test_direct_submit…publish_root / test_compute_pipeline…published_log）

A 层只做 env 路由 + 纯测试 bug 修，**不碰任何 B 层业务逻辑**（scheduler/migration/forcing/publish）。
