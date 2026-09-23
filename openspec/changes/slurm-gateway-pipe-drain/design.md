# Design

Change surface: `services/slurm_gateway/real_backend.py` `RealSlurmGateway._communicate_bounded`（:1289-1333），
仅由 `_run_command`（:1157）在 `subprocess.run` 未被替换时调用。上游是所有 Slurm CLI 调用
（`sbatch` 提交、`sacct` 状态/列表/数组、`scancel`、`sinfo` health）。

Must preserve:
- 截止时间：总耗时受 `subprocess_timeout_seconds` 约束；超时 `process.kill()` + `process.wait()`，然后抛
  `subprocess.TimeoutExpired`（由 `_run_command` 转成 `SlurmTimeoutError`）。
- 截断：单流超过 `MAX_SLURM_COMMAND_OUTPUT_BYTES` 时只保留前 N 字节，`truncated[stream]=True`，并 kill
  子进程；`_run_command` 随后抛 `SlurmCommandError`。具体是哪一种取决于 kill 的时机：子进程被 kill 后
  returncode 为 -9，走 `:1201` 抛 "failed with exit code -9"（常见）；子进程在 kill 前已经以 0 退出，则走
  `:1218` 抛 "above the safe capture limit"。两种都属于保持不变。
- 既有的真实 Popen 覆盖 `test_real_slurm_gateway_fake_binaries_cover_command_boundary`（`tests/test_real_slurm_gateway.py:134` 起）保持通过。
- 返回值形状 `(stdout_bytes, stderr_bytes, truncated)` 与 `process.returncode` 在返回前已确定（`_run_command`
  读 `process.returncode`）。
- `_run_command_via_subprocess_run` 分支与所有既有测试不变。

Must add/change:
- 读循环的结束条件改为 `selector.get_map()` 为空，也就是两个流都已读到 EOF（`read1` 返回 `b""` 时 unregister）。
  **删除两处 `if process.poll() is not None: break`**（:1311-1313 无事件分支、:1328-1329 事件处理后）。
  无事件时直接 `continue`，由截止时间兜底。
- 循环结束后照旧 `process.wait(timeout=剩余时间)`，保证 returncode 已确定。

Governing invariant: 子进程关闭其 stdout/stderr 之前写入的每一个字节（在截断上限以内），都出现在返回值里；
而且整个调用仍受截止时间约束。

Sibling surfaces:
- `_run_command_via_subprocess_run`：用 `subprocess.run` 的 communicate，本身读到 EOF，无此缺陷，不动。
- `fetch_logs`：读文件，不经 pipe，不涉及。
- 编排层对 502 的分类（#2584）：不在本单范围。

Seams under test: `RealSlurmGateway._communicate_bounded`，传入真实 `subprocess.Popen` 或持有真实 `os.pipe`
读端的最小进程替身；以及经 `_run_command` 的端到端路径（`subprocess.run` 保持原样，以便进入 Popen 分支）。

Required evidence:
1. B 形态（事件处理后 poll 已退出）：pipe 里预先写入 >8192 字节并关闭写端，替身 `poll()` 恒返回 0，
   返回完整字节。修复前只返回 8192 字节，确定性变红。
2. A 形态（无事件分支 poll 已退出）：首次 `select` 时 pipe 为空；替身在 `poll()` 被调用时写入
   `Submitted batch job 4242\n` 并关闭写端，然后返回 0；另有一个 1.0 s 的 Timer 做同样的（幂等）写入，
   因为修复后的代码不再调用 poll。返回值必须包含这一行；修复前为空，确定性变红。
3. stderr 同理不丢（在 1 或 2 中同时断言）。
4. 超时：真实子进程 `sleep` 超过截止时间（设很小的 `timeout_seconds`），仍 kill 并抛 `TimeoutExpired`，
   且在截止时间加小余量内返回。
5. 截断：真实子进程输出 > `MAX_SLURM_COMMAND_OUTPUT_BYTES`，返回长度 == 上限，`truncated["stdout"] is True`。
6. 端到端：`_run_command(["sh","-c", ...])`（或 `sys.executable -c`）输出 20000 字节时完整返回；sbatch 形态经
   `_parse_sbatch_job_id` 得到 job id。
7. node-22 复现脚本（`sleep_then_20000B`）300/300 返回 20000。

Non-goals:
- 修复前就存在、本单不处理的残留：如果子进程已关闭两个 pipe 但在截止时间前仍未退出，循环后的 `wait(timeout)` 会抛
  `TimeoutExpired`，却没有 kill 子进程。本单既不引入也不放大这个问题。
- 子进程把 pipe 句柄泄露给存活的后代进程（后代持有写端导致 EOF 迟迟不来）：此时改为等到截止时间后按超时处理。
  Slurm 客户端二进制不会派生常驻后代，这是有意接受的行为变化，在测试或注释中写明。
- 编排层不明确提交的分类与 manual-retry 缺口（#2584）。
- 改用 `sbatch --parsable`，或按 comment 反查作业（#1116：本集群 sacct 不存 comment）。

Review focus:
1. 两处 poll-break 都已删除，且没有其他路径会在 EOF 前返回。
2. 截止时间在所有分支上仍然有效（包括无事件分支持续空转直到超时）。
3. 截断分支 kill 之后，循环能靠 EOF 正常结束，不会等满截止时间。
4. 测试确实走真实 pipe/Popen 路径，没有 monkeypatch `subprocess.run`；并且在修复前的代码上确定性变红。
