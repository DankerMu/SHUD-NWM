# Design — station-forcing-csv-concurrent-replace-retry

行号锚定 `master` @ `57a14098`。**动手前每处引用按符号名 grep 复核**——issue #1660 正文的行号锚在更早的 commit。

## 风险分级（本地 triage，非上游给定）

issue #1660 是手填 issue，**不带** `Suggested fixture level` / `Minimal mergeable slice`，
因此 fixture 级别由本 change 自行裁定：**standard**。理由：改动面只有一个函数、
无 schema/迁移/配置变更，但触碰的是**生产对外 API 的并发语义**与一条共享安全原语的消费方式，
错误的重试范围会把真实损坏当成竞态吞掉。

风险轴（对齐 `openspec/project-profile.md`）：并发/竞态语义、共享安全原语的消费边界、对外 API 错误契约。
选中 risk pack：correctness、invariant/state-machine compatibility、test evidence。
未选：security（不放松任何 no-follow / containment 检查，见 D3）、performance（无 sleep，见 D5）。

## D1 —— 重试只包住 open，不包住 parse（本设计的中心论断）

`identity_changed` **只可能**从 `open_file_no_follow` 抛出（唯一 raise 点 `safe_fs.py:305`，
在 `os.open` 之后、返回 fd 之前）。fd 一旦拿到，它绑定的是**旧 inode**；`os.replace` 的语义保证
那个 inode 上的字节是一个**完整的、自洽的快照**——这正是原子替换存在的意义。

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

**非 `identity_changed` 的 kind（`unsafe` / `io` / `indeterminate`）必须首次即抛**，
不得进入重试——它们分别是 symlink/非常规文件/containment 拒绝与 I/O 故障，重试对它们无意义且会掩盖攻击面。

## D3 —— `FileNotFoundError` 路径完全不动

`os.replace` 是**原子**的：目标路径在替换全程始终指向某个 inode，**不存在文件缺失的窗口**。
因此 `FileNotFoundError` → `StationForcingFileNotFoundError`（HTTP 404，
`object_store_forcing.py:544-553`）与这场竞态无关，**不加重试、不改映射**。

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
  `parse_reason` 以稳定 token **`concurrent-replace`** 开头，其后可附脱敏后的原始文本；
- 其余错误的 `parse_reason` 保持 `_public_error_reason(error)` 原样——
  既有 spec scenario「file open/read OS errors are mapped to malformed」要求
  「`details.parse_reason` SHALL preserve operator-useful error text」，不得被本改动削弱。

`packages/common/object_store_forcing.py` 当前**没有引入 logging**（`:1-20` 无 logger）。
issue 验收标准写的是「错误 reason / **或** 日志」，本 change 选 reason 一路，
不为此新引入日志设施——那是独立的可观测面改动，不在本 change 范围。

## D5 —— 重试旋钮：次数为常量且可注入，间隔默认 0（**记录偏离**）

journal 先例是 `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`
（`file_orchestration_journal.py:153`，总尝试次数含首次），且**刻意不 sleep**
（`:4620` 注释 "and with no sleep"）。

issue #1660 验收标准第 1 条写「次数与间隔可配置/可注入」。本 change 交付：
次数为模块常量 + 可注入参数；**间隔为可注入参数但默认 0.0，即默认不 sleep**。

**这是对 issue 字面措辞的一处偏离，理由有二**：
(1) journal 先例明确 no-sleep，同一竞态在同一原语上应保持一致的吸收策略；
(2) `_read_csv_lines` 位于**同步** display API 请求路径上，sleep 会占住 worker 线程——
把一次亚毫秒级的 inode 竞态换成一次可测量的请求阻塞是净负收益。
参数保留是为了让"必须退避"的场景在未来无需改形状即可开启。此项须进 PR 的 `偏离记录`。

## D6 —— 与既有 spec 的关系

本 change 在 `object-store-station-series-read` 下 **ADD** 一条新 requirement，
不 MODIFY 既有 requirement：现存三条相关 scenario
（`spec.md:129` 硬边界、`:135` no-follow open、`:141` OS error → malformed）
描述的行为**全部保持不变**——本 change 只是把「并发原子替换」从 `:141` 那条的覆盖面里
切出来单独定义，`:135` 的 symlink 拒绝与 `:141` 的 `PermissionError`/`OSError` 映射一字不改。

## D7 —— 兄弟副本：报告不修

`packages/common/object_store.py` 的其它 `read_bytes_*` 读点（如 manifest/checksum 走
`read_bytes_limited`）若与并发原子写叠加同属此类，已在 #1600 普查范围内。
issue #1660 的 Out of scope 明确不扩面，本 change 遵守：**发现即报告，不在此 PR 修**。
