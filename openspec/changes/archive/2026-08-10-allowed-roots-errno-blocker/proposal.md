# Proposal: allowed-roots-errno-blocker

## Why

`_preflight_allowed_roots`(services/orchestrator/scheduler_preflight.py:516-530)是 #1344 修完 `_storage_root_check` 后遗留的同族位点:它仍用"非 strict `Path.resolve()` 会对 symlink 环抛错"的死判据。CPython 3.13+ 上非 strict resolve 不再抛(GH-113838 语义变更),后果分两臂:

- `db_free_required=False`(生产 node-27 主路径):环路 allowed root 被**静默接纳为合法容器根**——fail-open。后续所有 `_storage_root_check` 的 containment 判定以一个不可解析的根为基准,`checks["allowed_roots"]` 证据面展示了一个假根。
- `db_free_required=True`:本应走"词法回退容忍"(PR #831 语义:NFS 未挂载的根是合法配置),3.13+ 上 except 臂死代码化,两臂行为坍缩为同一条——判别器失效。

≤3.12(node-22 是 3.12.7)上非 db-free 臂的 `raise` 会让 **RuntimeError**(pathlib 把 ELOOP 转成无 errno 的 RuntimeError)直接逃逸出 `_slurm_preflight`,把结构化 preflight 报告变成未捕获异常——同样不是契约行为。

## What Changes

- `_preflight_allowed_roots` 判据换为 `os.path.realpath(path, strict=True)` + `except OSError` + errno 分流(与已合并的 #1344 三位点同范式;`Path.resolve` 两种形态均禁用,理由见 design D1)。
- **出口形态裁定为 (b) 结构化 blocker**(issue 推荐项):非 ENOENT 且非 db-free 时,该根**从有效 allowed roots 中剔除**并产出 `SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH` blocker;函数返回形状变为 `(roots, blockers)`,gateway 装配面 `blockers.extend(...)`。
- ENOENT 保持"合法缺失根"语义:非 strict `os.path.realpath` 回退后照常纳入,两臂均不产 blocker(与旧非 strict resolve 对缺失路径的行为逐字对齐)。
- db-free 臂对非 ENOENT 保持 PR #831 词法回退容忍,不产 blocker——判别器在 3.13+ 上恢复可观测差异。
- 行为变更披露(契约升级,非回归),两个面:
  1. ≤3.12 非 db-free 臂的环根由"RuntimeError 逃逸崩溃"升级为结构化 blocker + `status="blocked"`。旧行为不是任何调用方依赖的契约(gateway 不捕获该异常,崩溃即 500)。**可达性限定**:此升级只对"config 构造之后才出现的环"生效——构造前已存在的环根在 ≤3.12 上会先在配置层 `_optional_config_path`(scheduler_runtime_roots.py:505)崩溃,preflight 根本不跑;该上游位点由 #1347 独立跟踪,不在本变更范围。ELOOP 车道在生产解释器(3.11/3.12)经真实 config 仅对构造后出现的环可达;经真实 config 全版本可达的收紧车道是 ENOTDIR/EACCES 类。
  2. **收紧面覆盖全部非 ENOENT errno,且作用于所有版本(含生产 3.11/3.12)**:EACCES(祖先不可 traverse)/ENOTDIR(路径中段是普通文件)等根在旧代码所有版本上被非 strict resolve 静默纳入,新代码一律剔除 + blocker——与 #1344 `_storage_root_check` 同判据,故意 fail-closed。本变更不是 3.13+ 专属修复。

## Impact

- 代码:`services/orchestrator/scheduler_preflight.py`(判据+返回形状)、`services/orchestrator/scheduler_gateway.py:50`(唯一真实调用点,解包+extend)、`services/orchestrator/scheduler_candidate_runtime.py:239`(facade 纯符号再导出,无需改动——任务含 grep 核对无其他消费方)。
- 测试:`tests/test_production_scheduler.py` 新增锚点;既有 114 条 preflight 测试零回归。
- 规格:`slurm-array-runner-integration` MODIFIED requirement(新 scenario:unresolvable allowed storage root)。
- 无 DB/远端接触面;无需 node-27 receipt。

## Non-Goals

- 不改 `_storage_root_check` / `_path_is_under_any`(#1344 已修,词法 containment 保持)。
- 不改 `checks["allowed_roots"]` 的掩码规则(db-free 掩码 `[local-path]` 原样)。
- 不引入"全部根被剔除后的补救根":有效根集合可为空,后续 containment 全数 OUT_OF_ROOT——fail-closed 且 blocker 已解释成因(design D4)。
- 不动 D4(#1332 已独立裁定 scope out 的其他位点)。
