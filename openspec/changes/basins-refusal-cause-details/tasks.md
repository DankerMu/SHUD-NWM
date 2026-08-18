## 1. Implementation

- [x] 1.1 `BasinsPackageError` 扩 `details` 形参 + `to_payload()` 合并（与
      `BasinsRegistryImportError:42-64` 同形；details None 时不合并任何键）
- [x] 1.2 `basins_registry_import.py:218-224` raise 点传四键 details
      （status/missing/invalid/unreadable，取值 `model.get(...) or []`，status 原值）
- [x] 1.3 `basins_package.py:538-545` raise 点传同一组四键 details
- [x] 1.4 键名不新造别名：与 `publish_scheduler_file_registry.py:708-710`（键名真值点）前三键逐字一致，
      第四键与 discovery payload（#1552）逐字一致

## 2. Tests

- [x] 2.1 两通道 payload 断言：拒收时 `to_payload()` 含四键且取值来自 model record
- [x] 2.2 IC 头部畸形端到端：2-token `*.cfg.ic` → discovery 判 invalid → package/import
      拒收 payload 的 `invalid_required_files` 里能看到该文件名。**新建 sibling 测试**
      （`_make_valid_model` 后覆写 `<name>.cfg.ic` 为 2-token 头部），不得改造
      `tests/test_basins_package_publication.py:1030-1062` 既有用例（其 missing 覆盖
      保留，作 2.1 的 missing 半边）
- [x] 2.3 reingest 透传：`BasinsReingestError.to_payload()` 含同一组拒因键
      （`tests/test_basins_reingest.py`；驱动 **import 腿 `:154-161`**（直取
      `error.details`）或 package 腿 `:117-125`，具名记录驱动的是哪条腿；新用例
      **不得**标 `@pytest.mark.integration`，否则本地红证跑不了）
- [x] 2.4 向后兼容锁：既有键一个不删；`BasinsPackageError` 不传 details 的既有
      raise 点 payload 逐字节不变
- [x] 2.5 unreadable 第四键：unreadable-required-file 几何（#1552 三态）→ 拒收 payload
      `unreadable_required_files` 含该文件（status=partial 走 not-publishable 分支）。
      注入面：`monkeypatch.setattr(basins_discovery, "_sha256", fake)`（同
      `tests/test_basins_discovery.py:615-624` 口径），**禁用**全局 `Path.stat` patch
      （#1552 终审教训）；refusal 测试无需 object-store env（`_find_publishable_model`
      先于 `_object_store_from_env`）

## 3. Verification

- [x] 3.1 红证：2.1/2.2/2.3/2.5 新断言改动前红（payload 无 cause 键）
- [x] 3.2 uv run pytest -q tests/test_basins_package_publication.py
      tests/test_basins_registry_import.py tests/test_basins_reingest.py
- [x] 3.3 uv run ruff check .（issue AC 原形；本地已知 untracked 投影 E501 例外按会话惯例记录）
- [x] 3.4 openspec validate basins-refusal-cause-details --strict --no-interactive
- [ ] 3.5 merge 后 node-27 oracle receipt：3.2 套件，记入 #1432
