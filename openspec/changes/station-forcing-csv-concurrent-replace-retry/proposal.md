# station-forcing-csv-concurrent-replace-retry

## Why

display API 的 station-series 直读路径对「写方是原子 replace」这件事不设防，一次瞬时竞态就把对外接口打成 HTTP 500。

写读双方共用一块 NFS（node-22 producer 写 `/ghdc/data/nwm/`，node-27 display API 从 `/home/ghdc/nwm/object-store` 读，拓扑见 `docs/runbooks/object-store-forcing-series-read.md:65-82`）：

- **写方**：`workers/forcing_producer/producer.py:2101-2105` 对每个 `shud/<station>.csv` 走
  `write_bytes_atomic` → `packages/common/object_store.py:207-214` → `atomic_write_bytes_no_follow`
  （`packages/common/safe_fs.py:138-208`），以 `os.replace` **换入新 inode**。
- **读方**：`packages/common/object_store_forcing.py:503` 走 `open_file_no_follow`
  （`safe_fs.py:277-313`）。该原语在 `:285` 的 pre-open stat 与 `:303` 的 post-open fstat 之间比对
  `(st_dev, st_ino)`；窗内被 `os.replace` 换掉就在 `safe_fs.py:305` 抛
  `SafeFilesystemError(kind="identity_changed")`。
- **落点**：`object_store_forcing.py:551-556` 的
  `except (OSError, SafeFilesystemError, ValueError)` 把它吞成
  `StationForcingFileMalformedError`（HTTP 500 `STATION_FORCING_FILE_MALFORMED`），
  且该 handler **不读 `error.kind`**——正常并发写与真实内容损坏在 API 上完全不可区分。

`safe_fs.py:10-33` 的类文档已经明写这个 kind 是 **consistency refusal 而非 symlink 防线**，并明写
「拥有 concurrent-replace 关系的调用方可以用有界重试吸收它」；#1595/#1600 已按这条给 journal
chokepoint 做了实现（`services/orchestrator/file_orchestration_journal.py:4618-4649`），
但 tasks 4.2 明确把普查确证的**非 journal 调用点**另立 issue——本 change 就是其中的 station CSV 直读点。

## What Changes

- `packages/common/object_store_forcing.py` 的 `_read_csv_lines`：把 **no-follow open 这一步**
  放进有界重试；重试条件是 `SafeFilesystemError` **且** `error.kind == "identity_changed"`，
  按字段选择，**绝不匹配消息文本**。
- 重试耗尽后仍 fail-closed 抛 `STATION_FORCING_FILE_MALFORMED`（HTTP 500 不变），
  但 `details.parse_reason` 以 `concurrent-replace` 前缀标注，使运维能把并发替换与真实损坏分开。
- 重试次数为模块常量、可注入；间隔参数默认 **0（不 sleep）**——见 design D5 的偏离说明。
- 不改 `safe_fs.py` 的身份比对本身（#1600 已裁定不弱化），不改 producer 写侧，
  不改 journal chokepoint，不做跨进程 flock。

## Impact

- Affected specs: `object-store-station-series-read`
- Affected code: `packages/common/object_store_forcing.py`（唯一实现文件）、
  `tests/test_object_store_forcing.py`（新增确定性用例）
- 行为面：`STATION_FORCING_FILE_MALFORMED` 的**触发条件收窄**（并发替换不再首次即 500），
  HTTP 状态码、错误码、`details` 键集均不变；`FileNotFoundError` → 404 路径完全不动。
