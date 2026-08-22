# Tasks — station-forcing-csv-concurrent-replace-retry

行号锚定 `master` @ `57a14098`。**每一处行号引用在动手前必须按符号名 grep 复核**——
issue #1660 正文的行号锚在更早的 commit，可能已被后续合并推移。

## 1. 重试原语（`packages/common/object_store_forcing.py`）

- [x] 1.1 新增模块常量，命名对齐 journal 先例
  （`services/orchestrator/file_orchestration_journal.py:153`
  `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`），语义同为**总尝试次数（含首次）= 3**。
  在常量旁写明「3 = 首次 + 2 次重试」，避免被后人读成「重试 3 次」。
- [x] 1.2 把 `_read_csv_lines`（def `:490`）里的
  `open_file_no_follow(expected_path, containment_root=object_store_root)`（`:503`）
  抽成一个有界重试的私有 helper，**只包 open**（design D1）。
  helper 签名带 `attempts: int = <常量>`，并把该参数从 `_read_csv_lines` 一路透传上来，
  便于测试注入非默认值。
  **不加间隔参数、实现中不得出现 `time.sleep`**（design D5，此为对 issue 字面 AC 的记录在案的偏离）。
- [x] 1.3 重试判据**必须**是 default-deny 形状：
  `isinstance(error, SafeFilesystemError) and error.kind == "identity_changed"` 才重试，
  其余一切（其它 kind、非 `SafeFilesystemError` 异常）**首次即向外抛**（design D2）。
  **禁止**写成"拒绝清单"（枚举哪些 kind 不重试），也**禁止**任何形式的消息文本匹配
  （`in`、`startswith`、正则）作为判据。
- [x] 1.4 尝试耗尽时**原样抛出最后一次的异常**，由既有
  `except (OSError, SafeFilesystemError, ValueError)`（`:551`）接住 —— 不新增错误类型、
  不改 HTTP 状态码与错误码（design D4）。
- [x] 1.5 parse 段（`:504-535` header/行读取/`_parse_csv_header`）**保持在重试循环之外**，
  只执行一次。fd 的 `finally: os.close`（`:536-540`）保持原语义：
  每次成功 open 只对应一次 close，重试循环里失败的 open **不产生**需要关闭的 fd
  —— `open_file_no_follow` 在 `os.open` 成功后的所有异常路径都被
  `safe_fs.py:308-310` 的 `except Exception: os.close(file_fd); raise` 兜住，
  `os.open` 之前失败则根本没有 file fd，外层 `finally` 只关 parent fd。

## 2. 错误 reason 的 kind 映射（design D4）

- [x] 2.1 `:551-556` 的 malformed 分支：`parse_reason` 不再无条件走
  `_public_error_reason(error)`（def `:692`），改为先判 kind。
- [x] 2.2 `identity_changed` 耗尽时，`parse_reason` 以稳定 token **`concurrent-replace`** 开头，
  其后附 `_public_error_reason(error)` 的脱敏文本。token 与后文之间用固定分隔符，
  使消费方可用 `startswith` 判定而无需解析。
- [x] 2.3 其余错误的 `parse_reason` **一字不改**，仍**恰好等于** `_public_error_reason(error)` ——
  既有 spec scenario `openspec/specs/object-store-station-series-read/spec.md:141-145`
  要求「preserve operator-useful error text」，不得被本改动削弱（测试 3.3 逐字锁死）。
- [x] 2.4 `FileNotFoundError` 分支（`:541-550`，grep `StationForcingFileNotFoundError` 复核）
  **完全不动**：不加重试、不改映射（design D3）。

## 3. 测试（`tests/test_object_store_forcing.py`）

用例形状参照 `tests/test_file_orchestration_journal_read_cache.py:863` 的先例：
在 **`object_store_forcing` 命名空间内** monkeypatch `open_file_no_follow`
（模块顶部 `:19` 是 `from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow`，
因此必须 patch `packages.common.object_store_forcing.open_file_no_follow`，
patch `safe_fs` 原处**不会生效**）。

**3.1/3.2 必须注入非默认 `attempts`**（如 2 与 4，均 ≠ 模块常量 3），
否则"helper 忽略入参、硬用常量"这个变异体会全绿通过。

- [x] 3.1 重试成功分支：注入 `attempts=4`，前 3 次抛
  `SafeFilesystemError(kind="identity_changed")`，第 4 次委托真实 open，
  断言返回正常解析结果、无异常，且 open 调用次数 == 4。
- [x] 3.2 耗尽 fail-closed 分支：注入 `attempts=2`，每次都抛 `identity_changed`，
  断言抛 `StationForcingFileMalformedError`、`status_code == 500`、
  `code == "STATION_FORCING_FILE_MALFORMED"`、
  `details["parse_reason"].startswith("concurrent-replace")`，且 open 调用次数 == 2。
- [x] 3.3 非 `identity_changed` 不重试且 reason 不被削弱：
  分别以 `kind="unsafe"` 与 `kind="io"` 抛，断言 open **只被调用一次**，
  `parse_reason` **不含** `concurrent-replace` 前缀，
  且 `parse_reason` **逐字等于** `_public_error_reason(该异常)`（锁死 2.3）。
