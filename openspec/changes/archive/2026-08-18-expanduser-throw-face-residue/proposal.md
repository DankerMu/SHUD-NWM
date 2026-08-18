# Proposal: expanduser-throw-face-residue (#1436 + #1441)

## Why

`Path.expanduser()` 在家目录不可确定时（`~<不存在用户>/...`，或无 passwd 条目且
`HOME` 未设时的 `~/...`）抛**无 errno 的裸 RuntimeError**，不是 OSError。#1424/PR #1435
已治 artifact-guard lane，但同一抛型残留五处，全在 try 之外或 except 接不住：

- **#1436 四站点**（配置/selector 面）：`services/orchestrator/scheduler_preflight.py:534`
  （`_preflight_allowed_roots`）、`:587`（`_storage_root_check`）——逃出后 `_slurm_preflight`
  整趟崩，调用点 `scheduler_gateway.py` / `scheduler_candidate_execution_evidence.py` 无 try，
  `SLURM_PREFLIGHT_*` 结构化 blocker 全丢；`services/orchestrator/retry.py:1629`
  （`_db_free_selector_allowed_roots`）、`:1667`（`_db_free_selector_path_rejection`）——
  py3.14.2 live 实测 ESCAPED（issue #1424 评论 §3），`db_free_*` rejection 不再生成。
  同文件 `:1529-1533` 教义注释在案（#1401/PR #1426），副本未回扫——机械对齐缺口。
- **#1441 一站点**（object-store root 面）：`packages/common/object_store.py:48`
  `LocalObjectStore.__post_init__` 的 root 展开在错误转换 try（`:50`，只接
  `SafeFilesystemError`）之外。统一探针两条 except 臂（`scheduler_state_failure.py`
  `except ObjectStoreError` / `except (OSError, ValueError)`）都接不住裸 RuntimeError，
  整趟 pass 崩、零 evidence（与 #1402/#1424 同型）；触发面是配置的
  `OBJECT_STORE_ROOT`/`resource_profile.object_store_root`，不分 uri 形态。

用户裁定两单并单交付（同一抛型家族、修复互补、文件不相交）。

**可达性口径（task 0 实测 + round-1 评审归因矫正，如实记录）**：preflight 两站点
经今天的真 `ProductionSchedulerConfig` **不可达**——db-backed 臂在 config 构造期
更早抛同型 RuntimeError，按字段分两条机制：多数根字段（workspace/object-store/
published-artifact/runtime/temp/lock/evidence 等）崩在 `scheduler_config.py`
`_expanduser_for_mode`；而 `allowed_storage_roots`/`log_root` 走
`_optional_config_path_for_mode` db-backed 臂**绕过** `_expanduser_for_mode`，
崩在 `scheduler_runtime_roots.py` 的第七/第八家族副本（`_optional_config_path:572`
/ `_config_path_relative_to_preserve_final:601`，**已立单 #1549**）。db-free 臂经
config 展开层后已 cwd 锚定、无前导 tilde 到达 preflight。故 #1436 issue 正文
「`_slurm_preflight` 整趟崩」对 db-backed 臂不成立（崩在更早的 config 帧）。
preflight 两处修复定性为**纵深防御 + 机械家族对齐**（钉测 lane 扩面仍必要）。
另三站点是**真活口**：`retry.py` 两处直接吃 env/manifest 原始字符串（#1424
py3.14 live 实测 ESCAPED），`object_store.py` root 来自
`OBJECT_STORE_ROOT`/`resource_profile` 原始值，均不过 config 展开层。

## What Changes

- **四站点走家族原语**（#1424/PR #1435 定稿修法）：`Path(...).expanduser()` →
  `Path(os.path.expanduser(...))`。不可展开的 `~user/...` 原样留存为相对路径，从各自
  **既有**臂走掉——fixture review 已预探真实终态：`_preflight_allowed_roots` 走
  **既有 ENOENT 容忍臂静默收编**（cwd 锚定、不产 blocker——该臂 docstring 明言
  ENOENT 永不 blocker；phantom-root 几何属 #1427 邻接面，本 change 照实记录、不改
  判定，即 issue #1436 验收 2 的「或按既有臂容忍」分支）；`_storage_root_check`
  走 ENOENT 臂后 contained/visible 阶梯 → `*_OUT_OF_ROOT`/`*_NOT_VISIBLE` 结构化
  判定；`_db_free_selector_allowed_roots` → `db_free_allowed_root_relative` 类
  rejection；`_db_free_selector_path_rejection` → `db_free_selector_path_relative`
  类 rejection。终态 reason 以 task 0 探针实测复核为准（沿 #1424 先例记录法）。
- **object-store root 不照抄原语**（#1441 issue 已论证副作用：原样留存的 `~unknown/store`
  会锚到 cwd 并**真建**字面 `~unknown` 目录）：把 `:48` 展开纳入既有错误转换边界，
  不可展开时抛 `ObjectStoreError`（RuntimeError 子类，纯收窄）。两个已复核调用点
  （`scheduler_state_failure.py` 统一探针 / sidecar tier）已接 `ObjectStoreError`，
  零改调用方即恢复 `artifact_probe_error` / `sidecar_unreadable` 归因。
