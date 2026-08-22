# Design — station-forcing-csv-concurrent-replace-retry

行号锚定 `master` @ `57a14098`。**每一处行号引用在动手前必须按符号名 grep 复核**——
issue #1660 正文的行号锚在更早的 commit。

## 风险分级（本地 triage，非上游给定）

issue #1660 是手填 issue，**不带** `Suggested fixture level` / `Minimal mergeable slice`，
因此 fixture 级别由本 change 自行裁定：**standard**。理由：改动面只有一个函数、
无 schema/迁移/配置变更，但触碰的是**生产对外 API 的并发语义**与一条共享安全原语的消费方式，
错误的重试范围会把真实损坏当成竞态吞掉。

风险轴（对齐 `openspec/project-profile.md`）：并发/竞态语义、共享安全原语的消费边界、对外 API 错误契约。
选中 risk pack：correctness、invariant/state-machine compatibility、test evidence。
未选：security（不放松任何 no-follow / containment 检查，见 D3）、performance（不引入 sleep，见 D5）。

## D0 —— 生产拓扑：读方在 NFS **服务端本地盘**，写方才是客户端

这条排在最前，因为 D1 的成立与否完全取决于它，且它极易被读反。

实测（2026-08-22，两端 ssh 只读命令）：

- node-27 的 `hostname` **就是 `ghdc`**（`210.77.77.27` / `10.0.1.27`），本机跑 `nfs-server`，
  `/etc/exports` 导出 `/home/ghdc`。
- node-27 上 `df -hT /home/ghdc/nwm` → `/dev/mapper/ubuntu--vg-home ext4 … /home`，
  即 object store 在 node-27 是**本地 ext4**，不是挂载。
- node-22 上 `mount | grep ghdc` → `ghdc:/home/ghdc on /ghdc/data type nfs4 … addr=10.0.1.27`，
  即 node-22 是**客户端**。
- node-27 自身确有 NFS 客户端挂载，但只有 `stor:` 的 `/data/SpatialData` 与 `/data/ForcingData`，
  **不含** object store 路径。

因此 display API 的读（`apps/api/routes/data_sources.py:160` → `read_station_forcing_csv`）
发生在**服务端本地文件系统**上；producer 的原子写是**客户端**发起、由服务端在本地执行 rename。

## D1 —— 重试只包住 open，不包住 parse（本设计的中心论断）

`identity_changed` **只可能**从 `open_file_no_follow` 抛出（全仓唯一 raise 点 `safe_fs.py:305`，
在 `os.open` 之后、返回 fd 之前）。fd 一旦拿到，它绑定的是**旧 inode**；rename 使旧 inode
链接数归零，但本地 fd 保活该 inode 直到 close ——POSIX 的 unlink-while-open 语义——
所以 fd 上读出的字节是一个**完整的、自洽的快照**。

**这一条已实测，不是推论。**（受 fixture 审查质疑后补测；receipt 见 D8。）

由此得到边界：**open 之后的任何 `ValueError`（空文件、坏 header、blank row、nrow 不符、超界）
都是真实内容损坏，永远不是这场竞态。** 把 parse 也纳入重试会做两件坏事：
(a) 对真实损坏的文件反复重读，把确定性失败变成 N 倍延迟；
(b) 掩盖"写方产出了损坏内容"这类真缺陷。

因此实现形状是：open 在有界循环里，parse 在循环外只跑一次。

## D2 —— 选择判据是 `error.kind`，绝不是消息文本

`SafeFilesystemError` 的 `kind` 就是为此存在的（`safe_fs.py:14-32` 的类文档明写
「callers can branch on the field rather than on message text」）。
journal chokepoint 的实现（`file_orchestration_journal.py:4632`
`if error.kind != "identity_changed": raise`）与其变异矩阵的 M6 格
（「重试判据改成 message 子串匹配」必须转红）已经把这条钉死；本 change 沿用同一纪律。

判据必须是 **default-deny**：只有 `kind == "identity_changed"` 进重试，其余一律首次即抛。
读侧实际可达的其余 kind 是 `unsafe`（symlink / 非常规文件 / containment）与 `io`；
`indeterminate` 只由写侧 `atomic_write_bytes_no_follow`（`safe_fs.py:181,188,204`）产生，
读路径不可达——写 default-deny 而非枚举拒绝清单，正是为了不依赖这份清单的准确性。

## D3 —— `FileNotFoundError` 路径完全不动

`os.replace` 是**原子**的：目标路径在替换全程始终指向某个 inode，**不存在文件缺失的窗口**。
因此 `FileNotFoundError` → `StationForcingFileNotFoundError`（HTTP 404，
`object_store_forcing.py:541-550`）与这场竞态无关，**不加重试、不改映射**。

写下这条是因为它是最容易被"顺手加固"的地方：给 NotFound 也加重试会把
"文件已被 retention 轮转掉"这个正常的 404 拖成 N 次重试后的 404。

## D4 —— 耗尽后 fail-closed，且 reason 由 kind 映射而非消息透传

重试耗尽后**仍然**抛 `StationForcingFileMalformedError`（HTTP 500 / `STATION_FORCING_FILE_MALFORMED`），
不新增错误类型、不新增状态码——issue 验收标准第 2 条即此。

但 `details.parse_reason` 必须能把并发替换与真实损坏分开。现状
`_public_error_reason`（`object_store_forcing.py:692-696`）只做绝对路径脱敏
（`/a/b/c` → `<path>`），消息文本 `Target file changed while being opened: <path>` 虽然肉眼可辨，
**但按 D2 的同一条纪律，reason 不得由消息文本推导**。因此：

- 当 `isinstance(error, SafeFilesystemError) and error.kind == "identity_changed"` 时，
  `parse_reason` 以稳定 token **`concurrent-replace`** 开头，其后附脱敏后的原始文本；
