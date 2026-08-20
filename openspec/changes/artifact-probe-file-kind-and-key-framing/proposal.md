# Proposal: artifact-probe-file-kind-and-key-framing

## Why

Batch P1 合并交付两个缺口。两者同族：都落在 `services/orchestrator/scheduler_state_failure.py`
的**同一条 artifact 存在性探针**（`_artifact_uri_missing_status`，`:1183-1233`）上，都是
#1365 那轮「fail-closed + 可区分 reason」治理**没覆盖到的反向敞口**——#1365 治的是
「探不了却声称存在」，这两条治的是「探到了但答错」。

1. **#1394 目录被当成"存在的产物"（两条腿同病）**：
   - object 腿（`:1201`）：`validate_object_path` 是纯字符串形状的闭世界校验，
     `runs/r1/output/basin_a/` 这类 4 段 key 满足 `len(parts) > len(pattern.segments)`
     即被当 FILE key 收下；随后 `LocalObjectStore.exists`
     （`packages/common/object_store.py:68-78`）走 `stat_no_follow`，而该函数
     （`packages/common/safe_fs.py:270-288`）**只拒 symlink**（`:277-278`），**不拒目录**
     （`S_ISREG` 检查只写在 `open_file_no_follow` 的 `:247`/`:257`，stat 路径没有）。
     于是一个占位目录 → `exists()==True` → `missing=False`。
   - local 腿（`:1231`）：`Path.exists()` 对目录同样为 True。

   后果：产物其实**不可读**（对该 key 写文件会 `IsADirectoryError`），探针却放行，
   下游 `missing_forcing_package_uri` / copyback / raw-manifest repair 三条腿全部
   丢失本该发出的 blocker——**这是 #1365 的反向 fail-open**。

2. **#1397 分类器与探针问的不是同一个 key（取景错位）**：
   `_needs_package_manifest_witness`（`:1047-1088`）拿**原始记录值**问
   `validate_object_path`，而探针经 `_object_manifest_is_missing` →
   `LocalObjectStore.normalize_key`（`object_store.py:183-196`）问的是**归一化后**的 key
   ——`normalize_key` 会剥掉 `object_store_prefix` 的**路径段**并做 percent-decode。
   该函数 docstring `:1064-1067` 明写「validator 不带 `OBJECT_STORE_PREFIX` 咨询，
   因为它在所有 tracked config 里都是 `s3://` uri，validator 自己的 `urlparse` 已经剥掉」
   ——**这句只对"无路径段的裸桶前缀"成立**。当 `OBJECT_STORE_PREFIX` 带路径段
   （或非 s3、或值含 percent-encoding）时，一个**物理存在**的 forcing FILE key 被误判为
   prefix-shaped → 伪造出 `<file>.nc/forcing_package.json` witness → `stat` 抛
   `NotADirectoryError` → `SafeFilesystemError` → `ObjectStoreError` → 非空
   `unsafe_reason="artifact_probe_error"` → `services/orchestrator/scheduler_candidates.py:1617-1621`
   以 `forcing_artifact_reference_unsafe` 拒绝授权修复。

   当下 latent（所有 tracked 部署都是裸桶前缀），但这是一条**无守卫的隐式部署约束**，
   而带路径段的前缀在别处是被刻意支持的
   （`services/production_closure/object_store_validation.py:2586` `_operational_prefix` 保留 `parsed.path`）。

## What Changes

- 探针层新增**文件种类判定**：object 腿与 local 腿在"存在"之上再要求"是常规文件"，
  非常规且确实存在的目标以新的可区分 `unsafe_reason = "artifact_target_not_a_file"`
  报 missing。`LocalObjectStore.exists` **语义逐字不变**（20+ 构造点）。
- 分类器改为**先归一化再问 validator**：从 `object_store.py` 抽出**纯字符串**归一化函数，
  `normalize_key` **委托**给它，分类器调用纯函数；签名加 `candidate`，prefix 经一个
  **store-free 全函数** helper 读取。分类器**绝不构造 `LocalObjectStore`**，
  其唯一允许的异常面是纯函数的 `ValueError`（见 design D4/D1.4）。
- sidecar 层不改代码：新 token **故意**落到该层既有的 missing-package blocker 并被修复渠道拒绝，
  不进 #1203 的读故障特判（design D6）——这同时补上 #1394 的 AC-2。
- 修正四处已成假的注释/文档：`:1064-1067`、`:1196-1198`、`:811-816`（自称本缺陷"unchanged here"），
  以及 `docs/runbooks/current-production-ops.md` 的 operator reason 路由表与其上一句。
- spec：`job-retry-mechanism` 中「store-side probe fault」那句把**会抛的故障**与
  **存在但非常规文件**分开表述，新增 key 取景要求、local 腿 symlink carve-out 与 sidecar 一句。

## Impact

- 受影响代码：`services/orchestrator/scheduler_state_failure.py`、
  `services/orchestrator/scheduler_state_common.py`（两个共享 helper；放这里是因为
  `scheduler_state_failure.py:19` 单向 import 它，反向会成环）、
  `packages/common/object_store.py`（**纯新增** + 一处委托重构）。
- 受影响 spec：`openspec/specs/job-retry-mechanism/spec.md`。
- 无 DB、无迁移、无 API 契约变更；db-free 纯文件态逻辑，本地 pytest 闭环。
