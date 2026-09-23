# Tasks

## Risk packs

- [x] Concurrency / shared state / ordering — **selected**：子进程退出与读取之间的竞态。→ 测试 2.1、2.2
- [x] Resource limits / large input — **selected**：大于 8 KiB 的输出，以及截断上限不变。→ 测试 2.1、2.5
- [x] Error handling / partial outputs — **selected**：丢输出会被上游当成"作业未提交"。→ 测试 2.2、2.6；超时语义 → 2.4
- [x] Public API / CLI、Config、File IO、Schema、Auth、Legacy compat、Release、Docs — **not selected**：不改接口、配置、格式和文件行为。

## 1. 实现

- [x] 1.1 `services/slurm_gateway/real_backend.py` `_communicate_bounded`：删除两处 `if process.poll() is not None: break`，
      读循环只以 `selector.get_map()` 为空（两个流都读到 EOF）结束；无事件时 `continue`，截止时间逻辑不变。
- [x] 1.2 在函数内用一行注释写明不变式与 #2583，以及"后代进程持有 pipe 时按超时处理"这一有意接受的行为。

## 2. 测试（`tests/test_real_slurm_gateway.py`；**不得** monkeypatch `subprocess.run`）

- [x] 2.1 B 形态：stdout、stderr 各一个真实 `os.pipe`。向 stdout 写端**单次** `os.write` 20000 字节（远小于 64 KiB；
      macOS pipe 默认 16 KiB，只有向空 pipe 做一次大写入才会扩容，分多次写会死锁），然后**关闭两个写端**
      （stderr 写端不关的话，新代码会等到截止时间）。读端用 `os.fdopen(fd, "rb")` 包装。
      进程替身提供 `args`、`poll()`（恒返回 0）、`wait(timeout=None)`（返回 0）、`kill()`（空操作）、`returncode=0`。
      断言返回完整 20000 字节。
- [x] 2.2 A 形态：两个真实 pipe，首次 select 时均为空，写端未关。写入动作 = 向 stdout 写 `Submitted batch job 4242\n`、
      向 stderr 写一行、关闭两个写端；用 `threading.Lock` 加只执行一次的标志保护。触发方式有两个，**两个都要装**：
      替身 `poll()` 被调用时先执行写入动作再返回 0（重现旧代码"空闲 select 后 poll 已退出"的窗口）；
      另设 `threading.Timer(1.0)` 执行同一写入动作（修复后的代码不再调用 poll，靠它送达数据与 EOF）。
      Timer 在 `finally` 中 `cancel()` 并 `join()`。替身接口同 2.1。`timeout_seconds=10`。断言 stdout/stderr 都完整。
- [x] 2.3 2.1、2.2 在修复前的源码上确定性失败：实现者用 `git stash`（或临时回退）实跑并在报告中贴出红的输出。
- [x] 2.4 超时：真实 `Popen([sys.executable, "-c", "import time; time.sleep(30)"])`，`timeout_seconds=1`，
      抛 `subprocess.TimeoutExpired`，子进程已被 kill（`returncode` 非 None），耗时 < 5 s。
- [x] 2.5 截断：真实子进程输出 `MAX_SLURM_COMMAND_OUTPUT_BYTES + 10000` 字节，返回长度 == 上限，`truncated["stdout"] is True`。
- [x] 2.6 端到端：测试开头断言 `subprocess.run is real_backend._ORIGINAL_SUBPROCESS_RUN`（否则调用会悄悄走
      `_run_command_via_subprocess_run`，测了个空）。经 `gateway._run_command([...])` 的真实 Popen 路径，子进程输出
      20000 字节时完整返回；输出 `Submitted batch job 4242` 时 `_parse_sbatch_job_id` 得到 `"4242"`。
- [x] 2.7 既有 `test_real_slurm_gateway_fake_binaries_cover_command_boundary` 保持通过（不修改）。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_real_slurm_gateway.py`
- [x] 3.2 `uv run ruff check services/slurm_gateway tests/test_real_slurm_gateway.py`
- [x] 3.3 `uv run python scripts/select_ci_tests.py`（或等价方式）确认改 `real_backend.py` 时会选中 `tests/test_real_slurm_gateway.py`（已有路由，只核对不改）
- [x] 3.4 node-22 Linux 复现（只读，禁止 `uv sync` 或裸 `uv run`；在临时 checkout 上跑，不动活动树）。
      脚本全文如下（同一份在修复前的 `327df271` 上的实测结果为 `sleep_then_20000B: {8192: 8, 16384: 274, 20000: 18}`）：

      ```python
      import subprocess, sys, types, collections
      from datetime import datetime, UTC
      from services.slurm_gateway import real_backend as rb
      cls = rb.RealSlurmGateway
      fake = types.SimpleNamespace(_now=lambda: datetime.now(UTC))
      n = int(sys.argv[1])
      cases = {
        "sleep_then_20000B": ["sh", "-c", "sleep 0.05; exec head -c 20000 /dev/zero"],
        "sleep_then_line": ["sh", "-c", "sleep 0.05; exec printf 'Submitted batch job 1\\n'"],
      }
      for name, cmd in cases.items():
          got = collections.Counter()
          for _ in range(n):
              p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
              out, err, t = cls._communicate_bounded(fake, p, timeout_seconds=30)
              got[len(out)] += 1
          print(f"{sys.platform} {name}: returned_len_histogram={dict(got)}")
      ```

      运行：`cd <临时 checkout> && PYTHONPATH=. /scratch/frd_muziyao/NWM/.venv/bin/python repro.py 300`，
      要求 `sleep_then_20000B: {20000: 300}`、`sleep_then_line: {22: 300}`。

      实测（2026-09-23，node-22 临时 clone `~/tmp-2583`，活动解释器 3.12.7，活动树未动）：
      修复前 `real_backend@327df271`：`sleep_then_20000B: {20000: 36, 16384: 260, 8192: 4}`；
      修复后 `real_backend@87cf7a90`：`sleep_then_20000B: {20000: 300}`、`sleep_then_line: {22: 300}`。
      同一 clone 上 `tests/test_real_slurm_gateway.py` Linux 实跑：294 passed。
