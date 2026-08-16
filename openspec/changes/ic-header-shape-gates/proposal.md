# Proposal: ic-header-shape-gates (#1197)

## Why

上游交付的 lh_gl 标定 IC 头部只有 2 个数字 token（`23106\t6`，缺 minute-time），
穿过注册（`basins_discovery.py` 仅 glob 存在性）、打包 checksum
（只覆盖 manifest）、限定（`audit_first_cycle_initial_state.py` 仅
stat+sha256）三道关，在**首次真实消费**时由注入器
`_shift_cfg_ic_time`（`workers/shud_runtime/runtime.py:3215-3229`）按「最后一个
数字 token = minute-time」把**列数 `6`** 覆写成 epoch-minute `29720160.0` →
SHUD 按列数分配约 183 GB → OOM SIGKILL → 生产 IFS replay Phase 1 停机
（issue #1197 证据；即时文件修复已完成并归档，本 change 只做系统性防线）。

根因是**没有任何一处看 IC 内容形状**：`cfg_ic_header_minute_index`
（`packages/common/state_qc.py:503-521`）只在数字 token 少于 2 个时返回
None，`["23106","6"]` 恰好 2 个 → `minute_index=1` 命中列数。下一个畸形交付
物会在生产 run 里以同样方式炸掉，而不是在注册/限定期 fail-closed。

## What Changes

- `packages/common/state_qc.py` 新增**单一**共享 IC 头部形状校验 helper
  （frozen dataclass 返回：数字 token 计数 / mesh 计数 / 判定 / reason；
  合法形状 = 3-token native `<mesh> <cols> <minute>` 或 4-token 兼容
  `<mesh> <river> <lake> <minute>`；可选 `expected_mesh_count` 交叉校验
  ——由调用方传入，沿用本模块 `expected_*_count` 惯例）。
  **不改** `cfg_ic_header_minute_index`/`_header_counts` 语义（旁路新增，
  零既有调用点行为变化——`state_cli.py` checkpoint 路径同型漏洞显式
  out-of-scope，另行立单）。
- **注册门**（`workers/model_registry/basins_discovery.py`）：baseline 注册
  在 glob 匹配后对 `cfg_ic` 首行做有界读取 + 形状校验 + 与 `.sp.mesh`
  首行 mesh 计数交叉校验；畸形 → 该 model fail-closed 拒绝注册，reason
  可定位（含文件路径与实际数字 token 数）。
- **dg-variant 打包门**（`scripts/provision_direct_grid_scheduler_registry.py:354`
  一带）：`build_direct_grid_variant` 喂入 `state_schema_bytes` 前做同一形
  状校验，畸形 fail-closed 拒绝 provision。（issue 原文指向的
  `direct_grid_variant_registration.py` 经勘察**不接触 IC 字节**——纯 DB
  行插入；IC 字节唯一 dg 读取点在 provision 脚本。具名偏离，见 design D0。）
- **限定门**（共享判定边界，fixture review P1-1 更正落点）：形状判定下沉
  到 `PackagedIcObjectProbe`（`scheduler_generation.py:365-378`）新增
  header-shape 字段，由**两个**探针实现各自填充——生产调度门
  `services/orchestrator/scheduler_generation_gate.py:205-244`
  `_canonical_packaged_ic_probe` 与审计镜像
  `scripts/audit_first_cycle_initial_state.py:318-390`；判定统一在
  `classify_packaged_initial_condition`：畸形 → `ic_qualified=False`，新
  token `packaged_initial_condition_header_shape_invalid`（归
  UNQUALIFIED 内容判定分域）；「探针不可读」保持既有 UNREADABLE 分域
  **不混同**（AC-4 明文）。tier-a（inventory 形，baseline 打包产物如
  lh_gl）：classify 的 tier 分派与「tier-a 永不探针」既有锁不动，审计脚
  本在**自有层**对 tier-a 行追加内容探针（同一 helper 判形状、receipt
  覆写 + 新 `ic_qualification_source` 值 + schema 同步），让存量 51 个打
  包 package（含 dg 变体共 103 个 IC 文件）获得离线左移；生产调度门 tier-a 保持 metadata-only（判别 seam 与 receipt
  契约见 design D2 行 3）。
- **注入器 fail-closed**（`runtime.py:3215-3229` `_shift_cfg_ic_time`）：头部
  数字 token **少于 3 个**时不写文件 + 上抛 `SHUDRuntimeError`（文件字节
  前后一致）；3-token native 与 4-token 兼容布局行为逐字节不变。
- 回归夹具：真实畸形头部 `23106\t6` 在注册门、限定门、注入器三处分别
  被拦截的用例。

## Impact

- Affected specs: `basins-asset-discovery`（ADDED：注册期 IC 头部形状
  fail-closed；MODIFIED：既有「Valid SHUD input package」存在性 scenario
  补形状合法限定，避免归档后 spec 内自相矛盾）、`forecast-warm-start`
  （ADDED：packaged-IC 限定期形状判定与不可读分域不混同）、
  `shud-runtime`（ADDED：注入器头部形状 fail-closed）。
- Affected code: `packages/common/state_qc.py`（新增 helper）、
  `workers/model_registry/basins_discovery.py`、
  `scripts/provision_direct_grid_scheduler_registry.py`、
  `services/orchestrator/scheduler_generation.py`（`PackagedIcObjectProbe`
  形状字段 + `classify_packaged_initial_condition` 判定 + token 词表）、
  `services/orchestrator/scheduler_generation_gate.py`（生产门探针填充）、
  `scripts/audit_first_cycle_initial_state.py`（审计镜像探针填充 +
  tier-a 内容探针 + receipt note）、
  `schemas/first_cycle_initial_state_audit_receipt.schema.json`（limits 新
  键 + source 词表扩值，`additionalProperties:false` 故必须改）、
  `workers/shud_runtime/runtime.py`。
- Affected tests: `tests/test_state_qc.py`、`tests/test_basins_discovery.py`、
  `tests/test_runtime_ic_header.py`（其中 `:57-63`
  `test_shift_header_without_minute_time_pair_is_noop` 按新需求**重判**：
  1 数字 token 从静默 noop 改为上抛，见 design D5）、
  `tests/test_first_cycle_initial_state_audit.py`（含 `:713` inventory
  source 锁保持绿）、`tests/test_scheduler_generation.py`（含
  `:3035-3044` tier-a 永不探针锁保持绿——closure F-A 判别 seam 的直接钉
  面）、`tests/test_shud_runtime.py`（既有 `test_packaged_ic_*` 14 个全绿
  保持）。
- Out of scope（均具名，见 design Non-goals）：`state_cli.py` checkpoint
  归一化同型漏洞（另立单）；`basins_package.py` 打包 checksum 门（内容形
  状由上游注册门覆盖）；`state_clone.py` G10 指纹门（不同门，勿混）；
  SHUD 侧列数 sanity 上界（issue 第 5 项，可选可后置）；registry
  `package_checksum` 语义（issue 备选臂已裁 tradeoff 不做）；lh_gl 文件本
  体修复（已完成归档）；AC-1 通知半边（用户侧外部动作，issue 评论已登
  记）。
