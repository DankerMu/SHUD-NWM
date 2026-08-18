## 1. Implementation
- [x] 1.1 fixture 钉 source_backends=(GFS_NOMADS_BACKEND,)（含 hermetic 注释）
- [x] 1.2 encode_test_netcdf4_bundle（多变量 data_vars + GRIB_shortName attrs）
- [x] 1.3 fixture 按 bundle.variables 编码；f000 计数期望修真（−2 当 0∈hours）
## 2. Verification
- [x] 2.1 `NHMS_RUN_E2E=1 uv run pytest tests/test_e2e.py -m e2e -q` → 2 passed（master 上 2 failed，红证=复现记录）
- [x] 2.2 无 marker 门时 2 passed, 2 skipped（收集路径不变）
- [x] 2.3 邻域回归：tests/test_canonical_converter.py + tests/test_gfs_adapter.py → 110 passed
- [x] 2.4 uv run ruff check 通过
