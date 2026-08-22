# Tasks — station-forcing-csv-concurrent-replace-retry

行号锚定 `master` @ `57a14098`。**每一处行号引用在动手前必须按符号名 grep 复核**——
issue #1660 正文的行号锚在更早的 commit，可能已被后续合并推移。

## 1. 重试原语（`packages/common/object_store_forcing.py`）

- [ ] 1.1 新增模块常量，命名对齐 journal 先例
  （`services/orchestrator/file_orchestration_journal.py:153`
  `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`），语义同为**总尝试次数（含首次）= 3**。
  在常量旁写明「3 = 首次 + 2 次重试」，避免被后人读成「重试 3 次」。
- [ ] 1.2 把 `_read_csv_lines`（def `:490`）里的
  `open_file_no_follow(expected_path, containment_root=object_store_root)`（`:503`）
  抽成一个有界重试的私有 helper，**只包 open**（design D1）。
  helper 签名带 `attempts: int = <常量>` 与 `retry_interval_seconds: float = 0.0` 两个可注入参数，
  并把这两个参数从 `_read_csv_lines` 一路透传上来，便于测试注入。
  `retry_interval_seconds` 为 0 时**不得调用 `time.sleep`**（design D5）。
- [ ] 1.3 重试判据**必须**是
  `isinstance(error, SafeFilesystemError) and error.kind == "identity_changed"`。
  其余 kind（`unsafe` / `io` / `indeterminate`）与非 `SafeFilesystemError` 异常
  **首次即向外抛**，不进循环下一轮（design D2）。
  **禁止**任何形式的消息文本匹配（`in`、`startswith`、正则）作为判据。
- [ ] 1.4 尝试耗尽时**原样抛出最后一次的异常**，由既有
  `except (OSError, SafeFilesystemError, ValueError)`（`:551`）接住 —— 不新增错误类型、
  不改 HTTP 状态码与错误码（design D4）。
- [ ] 1.5 parse 段（`:504-535` header/行读取/`_parse_csv_header`）**保持在重试循环之外**，
  只执行一次。fd 的 `finally: os.close`（`:536-541`）保持原语义：
  每次成功 open 只对应一次 close，重试循环里失败的 open **不产生**需要关闭的 fd
  （`open_file_no_follow` 在 `safe_fs.py:307-309` 的 `except Exception:` 里已自行 close）。

## 2. 错误 reason 的 kind 映射（design D4）

- [ ] 2.1 `:551-556` 的 malformed 分支：`parse_reason` 不再无条件走
  `_public_error_reason(error)`（def `:692`），改为先判 kind。
- [ ] 2.2 `identity_changed` 耗尽时，`parse_reason` 以稳定 token **`concurrent-replace`** 开头，
  其后附 `_public_error_reason(error)` 的脱敏文本。token 与后文之间用固定分隔符，
  使消费方可用 `startswith` 判定而无需解析。
- [ ] 2.3 其余错误的 `parse_reason` **一字不改**，仍是 `_public_error_reason(error)` —— 
  既有 spec scenario `openspec/specs/object-store-station-series-read/spec.md:141-145`
  要求「preserve operator-useful error text」，不得被本改动削弱。
- [ ] 2.4 `FileNotFoundError` 分支（`:544-553` 附近，grep `StationForcingFileNotFoundError`）
  **完全不动**：不加重试、不改映射（design D3）。

## 3. 测试（`tests/test_object_store_forcing.py`）

用例形状参照 `tests/test_file_orchestration_journal_read_cache.py:863` 的先例：
在 **`object_store_forcing` 命名空间内** monkeypatch `open_file_no_follow`
（模块顶部 `:19` 是 `from packages.common.safe_fs import ... open_file_no_follow`，
因此必须 patch `packages.common.object_store_forcing.open_file_no_follow`，
patch `safe_fs` 原处**不会生效**）。

- [ ] 3.1 重试成功分支：前 `attempts-1` 次抛 `SafeFilesystemError(kind="identity_changed")`，
  最后一次委托真实 open，断言返回正常解析结果、无异常，且调用次数 == attempts。
- [ ] 3.2 耗尽 fail-closed 分支：每次都抛 `identity_changed`，断言抛
  `StationForcingFileMalformedError`、`status_code == 500`、`code == "STATION_FORCING_FILE_MALFORMED"`，
  且 `details["parse_reason"].startswith("concurrent-replace")`，调用次数 == attempts。
