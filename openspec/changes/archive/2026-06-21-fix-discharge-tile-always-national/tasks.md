## 1. Backend single-point fix

- [ ] 1.1 在 `apps/api/routes/flood_alerts.py:2302`（`_default_layer_catalog` 函数体内）把 `national_discharge = national and layer_id == "discharge"` 改为 `national_discharge = layer_id == "discharge"`，加 inline 注释指出 spec 不变量：discharge 永远 national，与 caller 是否传 `run_id` 解耦。
- [ ] 1.2 验证 `_default_layer_catalog` 函数签名（`national: bool = False`）保持不变：`grep -n "def _default_layer_catalog" apps/api/routes/flood_alerts.py` 应仍是同一行，签名未动。

## 2. Regression tests (in `tests/test_flood_alerts_api.py`)

- [ ] 2.1 在 `tests/test_flood_alerts_api.py` 现有 `_default_layer_catalog` 测试块旁（参考既有 `test_layer_metadata_cache_identity_changes_when_run_updated_at_changes` 及邻近 `test_layer_metadata_*` 系列）新增 `test_layers_catalog_discharge_always_national`：用与既有同形 FakeSession + FakeRun fixture，对**带** `run_id` 调用 `_default_layer_catalog` 断言 discharge 条目：(a) `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'`、(b) `metadata.required_placeholders` 不含 `'run_id'`、(c) `metadata.maplibre_source_layer === 'hydro'`、(d) `'basin_id' in metadata.properties`。**覆盖** spec scenario *Run-scoped `/api/v1/layers?run_id=<X>` catalog*。
- [ ] 2.2 同一文件新增 `test_layers_catalog_flood_warning_remain_run_scoped`：带 `run_id` 调用时断言 flood-return-period / warning-level 模板含 `'{run_id}'`、river-network 模板 === `'/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf'`。**覆盖** spec scenario *Flood-return-period and warning-level remain run-scoped*（防止本次后端改动误伤）。
- [ ] 2.3 同一文件新增 `test_layers_catalog_discharge_cache_identity_run_agnostic`：分别用 `run_id=None` 和 `run_id='fake_run'` 调用 `_default_layer_catalog`，断言：(a) 两次返回的 discharge 条目 `metadata.source_refs == {}` 且 `metadata.version` 字符串字节相同（**覆盖** *Discharge catalog cache identity is run-agnostic*）；(b) 在 `run_id=None` 分支上同时断言 `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'`、`metadata.required_placeholders == ['valid_time','z','x','y']`、`metadata.maplibre_source_layer == 'hydro'`、`'basin_id' in metadata.properties`（**覆盖** *Runless `/api/v1/layers` catalog*）。
- [ ] 2.4 跑 `uv run pytest -q tests/test_flood_alerts_api.py -k "layers_catalog or layer_metadata"`，PASS。
- [ ] 2.5 跑 `uv run ruff check apps/api/routes/flood_alerts.py tests/test_flood_alerts_api.py`，clean。

## 3. Spec compliance + 文档 drift

- [ ] 3.1 `openspec validate fix-discharge-tile-always-national --strict --no-interactive` PASS。
- [ ] 3.2 `docs/runbooks/display-readonly-live-mvt.md` 若枚举当前 catalog 层的模板，需更新 discharge 行（hydro-national），保持 catalog 实拍证据与新不变量一致。其余 runbook 暂无 discharge tile URL 强假设。
- [ ] 3.3 不动 OpenAPI schema（已确认 `openapi/nhms.v1.yaml:4699-4701` `tile_url_template` 是 nullable string 无 enum）。本任务存档以表已确认。

## 4. CI 期望 & node-27 live receipt（merge 后产出）

- [ ] 4.1 PR 标为 ready 触发全量 CI：`unit-test` + `real-db-integration` 均需 PASS。本变更是逻辑层单点改动 + 单测扩展，`real-db-integration` **期望 PASS without 任何 DB-specific 新断言**（不引入 real-DB 依赖；real-DB list_models 覆盖 follow-up 见 [#598](https://github.com/DankerMu/93/issues/598)）。
- [ ] 4.2 `ssh -p 32099 nwm@210.77.77.27 'cd /home/nwm/NWM && git pull --ff-only'` → restart uvicorn（确保 source `infra/env/display.env`，参 [#597](https://github.com/DankerMu/SHUD-NWM/issues/597)）。
- [ ] 4.3 跑下面这段（一段脚本同时验证 runless + run-scoped），结果连同两侧响应原文写入 `docs/runbooks/receipts/discharge-tile-always-national-<date>.md`：

```bash
ssh -p 32099 nwm@210.77.77.27 'python3 - <<PY
import json, urllib.request
def fetch(url):
    with urllib.request.urlopen(url) as r: return json.load(r)
runs = fetch("http://127.0.0.1:8080/api/v1/runs?limit=1&status=published")
latest = runs["data"]["items"][0]["run_id"]
for label, url in [("runless", "http://127.0.0.1:8080/api/v1/layers"),
                   ("run_scoped", f"http://127.0.0.1:8080/api/v1/layers?run_id={latest}")]:
    res = fetch(url)
    by_id = {it["layer_id"]: it for it in res["data"]}
    d = by_id["discharge"]["metadata"]
    assert d["tile_url_template"] == "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf", (label, d["tile_url_template"])
    assert "{run_id}" not in d["tile_url_template"], (label, d["tile_url_template"])
    f = by_id["flood-return-period"]["metadata"]
    assert "{run_id}" in f["tile_url_template"], (label, f["tile_url_template"])
    print(label, "OK", "discharge=", d["tile_url_template"], "flood=", f["tile_url_template"])
PY'
```

**覆盖** spec scenarios *Runless* + *Run-scoped* + *Flood-return-period and warning-level remain run-scoped*。
- [ ] 4.4 浏览器实拍（确定性）：DevTools Network 面板录 maplibre `hydro` source 实际请求的 tile URL（应为 hydro-national），同时在 console 调 `map.getSource('hydro').getTileUrlTemplate()` 读 URL 字符串入 receipt；缩放至甘肃黑河流域，点击 ≥1 个 heihe 河段触发曲线弹窗，截图入 receipt。**覆盖** spec scenario *Frontend enrichment phase does not downgrade discharge*。
- [ ] 4.5 不跑 `mocked-regression-chromium`（按 CLAUDE.md，该 job 已从自动 CI 移出，是历史 mocked 视觉证据，不是 node-27 live display proof）。本 receipt 走 4.4 的 live 路径即可。

## 5. PR / merge hygiene

- [ ] 5.1 PR body 引用本 change（`Closes #601` / `Part of #600`），附 Chinese 工作总结：根因 / 修复 / 验证 / 残留 / 已知风险。
- [ ] 5.2 review-loop log append 一行（`docs/review-loop-log.jsonl`）记录本 PR 的 round 数 / gate_net_catch / verifier 结果。
- [ ] 5.3 OpenSpec archive：合并后 `openspec archive fix-discharge-tile-always-national --yes`，把 ADDED requirement 落到 `openspec/specs/overview-data-contracts/spec.md`，MODIFIED requirement 落到 `openspec/specs/mvt-tile-contract/spec.md`。
