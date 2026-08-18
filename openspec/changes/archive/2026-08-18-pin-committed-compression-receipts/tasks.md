## 1. Implementation
- [x] 1.1 前缀 glob 集合 + 参数化 jsonschema.validate
- [x] 1.2 glob 非空计数守卫（≥4，防 glob 写错空跑假绿）
## 2. Verification
- [x] 2.1 uv run pytest -q tests/test_node27_timeseries_compression.py → 115 passed
- [x] 2.2 负向红证：临时给 schema 1.0 分支加 required → 2 failed（两份 1.0 receipt）
- [x] 2.3 uv run ruff check 通过；receipt/schema 文件未动
