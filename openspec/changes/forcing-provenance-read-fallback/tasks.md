# Tasks: forcing-provenance-read-fallback

## 1. 实现

- [x] 1.1 `file_orchestration_journal.py` `candidate_state`：`rows.forcing_version is None` 时复用 `_forcing_context` 底层直读档（原始 mapping 层），浅拷贝物化 + `forcing_version_source="direct"`；journal 命中标 `"journal"`；两档空维持 `None`；调用点捕 `FileOrchestrationJournalError` 退化 None（不崩 pass）；`find_forcing_context` 零改动（D1）
- [x] 1.2 `scheduler_state_failure.py`：新 helper `_forcing_sidecar_provenance`（候选身份推导 key、限量读上限按生产 record 实况定（round-2：16 MiB）、六类不可用细因（含 `sidecar_oversized`）、绝不 fail-open/抛逃逸）；空 URI 分支三段化——sidecar 命中以 **manifest 文件 key** 进既有探针（目录形 package_uri 绝不直接进探针；真缺 → `missing_forcing_package_uri` 原语义），三档全空 → `forcing_version_row_absent`/`FORCING_VERSION_ROW_ABSENT`（reason+error_code+stable_classifier 三重）；guard 返回值扩 provenance annotation，最终决策 evidence 并入 `forcing_provenance`（D2）
- [x] 1.3 `scheduler_candidates.py`：新 token 对 `blocked_forcing_version_row_absent`↔`forcing_version_row_absent`；扩集**四处**（`:1412/:1443/:1519` reason + `:1452` stable_classifier 集合）+ `:1432` 兜底透传；`:728-754` echo 按底层 reason 回显；消费方逐点审计入 PR body（D3）
- [x] 1.4 sidecar key 与 record 播种与 producer 写侧同构（复用/断言字面一致，防漂移；record 含目录形 package_uri + `lineage_json.forcing_package_manifest_uri`）；**探针 key 由本候选 sidecar key 目录 + producer manifest 文件名派生**（`_sidecar_manifest_probe_key`，round-1 V2-C2；record 内 manifest uri 仅 evidence），默认文件名 `forcing_package.json`（R1）；source segment 大小写以写侧为准（D2/基线）
- [x] 1.5 兄弟副本 D4 裁定复述入 PR body（chain_forecast_state/chain_analysis 保留合成 fallback，理由）
- [x] 1.6 `docs/runbooks/current-production-ops.md` exact-cycle repair 前置条件扩双 reason/双 classifier + `tier_status` 分流表 + evidence 判读清单补 provenance 字段（D3 文档消费面，round-1 V3-C1 随修）

## 1R2. Round-2 修复（V5）

- [x] 1R2.1 读上限按生产实况：`_FORCING_SIDECAR_MAX_BYTES` 64 KiB → 16 MiB；`store.size()` 预判超限 → 新细因 `sidecar_oversized`（与 `sidecar_unreadable` 分立）；size/read 的 `ObjectStoreError` 仍不得逃逸（V5-C1）
- [x] 1R2.2 探针错语义归位：`ObjectStoreError` containment 分支从 `missing_forcing_package_uri` 改为 `forcing_version_row_absent` + `forcing_provenance={source:"absent", tier_status:"sidecar_manifest_probe_error"}`（V5-C2）
- [x] 1R2.3 runbook：分流表补 `sidecar_oversized`（重产无效——record 异常，先查 producer）与 `sidecar_manifest_probe_error`（重产无效——存储读故障）；evidence 判读清单补 `forcing_provenance.probe_key`/`.artifact_exists`/`artifact_guard.unsafe_reason`；复原 `:1503` 被截断的孤儿从句主干（V5-C3）
- [x] 1R2.4 测试：B4 `oversized` case 断言迁 `sidecar_oversized` 且播种超 16 MiB；B11 断言迁 row_absent/probe_error；新增 B12 生产量级 record（>1 MB lineage）恢复钉（V5-C1/C2）

## 2. 测试

- [x] 2.1 B1：缺行 + producer 同构 sidecar + manifest 在场 → 非 blocked、evidence `forcing_provenance.source="object_store_sidecar"`（AC-1）
- [x] 2.2 B2：同构 sidecar 在场 + manifest 缺失 → `missing_forcing_package_uri` + source=object_store_sidecar（AC-2 反向）
- [x] 2.3 B3：sidecar 缺失 → `forcing_version_row_absent` 三重断言 + 结构 repair-eligible
- [x] 2.4 B4：畸形/超限/root 未配置/basin_version None → 档位不可用细因，无异常逃逸；目录形 package_uri 不进探针钉
- [x] 2.5 B5：repair 授权双 reason 接受 + token 配对 + `_decision_is_stable_missing_forcing_blocker(row_absent) is True`
- [x] 2.6 B6：空 provenance 几何 pins 全名单迁移（design D5 名单 13 处含 `test_gateway_reconcile.py:1148`），仅换 reason/error_code/classifier，block 方向断言逐字保留；名单外零改动；新发现同类先补名单
- [x] 2.7 B7：candidate_state/find_forcing_context 一致性 + 两档全空 None + 损坏 direct 文件不抛退 None
- [x] 2.8 B8：`forcing_provenance.source` 四值现形（object_store_sidecar 必须在 B1 不-block 几何出现）
- [x] 2.9 B9：真实 sanitized 形状端到端（占位符 + sidecar 可恢复 → 不 block；sidecar 缺失 → row_absent）
- [x] 2.10 B10a/B10b：prefix 漂移仍恢复；异体 manifest witness 不 fail-open
- [x] 2.11 B11：探针 `ObjectStoreError` 护栏 + `run_once` 存活 + row_absent/probe_error 归类
- [x] 2.12 B12：生产量级 record（>1 MB lineage）仍见证恢复（旧 64 KiB 上限下红）
- [x] 2.13 B13：`sidecar_unreadable` 档回归锚（chmod 0 / symlink 几何）——round-3 覆盖缺口，移除档位读腿 containment 后须转红

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_production_scheduler.py tests/test_gateway_reconcile.py` 全绿
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate forcing-provenance-read-fallback --strict --no-interactive`
- [ ] 3.4 node-22 只读几何 receipt（journal null + sidecar 在场 + manifest 文件在场，D6；不制造失败）
- [ ] 3.5 PR body：D3 消费方审计表（含 runbook + open change `fix-node22-scheduler-business-concurrency` 冲突裁定）+ D4 裁定 + D6 live-drill 偏离记录 + issue 引文失实更正（six-basin change 不存在、#874 无 inline pin）+ B6 逐条迁移 diff 清单
