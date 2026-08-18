## 1. Implementation

- [x] 1.1 扩写 test_governance_config_and_receipt_carry_no_archive_root：
      打桩 collect_filesystem/collect_postgres/collect_systemd，
      config_from_args(build_parser().parse_args([]))，build_receipt(config)，
      断言 "archive_root" not in receipt

## 2. Verification

- [x] 2.1 `uv run pytest -q tests/test_node27_resource_governance.py` 全绿
- [x] 2.2 红证：临时在 build_receipt 返回 dict 注入 archive_root 键 → 测试红
- [x] 2.3 `uv run ruff check .` 通过
- [x] 2.4 未改动 scripts/node27_resource_governance.py（git diff 无该文件）
