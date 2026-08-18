## 1. Implementation

- [ ] 1.1 `BasinsPackageError` 扩 `details` 形参 + `to_payload()` 合并（与
      `BasinsRegistryImportError:42-64` 同形；details None 时不合并任何键）
- [ ] 1.2 `basins_registry_import.py:218-224` raise 点传四键 details
      （status/missing/invalid/unreadable，取值 `model.get(...) or []`，status 原值）
- [ ] 1.3 `basins_package.py:538-545` raise 点传同一组四键 details
- [ ] 1.4 键名不新造别名：与 `publish_scheduler_file_registry.py:675` 前三键逐字一致，
      第四键与 discovery payload（#1552）逐字一致

## 2. Tests

- [ ] 2.1 两通道 payload 断言：拒收时 `to_payload()` 含四键且取值来自 model record
- [ ] 2.2 IC 头部畸形端到端：2-token `*.cfg.ic` → discovery 判 invalid → package/import
      拒收 payload 的 `invalid_required_files` 里能看到该文件名（扩
      `tests/test_basins_package_publication.py:1059` 附近既有 `BASINS_MODEL_NOT_PUBLISHABLE`
      断言点）
- [ ] 2.3 reingest 透传：`BasinsReingestError.to_payload()` 含同一组拒因键
      （`tests/test_basins_reingest.py`）
- [ ] 2.4 向后兼容锁：既有键一个不删；`BasinsPackageError` 不传 details 的既有
      raise 点 payload 逐字节不变
- [ ] 2.5 unreadable 第四键：unreadable-required-file 几何（#1552 三态）→ 拒收 payload
      `unreadable_required_files` 含该文件（status=partial 走 not-publishable 分支）

## 3. Verification

- [ ] 3.1 红证：2.1/2.2/2.3/2.5 新断言改动前红（payload 无 cause 键）
- [ ] 3.2 uv run pytest -q tests/test_basins_package_publication.py
      tests/test_basins_registry_import.py tests/test_basins_reingest.py
- [ ] 3.3 uv run ruff check workers tests
- [ ] 3.4 openspec validate basins-refusal-cause-details --strict --no-interactive
- [ ] 3.5 merge 后 node-27 oracle receipt：3.2 套件，记入 #1432
