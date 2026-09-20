"""#2078: catalog-cache admission and layers post-slicing under the display role.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api import display_cache, main
from apps.api.routes import hydro_display, hydro_display_catalog
from tests.hydro_display_mvt_helpers import (
    _CYCLE,
    _full_coverage_rows,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _NationalDiscoverySession,
)

# --- #2078：display 角色下的目录缓存准入与 layers 后切片 -------------------------


def _display_role_catalog_app(monkeypatch: Any, session: Any, tmp_path: Path) -> Any:
    """真正会缓存的 `/api/v1/layers` 应用：display_readonly 角色，不替换缓存函数。

    别的用例用 `main.create_app()`（DEV_MONOLITH）+ 替身缓存，那条路径上
    `display_catalog_cached` 直通 loader，缓存 key 与准入谓词根本不被执行。
    角色 env 显式交给 `create_app`（照 `tests/test_precip_overlay.py` 的建法），
    这样 display 边界检查只看见这三个变量。`create_app` 起的预热线程由 conftest 的
    autouse fixture 停掉，缓存也在每个用例之间清空。
    """
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_latest"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a"))
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display_catalog, "_mvt_live_postgis_enabled", lambda _s: False)
    app = main.create_app(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "OBJECT_STORE_ROOT": str(object_store_root),
        }
    )
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    return app


def _three_layers() -> list[Any]:
    return [
        hydro_display.Layer(
            layer_id=f"layer-{index}",
            layer_name=f"Layer {index}",
            layer_type="vector",
            variables=[f"var-{index}"],
            metadata={"index": index},
        )
        for index in range(3)
    ]


def test_layers_pagination_is_applied_after_the_cache(monkeypatch: Any, tmp_path: Path) -> None:
    """三次不同分页只建一次目录，key 不含 limit/offset，越界 offset 是不落 DB 的空页。"""
    layers = _three_layers()
    builds: list[dict[str, Any]] = []

    def _catalog(_session: Any, **kwargs: Any) -> list[Any]:
        builds.append(kwargs)
        return layers

    monkeypatch.setattr(hydro_display, "_default_layer_catalog", _catalog)
    app = _display_role_catalog_app(monkeypatch, _NationalDiscoverySession([]), tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            first_page = client.get("/api/v1/layers", params={"offset": 0, "limit": 2})
            second_page = client.get("/api/v1/layers", params={"offset": 1, "limit": 1})
            beyond = client.get("/api/v1/layers", params={"offset": 5})
    finally:
        app.dependency_overrides.clear()

    # 逐字节 oracle：切片必须等于「完整目录的 model_dump 列表再切片」。
    dumped = [layer.model_dump() for layer in layers]
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["data"] == dumped[0:2]
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["data"] == dumped[1:2]
    assert beyond.status_code == 200, beyond.text
    assert beyond.json()["data"] == dumped[5:105] == []

    assert len(builds) == 1, builds
    assert list(display_cache._store) == ["layers:None"]
    assert display_cache._store["layers:None"][1] == dumped


def test_a_literal_run_id_none_does_not_fold_into_the_national_layer_cache_entry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """`?run_id=None` 是通过标识符校验的普通字面量，不是「没给 run_id」。

    改前红：key 用裸 `f"layers:{run_id}"` 插值，字面量 `None` 与国家级请求同 key ——
    公网可以拿到国家级目录（本用例里该 run 根本不存在，应当 404），并顺手把国家级
    条目的热 path 改写成 `/api/v1/layers?run_id=None`，让预热线程每 tick 回放它。
    """
    monkeypatch.setattr(hydro_display, "_default_layer_catalog", lambda _session, **_kwargs: _three_layers())
    app = _display_role_catalog_app(monkeypatch, _NationalDiscoverySession([]), tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            national = client.get("/api/v1/layers")
            assert national.status_code == 200, national.text
            assert display_cache._hot_paths["layers:None"][0] == "/api/v1/layers"

            literal = client.get("/api/v1/layers", params={"run_id": "None"})
    finally:
        app.dependency_overrides.clear()

    # `_require_display_ready` -> `_run_row` 找不到该 run：未知 run 就是 404，
    # 而不是别人的目录。
    assert literal.status_code == 404, literal.text
    assert literal.json()["error"]["code"] == "RUN_NOT_FOUND"
    # loader 抛在写缓存之前，所以这个 key 什么也没留下；国家级条目的热 path 不被劫持。
    assert "layers:'None'" not in display_cache._store
    assert display_cache._hot_paths["layers:None"][0] == "/api/v1/layers"


def test_a_literal_run_id_none_does_not_fold_into_the_national_valid_times_entry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """同一族缺陷的 valid-times 面：`?run_id=None` 不是「没给 run_id」。

    改前红：key 用裸 `f"valid-times:{layer_id}:{requested_run_id}:…"`，字面量 `None`
    折叠进国家级条目 —— 公网拿到的是国家级 valid_times（该 run 根本不存在，应当 404），
    国家级条目的热 path 还被改写成一个必然 404 的 URL：预热回放只会抛错，条目再也
    刷不新（refresh starvation）。
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _display_role_catalog_app(monkeypatch, session, tmp_path)
    national_key = "valid-times:discharge:None:None:None"
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            national = client.get("/api/v1/layers/discharge/valid-times")
            assert national.status_code == 200, national.text
            assert national.json()["data"]["valid_times"]
            assert display_cache._hot_paths[national_key][0] == "/api/v1/layers/discharge/valid-times"

            literal = client.get("/api/v1/layers/discharge/valid-times", params={"run_id": "None"})
    finally:
        app.dependency_overrides.clear()

    # `_require_display_ready` -> `_run_row` 找不到该 run：未知 run 就是 404。
    assert literal.status_code == 404, literal.text
    assert literal.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert "valid-times:discharge:'None':None:None" not in display_cache._store
    assert display_cache._hot_paths[national_key][0] == "/api/v1/layers/discharge/valid-times"


def test_empty_valid_times_are_not_cached_while_a_covered_cycle_is(monkeypatch: Any, tmp_path: Path) -> None:
    """交集外的 cycle 是客户端可控的无界 key 维度：空列表照常 200，但不留缓存条目。"""
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _display_role_catalog_app(monkeypatch, session, tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            uncovered = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-01T00:00:00Z"},
            )
            covered = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
            )
    finally:
        app.dependency_overrides.clear()

    assert uncovered.status_code == 200, uncovered.text
    assert uncovered.json()["data"]["valid_times"] == []
    uncovered_key = "valid-times:discharge:None:'gfs':'2026-09-01T00:00:00Z'"
    assert uncovered_key not in display_cache._store
    assert uncovered_key not in display_cache._hot_paths

    assert covered.status_code == 200, covered.text
    covered_valid_times = covered.json()["data"]["valid_times"]
    assert covered_valid_times
    covered_key = "valid-times:discharge:None:'gfs':'2026-09-02T12:00:00Z'"
    assert display_cache._store[covered_key][1]["valid_times"] == covered_valid_times
    assert covered_key in display_cache._hot_paths
