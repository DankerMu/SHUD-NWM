# Proposal: config-dbbacked-resolve-loop

## Why

Issue #1423（#1402 实现期 out-of-scope 观察，pre-existing，成因 d37dcbb4/#831）：
`_resolve_config_path_for_mode`（`services/orchestrator/scheduler_config.py:928-934`）
两臂处置不对称——db-backed 臂（`db_free_required=False`）`:930` 裸调
`path.resolve()` 无 except。后果按解释器分两条真实运行臂：

- **≤3.12（当前全部生产解释器：CI 3.11、node-27 3.11.15、node-22 3.12.7）**：
  环路 root 让 `ProductionSchedulerConfig.__post_init__` 构造即崩，抛无 errno 的
  `RuntimeError`（非 OSError），preflight/blocker 全部无从谈起。
- **3.13+**：不抛，环路 path 被原样收编成 `object_store_root`/`log_root`
  等正式字段值，静默进入下游（`workspace_root` 除外——3.14 实测在
  `scheduler_runtime_roots.py:711` 抛 ValueError，非静默收编，round-2
  Note-3 更正 issue 措辞）。

触发面是部署侧 env 配置（≥5 个 env 驱动字段，issue 证据 2 字段矩阵），非代码
路径。同函数 db-free 臂（`:931-934`）优雅降级、兄弟函数 `_optional_config_path`
（`scheduler_runtime_roots.py:569-590`，PR #1349 已修）不崩——本处是家族
（#1332→…→#1400/#1401/#1402）在 config 构造层的最后一条残余（db-backed 臂）。

## What Changes

按 issue 推荐方案：**沿用 PR #1349 在兄弟函数定型的严格 realpath 范式，逐字
对齐** `_optional_config_path`（`scheduler_runtime_roots.py:573-590`）：

- `:929-930` db-backed 臂改为 `os.path.realpath(path, strict=True)`，
  `except OSError` 回退非 strict `os.path.realpath(path)`（design D1：errno
  分流被兄弟函数注释显式裁弃——两条 lane 收敛同一产物；非 strict realpath
  在 3.11-3.14 永不抛且逐字复刻旧非 strict `Path.resolve()` 产物）。
- 裁定（issue 验收第 2 条「二选一」，design D3）：**canonical 产物下放 +
  分类归 storage preflight**——object_store/log/runtime 等 root 经
  `_slurm_preflight` 的 `_storage_root_check`（`scheduler_preflight.py:565-648`，
  strict realpath + errno 分流，非 ENOENT → `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH`
  结构化 blocker），与 allowed-roots 先例
  （2026-08-10-config-layer-allowed-roots-errno）同构。
- **构造不变量取景收窄（fixture review P1-1 实测）**：只对「末段环 + 非
  containment-base 字段」成立（OBJECT_STORE/LOG/RUNTIME/PUBLISHED_ARTIFACT/
  TEMP）。`WORKSPACE_ROOT`/LOCK 修复后仍经
  `scheduler_runtime_roots.py:558` 裸 resolve 崩且两臂异常类型不同（issue
  验收第 1 条点名 WORKSPACE_ROOT 的措辞超出单点判据可达范围，PR body 明记
  裁定）；EVIDENCE 两臂收敛于故意的 containment ValueError，属正确终态非
  残余（round-2 Note-1）；父段环形仍经 `:597`/`:604` 崩——:558/:597/:604
  三站点均不在 issue 名单，已路由 issue-scribe 另立追踪（见 Non-Goals）。
- db-free 臂 `:931-934` 逐字不动（#1400 属地，issue Out-of-scope 明文）。
- 测试：构造期不崩不变量（环根 + env 驱动字段参数化）、ENOENT 语义不回归、
  db-free 臂对照、e2e 锚（环路 object_store_root → preflight UNSAFE_PATH
  blocker）、版本矩阵复测（design D4）。
- spec delta：`slurm-array-runner-integration`「Array-capable model stages」新增
  「unresolvable general storage root at configuration construction」场景，镜像
  既有 allowed-roots 场景措辞。

## Risk Triage

- Fixture level: **compact**。issue 无 Suggested fixture level（旧格式）；预估
  规模 S、单点判据、范式已由 PR #1349/#1399 定型可逐字复用、下游承接面
  （`_storage_root_check`）已实测存在；但涉及解释器版本分叉 + 部署面触发 +
  零测试锁（issue 证据 5），不到 none/低。divergence：无基线可比，triage
  依据如上记录。
- Repair intensity: standard。
- Risk packs:
  - compatibility/regression: **selected** —— 非环路径产物必须与旧非 strict
    `Path.resolve()` 逐字同值（兄弟函数注释已论证）；ENOENT「配置期不做存在性
    校验」语义不回归；db-free 臂 diff 级零改动。
  - version-divergence（state-machine pack 变体）: **selected** —— ≤3.12 与
    3.13+ 两臂必须产出同一规范形与同一后续判定；红证只在 ≤3.12 可观测
    （本地 .venv 3.14.2，红证经隔离环境
    `UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311 uv run --python 3.11 …`
    + node-27 oracle，命令形态见 design D4）。
  - spec-compliance: **selected** —— 新场景与 allowed-roots 既有场景措辞
    对齐，归档后成为活契约。
  - decision-ladder、deletion-safety、security/auth、performance: not
    selected —— 无决策梯/删除/权限面；单函数判据非热路径。
- Seams under test：`_resolve_config_path_for_mode` 纯函数直测 +
  `ProductionSchedulerConfig()` 构造 seam（env 驱动）+ `_slurm_preflight`
  seam（e2e 锚）。

## Non-Goals

- `:931-934` db-free 臂——归 #1400（其验收标准第 6 条属地；本 change 不重开
  裁定）。#1400 与本 issue 是同函数两臂，issue 协调提示建议合并落地，但
  #1400 不在本运行池授权内——不扩面。
- `scheduler_runtime_roots.py:616`/`:625` 同款裸 resolve 副本——issue 自身
  「仅登记、不要求本 issue 一并修」，已在 #1423 正文登记跟踪，不另立单。
- **构造链残余裸 resolve 三站点**（fixture review P1-1/P2-3 实测，issue 任何
  名单均未登记）：`scheduler_runtime_roots.py:558`（`_confined_path`，
  containment-base 字段 WORKSPACE/LOCK/EVIDENCE 环根仍崩且两臂异常类型不同）、
  `:597`/`:604`（preserve-final helpers，父段环形 `loopdir/a/tail` 仍崩
  ≤3.12）——已路由 issue-scribe 立单（编号随 PR body 记录），本 change 不修。
- allowed-roots 级（#1348/PR #1399 已收）、`_local_runtime_root_safety`
  （#1401）、local-artifact containment（#1402）。
- `_safe_preserve_final_component`/`_confined_path_for_mode` 的既有
  except 约定——不动。

## Impact

- `services/orchestrator/scheduler_config.py`（`:928-930` db-backed 臂判据）
- `openspec/specs/slurm-array-runner-integration/spec.md`（archive 回写）
- `tests/test_production_scheduler.py`（新回归测试；既有 symlink 环族在
  `:14318-14790`（local-artifact，helper `_symlink_loop_dir`@:14318 可复用）
  与 `:36264-36500`（storage preflight/allowed-roots），均不覆盖本函数——
  填零覆盖缺口。issue 正文行号 `:12570-12699` 已陈旧，fixture review 实测
  更正）
