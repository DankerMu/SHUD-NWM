# Proposal

## Why

批 SL 合并两条 Slurm 面的残留缺陷，一个 PR 收口：

- **#2574**（#1908 剩余面）：`_render_production_template`（`services/production_closure/slurm_validation.py`）把本次提交的资源档写进
  `lane_dir/resource_profiles.yaml`，再构造 `RealSlurmGateway(resource_profiles_path=<该文件>)` 并调用
  `render_template(...)` **不传 `profile=`**，于是网关从磁盘回读。`lane_dir` 只按 `run_id` 派生，`--force` 跳过
  `EvidenceWriter` 的存在性守卫；两个同 `run_id` 的并发 `--force` 提交之间，A 可能渲染出 B 的
  partition / memory / walltime / cpus_per_task / shud_threads。
- **#2587**（#2583 同形兄弟）：`services/orchestrator/reconcile.py` 的 `_bounded_visibility_stdout` 与
  `_bounded_sacct_stdout` 在 `select` 空转分支里一见 `process.poll()` 退出就 break，丢掉 pipe 中未读 stdout，
  rc=0 + 空串被当成功结果；`default_sacct_querier` 由此返回 `None`，inflight reconcile 把活作业写成
  `reconcile_unverified`。

## What Changes

- `services/slurm_gateway/resource_profiles.py` 新增纯函数，把"default + overrides[model_id] + 部署级
  `partition_override` / `exclude_nodes` 叠加 + `validate_resource_profile`"从
  `RealSlurmGateway.resolve_resource_profile` 抽出；网关改为 load-then-call，行为不变。
- `_render_production_template` 继续把 `lane_dir/resource_profiles.yaml` 按原字节写出（降级为纯证据），
  但渲染用的资源档在内存里由**刚写出的同一段文本** `yaml.safe_load` 得到，经上述纯函数（带网关 settings 的
  override）解析后以 `profile=` 传入 `render_template`，不再回读磁盘。
- 两个 reconcile bounded reader 删除无事件分支的 poll-break：只有所有已注册 pipe 读到 EOF 或截止时间到才结束。
  超时 / 饱和异常语义与函数签名不变。

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: #2574 absent; #2587 compact (override: 批次合并后 #2574 命中 concurrency + file IO + shared state 触发条件，整批按 expanded)
Blast radius: 生产闭环 live-submit 的 sbatch 资源指令（错档 → Slurm 拒绝/作业被杀/超订）；node-22 inflight reconcile 的 sacct/visibility 读（丢读 → 活作业误写 reconcile_unverified，或读循环无界）
Selected risk packs: Concurrency / shared state / ordering; File IO / path safety / overwrite; Config / project setup; Error handling / rollback / partial outputs; Resource limits / large input / discovery
Evidence floor: 见 tasks.md Evidence Floor（两单 Verification 原样 + 新测试修复前红/修复后绿）
```

node-22 实机不是本批必需 oracle：两单的渲染路径与 reader 都能用本地打桩（`sbatch` 桩、真实 `os.pipe` 进程替身）确定性闭环；
node-22 在维护窗口前冻结，本批不在 22 上执行任何命令。
