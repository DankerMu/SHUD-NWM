# Proposal: config-layer-allowed-roots-errno

## Why

`_optional_config_path`(services/orchestrator/scheduler_runtime_roots.py:502-505)在 `ProductionSchedulerConfig.__post_init__` 构造期用非 strict `Path.resolve()` 规范化 `allowed_storage_roots`(唯一生产调用链:scheduler_config.py:945 ← :412,非 db-free 臂,无 try/except)。这是 #1332→#1344→#1345 同族第 5 处(#1347):

- **≤3.12(CI 3.11 + node-27 3.11.15 + node-22 3.12.7,全部真实运行面)**:构造前已存在的环路 root 让配置构造抛无 errno 的 `RuntimeError`,进程在任何 preflight 之前崩——PR #1346 的 `SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH` blocker 与 `blockers[0]` 证据契约在生产**不可达**(node-27 实机复现,见 issue #1347 证据 4)。
- **3.13+(仅本地开发机)**:配置层静默放行,preflight 接住出正确 blocker——同一份配置生产是裸栈、开发机是结构化裁决。

## What Changes

**出口形态裁定为选项 B(issue 两候选之一):配置层不做裁决,分类下放给 preflight。**

- `_optional_config_path` 换 `os.path.realpath(expanded, strict=True)` + `except OSError` + errno 分流(`Path.resolve` 两形态禁用,同 #1344/#1345 范式):
  - 成功 → 规范化路径(现状)。
  - ENOENT → 非 strict `os.path.realpath` 回退(缺失根规范化语义与旧非 strict resolve 逐字对齐,全版本不抛)。
  - 非 ENOENT(ELOOP/ENOTDIR/EACCES/…)→ **词法绝对路径原样下放**(expanduser + 相对时 cwd 绝对化),不抛、不裁决——由下游 `_preflight_allowed_roots`(PR #1346)剔根并产出结构化 blocker。
- 裁定理由:单一裁决点;#1346 的 blocker 与 `blockers[0]` 排序契约在 `_slurm_preflight` **seam** 上全版本真实可达(pass 级 ≤3.12 残余见下方披露 2)。选项 A(typed config error)会让刚合入的 blocker lane 变死代码、且把 NFS 瞬时错误(ESTALE/EACCES)升级为"进程无法启动"——即使一致性收窄到 seam,B 仍优于 A。
- 解除 PR #1346 留下的两处测试 workaround:stub docstring 的"separate ≤3.12 crash site"免责说明删除;端到端 ELOOP 锚改为**生产时序**(环先于 config 构造存在)。断言零削弱。
- 行为变更披露:
  1. ≤3.12 非 db-free 构造期崩溃 → 构造成功 + preflight 结构化 blocker(**seam 级**;pass 级见 2。契约升级,旧崩溃非契约)。
  2. **幽灵根窗口(有界)**:非 ENOENT 的不可解析值现在会存活在 `config.allowed_storage_roots` 里直到 preflight 剔除。消费方:`_preflight_allowed_roots`(剔根+blocker,#1346 已修)与 `_scheduler_allowed_roots`(scheduler_runtime_roots.py:448-462,#1348 已建单的同族缺陷位点)。后者在 ≤3.12 上**无条件**接管崩溃点:`run_once`(scheduler_runtime.py:606-609,非 db-free 臂无 try/except)先于 `_slurm_preflight`(:1159)调用 `_scheduler_lock_evidence_root_preflight`,其 not-required 早退 payload 自身就调 `_scheduler_allowed_roots`(scheduler_runtime_roots.py:168)——环根仍抛无 errno RuntimeError。**诚实结论:本变更使 `_slurm_preflight` seam 在全版本获得结构化裁决(锚点与 CI 可证),但 pass 级在 ≤3.12 上仍以裸栈崩溃,#1347 的用户可见症状要到 #1348 落地才完全解决**;3.13+ 上该位点行为与今日无异(配置层今日同样放行)。本变更加一枚版本门 tripwire 钉(B8)钉住该残余,#1348 落地时必须翻转它。

## Impact

- 代码:`services/orchestrator/scheduler_runtime_roots.py:502-505`(唯一改动函数;`os`/errno 常量该模块已导入,ENOENT 需补)。facade `scheduler_candidate_runtime.py:549` 动态 forwarder,签名(`Path|str|None → Path|None`)不变,零改动。
- 测试:`tests/test_production_scheduler.py` 新锚 + 两处 #1346 workaround 解除(断言不变、时序增强);既有全部测试零删除。
- 规格:`slurm-array-runner-integration` MODIFIED requirement——扩展 `unresolvable allowed storage root` scenario(配置构造永不因不可解析 allowed root 中止,分类下放 preflight)。
- 远端:AC 要求 node-27(3.11.15)实机复现修复后行为——PR 分支临时浅 clone(不动 `/home/nwm/NWM` ff-only 树)内跑 issue `Verification:` 选择器(主)+ 只读探针(补)+ provenance 断言(`__file__` 指向 clone),留 verbatim receipt。

## Non-Goals

- 不动 `_resolve_config_path_for_mode`(scheduler_config.py:926-932,db-free 词法容忍臂与其他 root 字段的非 db-free resolve)——issue 受影响面明示默认不做;db-free 臂行为以钉子锁定不回归。
- 不动 `_resolve_optional_config_path`(:528-531)、`_optional_config_path_relative_to`(:534-540)、`_config_path_preserve_final_component` 家族(保留末段语义不同,issue 明示单独评估)。
- 不修 `_scheduler_allowed_roots`(#1348)。
- 不改 `_preflight_allowed_roots` / preflight blocker 契约(PR #1346 定稿)。
