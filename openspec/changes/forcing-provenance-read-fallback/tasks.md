# Tasks: forcing-provenance-read-fallback

## 1. 实现

- [ ] 1.1 `file_orchestration_journal.py` `candidate_state`：`rows.forcing_version is None` 时复用 `_forcing_context` 底层直读档（原始 mapping 层），浅拷贝物化 + `forcing_version_source="direct"`；journal 命中标 `"journal"`；两档空维持 `None`；调用点捕 `FileOrchestrationJournalError` 退化 None（不崩 pass）；`find_forcing_context` 零改动（D1）
- [ ] 1.2 `scheduler_state_failure.py`：新 helper `_forcing_sidecar_provenance`（候选身份推导 key、`read_bytes_limited` ≤64 KiB、五类不可用细因、绝不 fail-open/抛逃逸）；空 URI 分支三段化——sidecar 命中以 **manifest 文件 key** 进既有探针（目录形 package_uri 绝不直接进探针；真缺 → `missing_forcing_package_uri` 原语义），三档全空 → `forcing_version_row_absent`/`FORCING_VERSION_ROW_ABSENT`（reason+error_code+stable_classifier 三重）；guard 返回值扩 provenance annotation，最终决策 evidence 并入 `forcing_provenance`（D2）
- [ ] 1.3 `scheduler_candidates.py`：新 token 对 `blocked_forcing_version_row_absent`↔`forcing_version_row_absent`；扩集**四处**（`:1412/:1443/:1519` reason + `:1452` stable_classifier 集合）+ `:1432` 兜底透传；`:728-754` echo 按底层 reason 回显；消费方逐点审计入 PR body（D3）
- [ ] 1.4 sidecar key 与 record 播种与 producer 写侧同构（复用/断言字面一致，防漂移；record 含目录形 package_uri + `lineage_json.forcing_package_manifest_uri`）；manifest 派生兜底复用 `_package_manifest_uri`/`package_manifest_filename`（默认 `forcing_package.json`，R1）；source segment 大小写以写侧为准（D2/基线）
- [ ] 1.5 兄弟副本 D4 裁定复述入 PR body（chain_forecast_state/chain_analysis 保留合成 fallback，理由）
- [ ] 1.6 `docs/runbooks/current-production-ops.md:1480-1481` exact-cycle repair 前置条件扩双 reason/双 classifier（D3 文档消费面）

## 2. 测试

- [ ] 2.1 B1：缺行 + producer 同构 sidecar + manifest 在场 → 非 blocked、evidence `forcing_provenance.source="object_store_sidecar"`（AC-1）
- [ ] 2.2 B2：同构 sidecar 在场 + manifest 缺失 → `missing_forcing_package_uri` + source=object_store_sidecar（AC-2 反向）
- [ ] 2.3 B3：sidecar 缺失 → `forcing_version_row_absent` 三重断言 + 结构 repair-eligible
- [ ] 2.4 B4：畸形/超限/root 未配置/basin_version None → 档位不可用细因，无异常逃逸；目录形 package_uri 不进探针钉
- [ ] 2.5 B5：repair 授权双 reason 接受 + token 配对 + `_decision_is_stable_missing_forcing_blocker(row_absent) is True`
- [ ] 2.6 B6：空 provenance 几何 pins 全名单迁移（design D5 名单 13 处含 `test_gateway_reconcile.py:1148`），仅换 reason/error_code/classifier，block 方向断言逐字保留；名单外零改动；新发现同类先补名单
- [ ] 2.7 B7：candidate_state/find_forcing_context 一致性 + 两档全空 None + 损坏 direct 文件不抛退 None
- [ ] 2.8 B8：`forcing_provenance.source` 四值现形（object_store_sidecar 必须在 B1 不-block 几何出现）

## 3. Evidence Floor

- [ ] 3.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_production_scheduler.py tests/test_gateway_reconcile.py` 全绿
- [ ] 3.2 `uv run ruff check .`
- [ ] 3.3 `openspec validate forcing-provenance-read-fallback --strict --no-interactive`
- [ ] 3.4 node-22 只读几何 receipt（journal null + sidecar 在场 + manifest 文件在场，D6；不制造失败）
- [ ] 3.5 PR body：D3 消费方审计表（含 runbook + open change `fix-node22-scheduler-business-concurrency` 冲突裁定）+ D4 裁定 + D6 live-drill 偏离记录 + issue 引文失实更正（six-basin change 不存在、#874 无 inline pin）+ B6 逐条迁移 diff 清单
