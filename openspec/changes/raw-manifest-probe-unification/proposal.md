# Proposal: raw-manifest-probe-unification (#1393)

## Why

`services/orchestrator/scheduler_state_failure.py` 的两条 raw-manifest 修复
腿——`_missing_raw_manifest_repair_evidence`（探针调用 `:1346`）与
`_repaired_raw_manifest_downstream_retry_evidence`（`:1406`）——裸调
`scheduler_state_common._object_manifest_is_missing`，绕过 #1365/PR #1390
在 `_artifact_uri_missing_status` 落地的统一探针层，带来两个缺陷（均
pre-existing，PR #1390 fixture 显式 non-goal、代码注释 `:1027-1028` 写死
豁免）：

1. **root 未配置 fail-open**：`scheduler_state_common.py:164-169` 在
   profile 与 `OBJECT_STORE_ROOT` 皆空时 `return False`（＝「存在」）。
   downstream 腿 `:1406` 于是继续走到证据构造，发出
   `raw_manifest_repair.manifest_exists: true` +
   `retry_policy.automatic_retry_allowed: true`——**从未运行过的探针驱动
   自动重试**，与 live spec job-retry-mechanism 的 #1365 不变量（absent/
   present 只能来自真正跑过的探针；无 root 必须 fail closed 带
   `object_store_root_unconfigured`）正面冲突。镜像面：repair 腿 `:1346`
   同几何把真实缺失判成「存在」，吞掉修复通道。
2. **`ObjectStoreError` 未容器化**：`LocalObjectStore.exists` 把
   `SafeFilesystemError`（symlink 探针目标/祖先、NFS ESTALE/EIO）转抛
   `ObjectStoreError`（RuntimeError 子类）。两处调用、决策调用点
   `scheduler_state_decision.py:318/:322` 及以上整链无 except——异常逃逸
   中止整趟调度 pass，其余候选全不被评估。生产 NFS 几何下可达。

## What Changes

- 两处探针调用改走 `_artifact_uri_missing_status(candidate,
  str(manifest_uri))`，直接继承 root fail-closed
  （`object_store_root_unconfigured`）与 `artifact_probe_error` 容器。
- **显式定性（fixture review 两轮重裁终态：unsafe 时两腿弃权）**：
  `unsafe_reason` 非 null 时两条腿一律 `return None`——腿只有拿到真探
  针裁决（`unsafe is None`）才能 claim 候选。弃权后候选走既有决策梯子
  （permanent guard / cancelled / forcing block / generic retry 原样），
  瞬时失败保留自动重试。**这是对 issue 解决思路「降级为带 reason 的
  blocked/manual 通道」建议臂的具名偏离**，理由 = fixture round-2 P1 可
  用性实测：repair 腿在 unsafe 下剩余门极弱（任意非 source-cycle 失败 +
  raw uri + 有过成功 download + 非 permanent 即放行），fail-closed 人工
  证据会把几乎整个 cycle 的 SLURM_TIMEOUT/PREEMPTED 等瞬时失败从自动重
  试改判人工 blocked——一次 NFS 抖动（artifact_probe_error 典型源）冻
  住整批；且两腿探针互斥在 unsafe 下失效、门最松的 repair 腿吞并
  downstream 腿。弃权设计同时保住 #1313 permanence 面与 #1365 forcing
  rung（`_missing_forcing_block` 在 root 未配置时本就 fail-closed 带
  reason，可区分 reason 由该 rung 对其所属几何继续提供）。
- **决策层零改动**（弃权设计下腿只在真 claim 时返回非 None，
  `scheduler_state_decision.py:318-328` rung 硬编码 retry 保持正确——
  round-1 F1 的 P0 随弃权设计消失）。
- `unsafe_reason` 为 null 时行为逐字节不变（探针结果等价，byte-compat）；
  root 未配置几何下 repair 腿今日本就弃权（fail-open「存在」→ not
  missing → None），弃权设计对该腿 = 逐字节同今日；唯一行为变化 =
  downstream 腿不再发 `manifest_exists: true` 的假自动重试（缺陷 1 本
  体）+ ObjectStoreError 被容器化不再整趟中止（缺陷 2 本体）。
- `_object_manifest_is_missing` 本身零改动——本 change 落地后其真实调用
  方仅余统一探针层一处（fixture review 全仓核实：`:1346`/`:1406` 是除探
  针内部 `:1031` 外仅有的两处，compat 文件均为再导出），无未登记同型
  fail-open 面；`_artifact_uri_missing_status:1027-1028` 的豁免注释随本
  change 改真。

## Out of scope

- `_object_manifest_is_missing` 签名/语义改造（改后仅余探针层单一调用
  方，无需盘点 follow-up）。
- #1313 permanence 洗白（同两函数、不同根因、已独立修）。
- #1365/PR #1390 已交付的 forcing/copyback/sidecar 腿。

## Impact

- Affected specs: job-retry-mechanism（ADDED 1 requirement）
- Affected code: `services/orchestrator/scheduler_state_failure.py`
  （2 处调用改路由 + unsafe 弃权分支 + 豁免注释改真）、
  `tests/test_production_scheduler.py`；
  `scheduler_state_decision.py` **零改动**
- byte-compat 锚点（fixture review F3 换算）：真正的两条 run_once 用例
  `:20338`/`:20417`（配置真实 root，改后无感）保持绿；issue 引用的
  `:19064`/`:19156` 在 HEAD 已漂移到无关 #1313 用例，弃用。**测试几何迁
  移**：`:4657` 与 `_raw_manifest_decision` helper（`:20585`）调用者族
  （约 10 个，含 parametrize）为「无 root fixture + monkeypatch 裸探
  针」几何——统一探针下 root 未配置在 `:1019` 短路、monkeypatch 不再被
  调，必红。迁移 = **per-test 覆写**真实 `tmp_path` root（不动 179 处共
  享的 `_scheduler_candidate_fixture`；monkeypatch
  `_object_manifest_is_missing` 经探针内部 `:1031` 仍然生效），断言零改
  动；任何断言需要改动 → 停下重裁（其中一半是 #1313 permanence 钉子）。
  约束细节见 design D3 seam 8。
