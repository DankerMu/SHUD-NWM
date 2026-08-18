## 1. Implementation
- [x] 1.1 _resolve_design_path helper（pending 优先 + archive glob 回退 + pytest.fail）
- [x] 1.2 三个 read_text 调用点切换
## 2. Tests
- [x] 2.1 pending 优先（tmp_path 双形态）
- [x] 2.2 archive 回退取最新
- [x] 2.3 双缺失 pytest.fail 带 "pending" 指引
## 3. Verification
- [x] 3.1 uv run pytest -q tests/test_node27_timeseries_retention.py 全绿
- [x] 3.2 uv run ruff check 通过
