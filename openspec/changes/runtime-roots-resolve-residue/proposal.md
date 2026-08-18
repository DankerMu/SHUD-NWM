## Why

Issue #1520（#1332→…→#1423 家族，Depends on #1423 已落地）：同一配置构造链上仍余三处裸
`resolve()`——`scheduler_runtime_roots.py:558`（`_confined_path` parent，containment 检查内部）、
`:597`/`:604`（preserve-final helpers 父段解析，db-backed 臂经 `scheduler_config.py:883-899`
裸透传）。环路 root 在 ≤3.12 抛无 errno RuntimeError（CI 3.11 / 生产 3.11.15+3.12.7 全在崩溃臂），
3.13+ 走 ValueError 或静默——两臂异常类型分裂，违反家族「跨版本同一判定」原则。零测试锁（证据 3）。

## What Changes

- 三站点逐字复用 PR #1349/#1423 定稿范式：`os.path.realpath(strict=True)` + `except OSError`
  → 非 strict `os.path.realpath`，分类权交下游（containment 检查 / storage preflight），
  配置构造期不中止进程。
- `:597`/`:604` 把范式适配到**父段解析**（末段语义保持逐字不变）。
- `:558` 失败语义裁定（issue 点名的唯一待裁项）：环路 parent 取非 strict 规范形后交
  `_require_under_workspace` 判——两臂收敛到既有结构化 ValueError（containment 拒绝），
  即 ≤3.12 对齐 3.13+ 现状（证据 1 的 3.14 列为收敛目标）。
- 回归测试：三站点 × 两几何（末段环/父段环）× containment-base 与非 containment-base 字段；
  helper 直测 + config 构造级用例（沿 #1423 P2-3 先例）。

## Non-Goals

- `scheduler_config.py:930`（#1423 已修）；`:616`/`:625`（维持 #1423 登记面）；
  db-free 臂 path 级（#1400）；`_local_runtime_root_safety`（#1401）；local-artifact（#1402）；
  allowed-roots 三层（#1348/#1399）。
- 3.14 上 WORKSPACE_ROOT 环报 evidence_dir 字段名的归属偏差：修复中核对，若超范围另行登记。

## Risk triage

- Fixture level: compact（同族 #1423 先例一致；范式已定稿，纯复用+适配）。
- Repair intensity: medium（path-safety 面但范式既定、无新判定逻辑；#1423 同档）。
- Risk packs: version-divergence selected（家族主轴：3.11/3.12 vs 3.13+ 同判定）；
  path-safety/containment selected（:558 在 containment 检查内部，语义不得漂移）；
  test-evidence selected（证据 3 零覆盖缺口）；其余 not selected（无 DB/Slurm/发布行为）。

## Must preserve

- ENOENT（尚不存在的合法路径）与「配置期不做存在性校验」语义不回归。
- `_confined_path` containment 语义（must be under workspace_root）对非环输入逐字不变。
- 非 strict realpath 产物与旧非 strict resolve 逐字一致（#1423 D2 论证复用）。
- db-free 臂（`_safe_preserve_final_component` / `_confined_path_for_mode` db-free 臂）行为不变。
