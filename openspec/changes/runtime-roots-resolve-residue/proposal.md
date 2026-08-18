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
  `_require_under_workspace` 判——两臂收敛到既有结构化 ValueError，即 ≤3.12 对齐 3.13+
  现状（证据 1 的 3.14 列为收敛目标）。终态按字段分述：NHMS_SCHEDULER_LOCK_ROOT 末段环
  →containment 拒绝（must be under workspace_root）；WORKSPACE_ROOT 末段环→safe-directory
  拒绝（evidence_dir must be a safe directory，经第四站点守卫）。
- 第四站点（实现期 scope+1，tasks 1.4 记录）：`_require_safe_directory_final_component`
  的**父段**解析同范式——不改它，WORKSPACE_ROOT 末段环在 ≤3.12 仍在到达 lstat 裁决前崩溃，
  上一条的收敛无法成立。该守卫自身的**末段**裸 resolve 不在本 change 内（见 Non-Goals）。
- 回归测试：三 fixture 站点 + 第四站点 × 两几何（末段环/父段环）× containment-base 与非
  containment-base 字段；helper 直测 + config 构造级用例（沿 #1423 P2-3 先例）。

## Non-Goals

- `scheduler_config.py:930`（#1423 已修）；`:616`/`:625`（维持 #1423 登记面）；
  db-free 臂 path 级（#1400）；`_local_runtime_root_safety`（#1401）；local-artifact（#1402）；
  allowed-roots 三层（#1348/#1399）。
- 第五站点：`_require_safe_directory_final_component` 的**末段**裸 `path.resolve(strict=False)`
  （evidence 目录末段自身为 workspace 内环路时 ≤3.12 抛 errno-less RuntimeError、3.13+ 静默放行）
  ——已立单 #1544 独立裁决（其规范形参与 containment 与 is_dir 双判定，失败语义需另行裁定）。
- 3.14 上 WORKSPACE_ROOT 环报 evidence_dir 字段名的归属偏差：修复中已核对确认超范围，
  已按承诺立单 #1545 承接（含 ≤3.12 该几何诊断文案不再含路径的倒退登记）。

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
- db-free 臂：3.13+ 产物逐字不变。`_confined_path_for_mode` db-free 臂在 ≤3.12 父段环几何
  的产物收敛到 3.13+ 规范形（家族收敛目标，非回归；实测无消费者依赖旧形态）。
  `_safe_preserve_final_component`（db-free preserve-final 臂，喂八个 *_preflight_path 字段）
  零改动：≤3.12 仍吞 RuntimeError 返回原始路径——该残余属 #1400，不在本 change 收敛范围。