- [ ] 3.3 非 `identity_changed` 不重试：分别以 `kind="unsafe"` 与 `kind="io"` 抛，
  断言 open **只被调用一次**，且 `parse_reason` **不含** `concurrent-replace` 前缀。
- [ ] 3.4 判据不依赖文本（锁死 design D2）：
  (a) `kind="identity_changed"` 但 message 换成与现行原语完全不同的措辞 → **仍重试**；
  (b) `kind="unsafe"` 但 message 故意写成 `"Target file changed while being opened: /x"` → **不重试**。
- [ ] 3.5 parse 失败不重试（锁死 design D1）：open 正常成功，但磁盘上是坏内容
  （空文件 / nrow 不符 / blank row 任选其一，复用既有 fixture 构造），
  断言抛 malformed 且 open **只被调用一次**。
- [ ] 3.6 `FileNotFoundError` 不重试（锁死 design D3）：路径不存在，
  断言抛 `StationForcingFileNotFoundError`、open 只被调用一次。
- [ ] 3.7 默认不 sleep：monkeypatch `time.sleep` 断言**零次调用**（默认 interval=0）；
  另注入 `retry_interval_seconds > 0` 断言 sleep 被调用 `attempts-1` 次
  （证明参数确实接线，而非死代码）。

## 4. 文档

- [ ] 4.1 `docs/runbooks/object-store-forcing-series-read.md`：在读路径一节补一段，
  说明 `concurrent-replace` 这个 `parse_reason` 前缀的含义与排查动作
  （见到它 = 读到了 producer 的原子替换窗口且重试耗尽，不是文件损坏）。
  **先 grep 该 runbook 现有结构**，就近插入，不新开顶层章节。

## 5. 验证（Evidence Floor）

以下每一条都必须**实跑并贴出输出**，不得以论证替代测量。
issue #1660 未给 `Verification:` 字段，本节由本 change 自行裁定。

- [ ] 5.1 `uv run pytest -q tests/test_object_store_forcing.py tests/test_object_store_forcing_real_disk.py`
  —— 全绿；新增用例数与名称列进 PR。
- [ ] 5.2 `uv run pytest -q $(grep -rl object_store_forcing tests/)` —— 全绿。
  **改了被多个 display 套件共用的读入口，此项为必跑**，不得用 `-k` 替代
  （消费方覆盖分散在 `test_direct_grid_display_cutover_*` 与
  `test_forecast_api_met_station_series.py` 里）。
- [ ] 5.3 变异矩阵，逐个给出**实测**红/绿（凡填「预期红」而未实测的格子必须标明是推断）：

  | # | 变异体 | 应由哪条转红 |
  |---|---|---|
  | M1 | 重试上限改为 1（等价于不重试） | 3.1 |
  | M2 | 重试条件放宽到全部 `SafeFilesystemError` | 3.3 |
  | M3 | 重试判据改成 message 子串匹配 | 3.4a + 3.4b |
  | M4 | 重试范围从「只包 open」扩到「包住整个 parse」 | 3.5 |
  | M5 | 耗尽后改抛新错误类型 / 改状态码 | 3.2 |
  | M6 | `parse_reason` 改回无条件 `_public_error_reason` | 3.2 |
  | M7 | 非 identity 错误的 `parse_reason` 也加 `concurrent-replace` 前缀 | 3.3 |
  | M8 | `FileNotFoundError` 也纳入重试 | 3.6 |
  | M9 | 默认 interval 改为 >0 | 3.7 前半 |

- [ ] 5.4 `uv run ruff check $(git ls-files '*.py')` —— clean。
  （**不要跑 `uv run ruff check .`**，会命中本地未跟踪的 `skills/` 工具。）
- [ ] 5.5 `openspec validate station-forcing-csv-concurrent-replace-retry --strict --no-interactive` —— valid。
- [ ] 5.6 按 `CLAUDE.md` 的 oracle 路由，5.1/5.2 在 **node-27** 上复跑一遍并贴 receipt。
  **本 change 不欠 node-27 C1-C4 live receipt**：改动落在共享库的读路径语义上，
  不涉及 display 生产化、只读边界或 cross-plane identity（`docs/runbooks/node-27-bringup-checklist.md`
  C1-C4 的触发条件），evidence 以真机 pytest 为准。
