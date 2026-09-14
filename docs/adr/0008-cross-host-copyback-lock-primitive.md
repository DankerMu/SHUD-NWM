# ADR 0008 — 跨主机 copyback 批互斥的锁原语按主机选择

- 状态：Accepted
- 日期：2026-09-14

## 背景

copyback 批互斥（`packages/common/copyback_guard`）是共享 copyback root 下
`.nhms-copyback-batch.lock` 上的一把排他锁。这个 root 物理上是 node-27 本地的
`/home/ghdc/nwm/object-store`，node-22 以 NFSv4.2（`local_lock=none`）挂成
`/ghdc/data/nwm/object-store`。原有取锁方（publisher 各 lane、run-tree copyback、两个
backfill、orchestrator retention）全在 node-22，一律用 `flock`。

Issue #2252 要求 node-27 raw retention 删 `canonical/<S>/<cycle>` 时也持这把锁。动手前必须回答：
NFS server 本地文件系统上的锁，能不能排斥 NFS client 在同一文件上的锁，两个方向都要。

## 实测结论

2026-09-14 在 node-27（server，5.15）与 node-22（client，6.8）上对同一个探针文件逐对测试，
完整矩阵见 `openspec/changes/harden-copyback-mutex-residuals/evidence/lock-interop-20260914.md`：

1. **node-27 `flock` 与 node-22 `flock` 双向不互斥。** 原封不动地在 node-27 调用现有 guard，
   得到的是一把「看起来持有、实际什么都不串行化」的锁。
2. **node-27 POSIX 记录锁（`lockf` 或 OFD）与 node-22 `flock` 双向互斥。** client 的 `flock`
   在 server 上落成 NFSv4 字节范围锁，与 server 本地 POSIX 锁同表；而本地 ext4 上 BSD `flock`
   与 POSIX 锁互不相干。
3. **node-27 本机上 `lockf`/OFD 持有者不排斥 `flock` 取锁者。**
4. node-27 的 Python 3.11 `fcntl` 没有 `F_OFD_SETLK`；node-22 的 3.12 有。

## 决策

guard 的 `acquire_copyback_batch_lock` / `release_copyback_batch_lock` / `copyback_batch_lock`
增加 `primitive: Literal["flock", "posix"] = "flock"`。

- **NFS client 侧（node-22）与本地 root：`flock`**，所有既有调用方默认不变。
- **导出 root 的主机（node-27）：`posix`**，即 `fcntl.lockf(LOCK_EX | LOCK_NB)`；busy 为
  `EAGAIN` 或 `EACCES`，两者都当作「被持有」。身份检查、先拒后建、deadline、错误类型与
  `flock` 完全一致。
- 唯一的 `posix` 调用方是 `scripts/node27_raw_retention.py`，只在 canonical lane 取锁。

## 权衡

**选 `lockf` 而不是 OFD。** OFD 锁语义更好（按打开文件描述，线程间也互斥），但 node-27 的
3.11 不暴露 `F_OFD_SETLK`，手工打包 `struct flock` 依赖 ABI。`lockf` 的代价是按**进程**：同一
进程的线程不互斥，并且关闭该文件上的**任何**描述符都会丢锁。因此 `posix` 只允许单线程、
持锁期间不在别处打开锁文件的取锁方使用；node-27 raw retention 是单线程 CLI，满足。

**记录而不强制的约束。** node-27 本机上 `posix` 与 `flock` 不互斥，所以将来任何在 node-27
取这把锁的进程都必须用 `posix`。今天 node-27 上没有别的取锁方（publisher 在 node-27 上
object-store root 与 copyback root 同一，走 same-root skip，不取锁）。

**node-22 不需要跟着部署。** node-22 现网 `flock` 写者已经与 node-27 `posix` 互斥；node-22
的 checkout 在 #1831 维护窗口前冻结，这个决策不要求它动。

**不放宽锁身份契约。** node-27 retention unit 以 `nwm`(1005) 运行，打不开 `0600`、属主 1103
的锁文件。改成组共享 `0660` 会被 node-22 冻结的现网代码当作不安全锁拒绝、打断所有写者。
owner 接受的后果：在 unit 以 copyback root 属主运行之前，到龄 canonical cycle 一律
`lock_failure: lock_unsafe`、不删；raw 与 PNG 缓存不受影响。

## 已知限制

- 跨主机互斥只有 receipt 证明；CI 与 node-27 测试只能覆盖同机 `posix` 对 `posix`。
- NFS server 重启后，nfsd 的 grace period 不约束 node-27 本地 `lockf`，node-27 retention 可能在
  node-22 仍在回收自己锁的窗口内拿到锁。概率低，未缓解。
- NFS client 整机宕机时，server 持锁到租约过期，node-27 取锁方与 node-22 写者一样只能等。
