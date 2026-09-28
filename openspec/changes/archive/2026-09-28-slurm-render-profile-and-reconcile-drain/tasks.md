# Tasks

## Risk packs

- [x] Concurrency / shared state / ordering — selected：#2574 并发 `--force` 覆写窗口、#2587 读/退出时序；→ 任务 1.3、2.1、2.2。
- [x] File IO / path safety / overwrite — selected：lane_dir 证据文件被覆写，证据字节不变；→ 任务 1.4。
- [x] Config / project setup — selected：`SLURM_GATEWAY_PARTITION_OVERRIDE` / `EXCLUDE_NODES` 部署叠加不能丢；→ 任务 1.2、1.5。
- [x] Error handling / rollback / partial outputs — selected：丢读导致空串被当成功；超时/饱和异常语义保持；→ 任务 2.3、2.4。
- [x] Resource limits / large input / discovery — selected：删除 poll-break 后读循环必须仍受截止时间约束；→ 任务 2.3。
- [ ] Public API / CLI / script entry — not selected：CLI 参数与输出不变。
- [ ] Schema / columns / units / field names — not selected：无 schema/字段变化。
- [ ] Auth / permissions / secrets — not selected：不涉及。
- [ ] Legacy compatibility / examples — not selected：仅内部函数抽取，公共签名不变（由 1.2 回归覆盖）。
- [ ] Release / packaging / dependency compatibility — not selected：无依赖变化。
- [ ] Documentation / migration notes — not selected：无运维流程变化。

## 1. #2574 渲染资源档内存化

- [x] 1.1 `resource_profiles.py` 新增纯函数：输入 profiles dict、model_id、partition_override、exclude_nodes，输出已校验的资源档（default + **仅 mapping 型** overrides[model_id]（与 `resource_profile_override_model_ids` 键集口径一致） + 部署叠加 + `validate_resource_profile`，校验失败抛 `ConfigurationError` 与原语义一致）。
- [x] 1.2 `RealSlurmGateway.resolve_resource_profile` 改为 `load_resource_profiles` + 该函数，保留 `getattr(..., "")` 防御读取；既有网关测试全绿。
- [x] 1.3 `_render_production_template` 照旧写 lane 证据文件；以同一文本 `yaml.safe_load` 得到 profiles，经 1.1 函数（带网关 settings 的 override）解析后 `render_template(..., profile=...)`；测试：委托前覆写 lane 文件为 B 的值，B 五项全部与 A 不同，脚本仍携带 A 的 partition/memory/walltime/cpus_per_task（`#SBATCH` 行）与 shud_threads（`SHUD_THREADS=`/`OMP_NUM_THREADS=` 导出行）；修复前红、修复后绿。
- [x] 1.4 dry-run / fake 车道 `lane_dir/resource_profiles.yaml` 与 master 逐字节一致（断言固定期望文本）。
- [x] 1.5 setenv `SLURM_GATEWAY_PARTITION_OVERRIDE` / `SLURM_GATEWAY_EXCLUDE_NODES` → 渲染脚本 `--partition=` / `--exclude=` 取 override 值。

## 2. #2587 reconcile reader 读到 EOF

- [x] 2.1 删除 `_bounded_sacct_stdout` 无事件分支的 poll-break；shape A 确定性回归（真实 `os.pipe` 替身 + 幂等写入动作经 `poll()` 与 `threading.Timer` 双送达，Timer 延迟 > 0.25 s 空转 select，取 1.0 s），修复前红；修复后断言不依赖 Timer/`poll()` 先后。
- [x] 2.2 删除 `_bounded_visibility_stdout` 的 poll-break；stdout+stderr 双 pipe shape A 回归（同 2.1 的墙钟无关约束），修复前红；`default_sacct_querier` shape A 下返回非 `None` 的 `SacctRecord`。
- [x] 2.3 超时：写端永不关时在截止内抛 `ReconcileQueryUnavailable`（两个 reader 各一例，不耗生产超时时长）。
- [x] 2.4 byte/row 饱和仍抛 `ReconcileQuerySaturated`（既有用例保持绿或新增）。
- [x] 2.5 新测试放入已被 selector 路由的既有测试文件，不新建 `tests/test_*.py`。

## Evidence Floor

- [x] `uv run pytest -q tests/test_production_slurm_validation.py`
- [x] `uv run pytest -q tests/test_gateway_reconcile_comment_sacct_bounds.py tests/test_gateway_reconcile_comment_capability.py tests/test_gateway_reconcile_inflight_identity.py`
- [x] `uv run pytest -q tests/test_gateway_reconcile_*.py`
- [x] `uv run pytest -q tests/test_real_slurm_gateway.py`（网关 resolve 抽取回归）
- [x] `uv run ruff check .`
- [x] 新行为测试对 `origin/master` 源码（`git show origin/master:<file>` 临时还原）确定性变红、修复后变绿，输出贴 PR。
- [x] `openspec validate slurm-render-profile-and-reconcile-drain --strict --no-interactive`
- [x] node-22：不需要（本地打桩闭环；22 维护窗口前冻结）。
