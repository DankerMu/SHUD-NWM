# Proposal

## Why

#2583：`RealSlurmGateway._communicate_bounded`（`services/slurm_gateway/real_backend.py:1289-1333`）
一看到 `process.poll()` 返回退出码就停止读取，**不管 pipe 里还有没有没读完的数据**。
生产代价（node-22，2026-09-22/23，gateway journal + `sacct` 实测）：

- sbatch 两次 0 字节 stdout，但作业**已被 Slurm 接收并 COMPLETED**（53763、54918），gateway 返回
  502 `SLURM_PARSE_ERROR`。其中 54918 是 `basins_hlj` IFS 12Z 的 `state_save_qc`，编排层把它记为
  `submission_failed`，进而判为 `permanent_failure_guard`，candidate 从此每趟都被 blocked。
- sacct 两次输出**恰好截断在 8192 字节**（一个 `read1(8192)` 块），解析到半行，gateway 返回 502。

复现：node-22 Linux 上直接调用 `_communicate_bounded`，子进程为
`sh -c 'sleep 0.05; exec head -c 20000 /dev/zero'`，300 次里 **282 次被截断**（停在 8192/16384 字节）；
macOS 0/300。

现有 gateway 测试绝大多数 monkeypatch `subprocess.run`，走的是 `_run_command_via_subprocess_run`；
只有 `tests/test_real_slurm_gateway.py:134` 起的 `test_real_slurm_gateway_fake_binaries_cover_command_boundary`
（PATH 注入假二进制）走真实 `Popen` + `_communicate_bounded`，但它只输出短行，覆盖不到这个竞态。

## What Changes

- `_communicate_bounded` 的读循环改为以"两个流都读到 EOF"为结束条件；不再用 `process.poll()`
  决定停止读取。截止时间（`subprocess_timeout_seconds`）、超时 kill、`MAX_SLURM_COMMAND_OUTPUT_BYTES`
  截断语义不变。
- 新增针对真实 `Popen` 路径的确定性回归测试。

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: compact (override: 所有 Slurm 命令共享这一个读入口，且缺陷本身是竞态——命中 expanded 的 concurrency + reader 两个触发条件)
Blast radius: 所有 sbatch/sacct/squeue/scancel/sinfo 调用；改错会导致丢输出（当前现象），或者读循环无界/过早超时，让健康命令变成 SLURM_TIMEOUT
Selected risk packs: Concurrency / shared state / ordering; Resource limits / large input; Error handling / rollback / partial outputs
Evidence floor: tests/test_real_slurm_gateway.py 全绿；新测试在修复前的代码上确定性变红；node-22 复现脚本 300/300 完整
```