- [x] 3.4 判据不依赖文本（锁死 design D2）：
  (a) `kind="identity_changed"` 但 message 换成与现行原语完全不同的措辞 → **仍重试**；
  (b) `kind="unsafe"` 但 message 故意写成 `"Target file changed while being opened: /x"` → **不重试**。
- [x] 3.5 parse 失败不重试（锁死 design D1 的内容侧）：open 正常成功，但磁盘上是坏内容
  （空文件 / nrow 不符 / blank row 任选其一，复用既有 fixture 构造），
  断言抛 malformed 且 open **只被调用一次**。
- [x] 3.6 `FileNotFoundError` 不重试（锁死 design D3）：路径不存在，
  断言抛 `StationForcingFileNotFoundError`、open 只被调用一次。
- [x] 3.7 实现中不 sleep（锁死 design D5）：**必须在真实触发一次重试的序列下断言**——
  注入 `attempts=3`、前 2 次抛 `identity_changed` 第 3 次成功，
  同时 monkeypatch `time.sleep` 断言**零次调用**。
  （只读一个好文件再断言 sleep 为零是无效测试：那条路径本就不重试，变异体同样绿。）
- [x] 3.8 parse 阶段抛 `identity_changed` 也不重试（锁死 design D1 的**范围**侧）：
  monkeypatch `_ChunkedBoundedCsvLineReader.readline`（或其底层 `os.read`）
  在 open 成功后抛 `SafeFilesystemError(kind="identity_changed")`，
  断言 open **只被调用一次**、直接 malformed。
  **此项不可省**：没有它，「把重试范围从 open 扩到整个 parse」是一个**等价变异体**
  （判据仍是 kind，而 parse 正常只抛 `ValueError`/`OSError`，永不命中判据），
  5.3 的 M4 格将结构性不可能转红。

## 4. 文档

- [x] 4.1 `docs/runbooks/object-store-forcing-series-read.md`：在读路径一节补一段，
  说明 `concurrent-replace` 这个 `parse_reason` 前缀的含义与排查动作。
  措辞**只写单向**：「**有**该前缀 = 读到了 producer 的原子替换窗口且重试耗尽，不是文件损坏」。
  **不得**写成「无该前缀 = 文件损坏」——那是把一个未穷举的否定当结论。
  **先 grep 该 runbook 现有结构**，就近插入，不新开顶层章节。

## 5. 验证（Evidence Floor）

以下每一条都必须**实跑并贴出输出**，不得以论证替代测量。
issue #1660 未给 `Verification:` 字段，本节由本 change 自行裁定。

- [x] 5.1 `uv run pytest -q tests/test_object_store_forcing.py tests/test_object_store_forcing_real_disk.py`
  —— 全绿；新增用例数与名称列进 PR。
- [x] 5.2 `uv run pytest -q $(grep -rl object_store_forcing tests/)` —— 全绿。
  **改了被多个 display 套件共用的读入口，此项为必跑**，不得用 `-k` 替代
  （消费方覆盖分散在 `test_direct_grid_display_cutover_b4_leak.py`、
  `test_direct_grid_display_cutover_history.py`、
  `test_direct_grid_display_cutover_model_resolution.py`、
  `test_forecast_api_met_station_series.py`、
  `test_node27_autopipeline_handoff.py` 里）。
- [x] 5.3 变异矩阵，逐个给出**实测**红/绿（凡填「预期红」而未实测的格子必须标明是推断）：

  | # | 变异体 | 应由哪条转红 |
  |---|---|---|
  | M1 | 重试上限改为 1（等价于不重试） | 3.1 |
  | M2 | 重试条件放宽到全部 `SafeFilesystemError` | 3.3 |
  | M3 | 重试判据改成 message 子串匹配 | 3.4a + 3.4b |
  | M4 | 重试范围从「只包 open」扩到「包住整个 parse」（判据不变） | **3.8**（3.5 杀不死它——见 3.8 说明） |
  | M5 | 耗尽后改抛新错误类型 / 改状态码 | 3.2 |
  | M6 | `parse_reason` 改回无条件 `_public_error_reason` | 3.2 |
  | M7 | 非 identity 错误的 `parse_reason` 也加 `concurrent-replace` 前缀 | 3.3 |
  | M8 | `FileNotFoundError` 也纳入重试 | 3.6 |
  | M9 | 在重试循环里插入 `time.sleep` | 3.7 |
  | M10 | helper 忽略 `attempts` 入参、硬用模块常量 | 3.1 + 3.2 |

- [x] 5.4 `uv run ruff check $(git ls-files '*.py')` —— clean。
  （**不要跑 `uv run ruff check .`**，会命中本地未跟踪的 `skills/` 工具。）
- [x] 5.5 `openspec validate station-forcing-csv-concurrent-replace-retry --strict --no-interactive` —— valid。
- [ ] 5.6 按 `CLAUDE.md` 的 oracle 路由，5.1/5.2 在 **node-27** 上复跑一遍并贴 receipt。
  **本 change 不欠 node-27 C1-C4 live receipt**：改动落在共享库的读路径语义上，
  不涉及 display 生产化、只读边界或 cross-plane identity（`docs/runbooks/node-27-bringup-checklist.md`
  C1-C4 的触发条件），evidence 以真机 pytest 为准。
  拓扑侧的 receipt（读方本地 ext4 / 写方 NFS 客户端、跨客户端 replace 后 fd 仍完整读出）
  已在 design D8 记录，无需重跑。