- 其余错误的 `parse_reason` 保持 `_public_error_reason(error)` **一字不改**——
  既有 spec scenario「file open/read OS errors are mapped to malformed」要求
  「`details.parse_reason` SHALL preserve operator-useful error text」，不得被本改动削弱。

`packages/common/object_store_forcing.py` 当前**没有引入 logging**（`:1-20` 无 logger）。
issue 验收标准写的是「错误 reason / **或** 日志」，本 change 选 reason 一路，
不为此新引入日志设施——那是独立的可观测面改动，不在本 change 范围。

## D5 —— 只交付次数旋钮，**不交付间隔旋钮**（记录偏离）

journal 先例是 `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`
（`file_orchestration_journal.py:153`，总尝试次数含首次），且**刻意不 sleep**
（`:4621` 注释 "and with no sleep"）。

issue #1660 验收标准第 1 条写「次数与间隔可配置/可注入」。本 change 交付**次数**
（模块常量 + 可注入参数），**完全不交付间隔**。

**这是对 issue 字面措辞的一处偏离，理由有三，必须进 PR 的 `偏离记录`**：

1. journal 先例明确 no-sleep；同一竞态在同一原语上应保持一致的吸收策略。
2. `_read_csv_lines` 位于**同步** display API 请求路径上，sleep 会占住 worker——
   把一次亚毫秒级的 inode 竞态换成一次可测量的请求阻塞是净负收益。
3. 更关键：`_read_csv_lines` 的唯一生产调用点是 `object_store_forcing.py:421`
   （`read_station_forcing_csv` 内部），不暴露给 API 层。若只把 interval 参数透传到
   `_read_csv_lines`，它在生产路径上**恒为 0、永不可达**，只有测试能设非零值——
   那就是一个需要专门写一条测试来证明自己不是死代码的旋钮，YAGNI 上净负。
   真要开退避，届时改 `read_station_forcing_csv` 及其调用方签名即可，代价与现在加它相当。

配套约束：实现中**不得出现 `time.sleep`**，并由测试 3.7 锁死（见 tasks §3）。

## D6 —— 与既有 spec 的关系：**MODIFIED 是必须的，不能只写 ADDED**

本 change 既 ADD 一条新 requirement，也**必须 MODIFY** 既有 requirement
`openspec/specs/object-store-station-series-read/spec.md:63`
（"CSV parse and valid_time computation"）。原因：

`SafeFilesystemError` 是 **`RuntimeError`** 子类（`safe_fs.py:10`），**不是 `OSError`**。
因此 `:141` 那条 scenario（WHEN 是 `PermissionError` 或 generic `OSError`）
**从来没有覆盖过** `identity_changed`。真正覆盖它的是 `:135`：
「a symlink **or otherwise rejected by no-follow filesystem checks**」——
identity 拒绝正是一种 no-follow 检查拒绝。

若只写 ADDED，合并后的 spec 会对同一触发条件给出两条相反义务：`:135` 说必须 500，
新 requirement 说重试成功时返回 200。`openspec validate --strict` 只查结构、抓不到这种冲突。

因此 delta 里 MODIFIED 全文重述 `:63` 名下的 requirement（本仓惯例是整条含全部 scenario 重述），
把 `:135` 的 WHEN 收窄为「symlink / 非常规文件 / containment 违规」，
并显式把 mid-open identity 拒绝划归新 requirement。`:129`（硬边界）与 `:141`（OSError 映射）
一字不改——前者与本改动无交互，后者本就不覆盖本竞态。

## D7 —— 兄弟副本：报告不修

`packages/common/object_store.py` 的其它 `read_bytes_*` 读点（如 manifest/checksum 走
`read_bytes_limited`）若与并发原子写叠加同属此类，已在 #1600 普查范围内。
issue #1660 的 Out of scope 明确不扩面，本 change 遵守：**发现即报告，不在此 PR 修**。

## D8 —— 已裁定的审查发现：post-open ESTALE 假说（**实测推翻**）

fixture 审查提出一条 P1：本 change 的拓扑是跨 NFS 客户端，node-22 的 `os.replace`
会让 node-27 手中的 filehandle 在后续 `os.read` 上返回 **ESTALE**，
因而 D1 的"fd 即自洽快照"为假，竞态只是从 open 挪到了 read。

**该假说的前提为假，已由 D0 与下面的实机 receipt 推翻。** ESTALE 的前提是**读方是 NFS 客户端**；
本拓扑里读方是**服务端本地 ext4**（D0），写方才是客户端。服务端执行的是一次本地 rename，
旧 inode 因本地 fd 持有而保活。

实机 receipt（2026-08-22，probe 目录用后即删）：

```
# reader on node-27 (local ext4), holding an fd across the replace
OPENED ino=12474865 size=300012 head=b'OLD_CONTENT_oooo'
# writer on node-22 (NFS client)
WRITER=REPLACED new_ino=12474866
# reader continues
RESULT=READ_OK tail_bytes=299996 total=300012 fstat_ino=12474865 nlink=0
```

`nlink=0` 证明旧 inode 已被 unlink，而 fd 仍读出全部 300,012 字节、内容未变、
无 ESTALE、无短读。D1 的第二分句在真实拓扑上成立。

**残留（如实记录，不掩盖）**：若未来把 display API 迁到 node-27 之外、
使其经 NFS 客户端读同一 mirror，本条论断即失效，届时必须重开 post-open 分类问题。
这个前提已写进 D0，实现里不做防御性代码。

审查的另一半仍被采纳：runbook 措辞**不得**写成「无 `concurrent-replace` 前缀 = 文件损坏」，
只写「有前缀 = 读到了原子替换窗口且重试耗尽」（见 tasks 4.1）。
