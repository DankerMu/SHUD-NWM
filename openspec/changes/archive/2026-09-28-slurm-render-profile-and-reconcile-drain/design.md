# Design

Change surface:
- `services/production_closure/slurm_validation.py::_render_production_template`
- `services/slurm_gateway/resource_profiles.py`（新增纯解析函数）
- `services/slurm_gateway/real_backend.py::RealSlurmGateway.resolve_resource_profile`（改为 load-then-call）
- `services/orchestrator/reconcile.py::_bounded_visibility_stdout`、`::_bounded_sacct_stdout`

Must preserve:
- `lane_dir/resource_profiles.yaml` 的文件名与内容逐字节不变（dry-run / fake 车道产出与 master 一致）。
- `RealSlurmGateway.resolve_resource_profile(model_id)` 对既有两个调用点（`real_backend.py:360` `submit_array`、`:787` `render_template` 空 profile 回落）
  返回值不变；scheduler / #2543 cohort 拆分走 `resource_profile_override_model_ids`，新纯函数必须只应用 mapping 型 overrides，与其键集口径一致；`getattr(self.settings, "partition_override"/"exclude_nodes", "")` 的防御式读取保留（测试替身 settings 可能缺字段）。
- `render_template` 内部仍跑 `validate_resource_profile` + `validate_sbatch_directive_context`；传入的 profile 必须非空
  （`profile or ...` 对空 dict 会回落磁盘）。
- PR #2571 的提交级 token / runtime inputs 隔离与 lane_dir 其余文件的 last-writer-wins 证据语义不动。
- reconcile：超时仍抛 `ReconcileQueryUnavailable("... timed out")`，byte/row 饱和仍抛 `ReconcileQuerySaturated`；
  `_bounded_sacct_stdout` 签名不变（`budget` 关键字被 `inspect.signature` 兼容垫片依赖）。

Must add/change:
- 渲染资源档来自内存，不再在写与读之间留文件窗口。
- 部署级 `SLURM_GATEWAY_PARTITION_OVERRIDE` / `SLURM_GATEWAY_EXCLUDE_NODES` 在内存路径上照样叠加（与网关同一函数）。
- 两个 reader 以"全部 pipe EOF 或截止"为唯一结束条件。

Governing invariant:
- 一次提交渲染出的资源指令只由该次提交的 config（加部署级 override）决定；一次 sacct/visibility 查询返回的是子进程写出的全部字节（上限内），与退出时刻无关。

Sibling surfaces:
- 资源档其他消费方：`RealSlurmGateway.resolve_resource_profile`（scheduler/网关 submit 走仓库 checked-in 共享 profile 文件，不按 run 现写现读，无同形窗口）；`resource_profile_override_model_ids`（只读 overrides 键集，不受影响）。
- production_closure 内 `resource_profiles_path` 仅 `slurm_validation.py:1007` 一处（grep 已确认）。
- 同形 poll-break：`reconcile.py` 中仅 `:324`、`:1201` 两处 `poll() is not None`；`_bounded_sacct_page` 是 `_bounded_sacct_stdout` 的包装；gateway `_communicate_bounded` 已由 #2585 修复；`packages/common/node27_pgdata_command.py`、`scripts/node27_timeseries_compression_supervisor.py` 已排除（均 EOF 才 break）。

Seams under test:
- `run_production_slurm_validation`/`_render_production_template` 的 live-submit 渲染输出（`sbatch` 打桩）。
- `resource_profiles` 新纯函数与 `RealSlurmGateway.resolve_resource_profile`。
- `_bounded_sacct_stdout`、`_bounded_visibility_stdout`、`default_sacct_querier`（monkeypatch `reconcile.subprocess.Popen` 返回持真实 `os.pipe` 读端的进程替身）。

Required evidence:
- 并发覆写：包装 `slurm_validation` 命名空间内的 `RealSlurmGateway.render_template`，在委托前把 `lane_dir/resource_profiles.yaml` 覆写为 B 的 partition/memory_gb/walltime/cpus_per_task/shud_threads（五项全部与 A 不同）→ 渲染脚本仍逐项携带 A 的值（shud_threads 无 `#SBATCH` 行，断言 `SHUD_THREADS=` / `OMP_NUM_THREADS=` 导出行，`real_backend.py:1067-1068`）；修复前红。
- override：setenv 两个 override → 渲染脚本 `--partition=` / `--exclude=` 取 override 值。
- 证据字节：dry-run 与 fake 车道 `lane_dir/resource_profiles.yaml` 与 master 逐字节一致。
- shape A：替身首次 select 时 pipe 空、写端未关；`poll()` 先执行幂等写入动作（写行 + 关写端）再返回 0；`threading.Timer` 执行同一动作供修复后代码送达；返回值含该行；修复前确定性红。
- 墙钟无关：`threading.Timer` 延迟大于 reader 0.25 s 空转 select（参照 #2585 用 1.0 s），修复前失败必经 `poll()` 路径；修复后断言与 Timer/`poll()` 谁先触发无关。
- `default_sacct_querier` 在 shape A 下返回非 `None` 的 `SacctRecord`。
- 超时：写端永不关 → 截止内抛 `ReconcileQueryUnavailable`（sacct 用短 `_SacctScanBudget.deadline`，visibility monkeypatch 超时常量）。
- 饱和：仍抛 `ReconcileQuerySaturated`。

Non-goals:
- `record=None` → `reconcile_unverified` 的状态机语义与恢复路径；comment 集群 `global_absence` 判据；`real_backend._communicate_bounded`（#2585）；`infra/sbatch` 模板；#2478 测试墙钟竞态；node-22 实机执行。

Review focus:
1. 内存 profile 与磁盘 profile 类型/取值零漂移（同一文本 safe_load）。
2. override 叠加在两条路径共用同一函数，网关既有行为不变。
3. 新并发测试在修复前真实变红（非恒绿）。
4. 删除 poll-break 后读循环仍有界（截止时间 + 终止回收）。
5. 新测试不依赖墙钟先后。