- **钉测扩面**（receiver 判别式，非 attr 名全禁——沿 #1424 F1 裁定）：
  `tests/test_production_scheduler.py` 的 `_expanduser_calls_with_foreign_receiver` lane
  从 artifact-guard 扩到上述四函数，保留非空洞断言（lane 内 `os.path.expanduser`
  调用数 > 0）；`LocalObjectStore.__post_init__` 用行为门测锁（不逃逸 + 不建目录），
  不强塞 AST lane。
- **ride-along（#1436 附录，显式裁定）**：`scheduler_state_failure.py` 死包装
  `_artifact_uri_is_missing`（自诞生零调用方，grep + git log -S 双证）——删除；
  若实施中发现活调用方则保留并回报（则本条降级为 no-op，记偏离）。
- except 臂**不扩宽**（两 issue 备选均拒：宽类型吞编程错误 + 家族语义分叉）。

## Non-Goals

- #1400（`retry.py` `_db_free_selector_path_rejection` 内相邻行 `path.resolve(strict=False)`
  判据的 symlink 环/版本分岔）——只动展开行，不碰 resolve 行及其 except。
- #1427（`_db_free_selector_allowed_roots` ENOENT 臂 phantom 几何）；#1423/#1520 config
  db-backed 臂（已修）；#1424/PR #1435 已治的 artifact-guard lane（不重复动）。
- `services/orchestrator/` 其余约 40 处 `.expanduser()` 站点与 `LocalObjectStore` 其余
  构造站点（`scheduler_file_providers.py`、`tile_publisher/`、`workers/**` 等）：两 issue
  均声明未逐一审计，本 change 只治五处已复核站点，不做全仓扫（扩面需另立单）。
- 第六副本 `packages/common/safe_fs.py:721-723`（`_expand_path` 的裸
  `Path(path).expanduser()`，safe_fs 全部 16 个公共入口的共享前奏）：fixture review
  发现，两 issue 均未跟踪；本修复后 `LocalObjectStore` 交给 safe_fs 的已是绝对路径，
  无前导 tilde 可达——已立单 #1547 承接（含 scripts 面 live 逃逸证据与
  `chain_runtime_utils._absolute_configured_path` 孪生登记）。
- S3 适配面与 object-key 校验语义不变。

## Risk triage

- Fixture level: compact（#1424/PR #1435 同族先例；修法定稿，复用+一处域内错误收编）。
- Repair intensity: medium（fail-closed 判据面；但无新判定逻辑，S 规模）。
- Risk packs: path-safety/fail-closed selected（五站点全是判据函数，拒绝语义不得漂移）；
  test-evidence selected（钉测扩面非空洞 + 两触发面门测）；version-divergence **not**
  selected（本抛型全版本一致，无 3.11/3.13 分岔——与 #1520 家族不同轴）；其余 not
  selected（无 DB/Slurm runtime/display 行为）。

## Must preserve

- 不含 `~` 的绝对路径与可展开 `~/...` 在五处的判定结果逐字不变（含 ENOENT 臂、
  db-free 词法回退臂、object-store 现有全部用例）。**记录在案的接受偏差**（round-1
  path-safety 实测）：`./~/x` 形态（前导 `.` 且首个存续段为 `~`）在三个 str 输入
  站点从「Path 归一化后展开收编」变为「原样拒绝」——方向 fail-closed、非合理运维
  配置，显式入 byte-compat 钉测的 carve-out 分支，不算回归。
- `_slurm_preflight` blocker 结构与 reason 码集合不变（只是不再崩，不新增 reason）。
- **接受的新状态**：不可展开 tilde 的 allowed root 由 ENOENT 容忍臂以 cwd 锚定形态
  收编进 containment base（不产 blocker）——这是家族原语的既定语义（#1424 同款），
  其 phantom-root 面由 #1427 承接，本 change 不扩其 scope。
- `ObjectStoreError` 语义：仍是 RuntimeError 子类；`:49` cwd 锚定与
  `ensure_directory_no_follow` 对合法 root 的行为不变。
- 既有 receiver 判别式钉测对 artifact-guard lane 的约束不放松。

## Evidence mapping（→ tasks / 验收标准）

- #1436 验收 1-5 → tasks 1.1-1.2、2.1-2.3、2.5；#1441 验收 1-6 → tasks 1.3、2.4、2.6。
- 两触发面（unknown-user tilde / HOME-less plain tilde）monkeypatch 家目录探测覆盖，
  不造无 passwd 容器（两 issue 验收原文允许）。
- oracle 路由：`uv run pytest` 定向 + ruff + openspec validate 本地；merge 后 node-27
  后端验证 receipt（#1436 验收 6，Slurm preflight 判据）。
