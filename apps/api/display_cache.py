"""display_readonly 进程内 TTL 缓存 + 自预热（目录类只读端点）。

背景：runs、layers、basins 等展示目录端点由只读 display API 高频访问；
目录数据节奏为小时级 cycle，同一匿名请求高度重复。

机制（display_readonly 角色专属，其它角色直通）：
- 新鲜（< TTL 60s）：直接命中。
- 过期但 < STALE_MAX 10min：先回 stale 不阻塞访客，预热线程负责刷新
  （stale-while-revalidate；展示数据小时级节奏下 10min 内陈旧诚实可接受）。
- 超过 STALE_MAX：阻塞重算（真冷路径，仅进程刚启动或长期无人访问后出现）。
- 自预热：记录最近访问的目录 GET path，后台线程每 45s 经 ASGI 回放
  （带 force-refresh 头旁路缓存），保持热 key 常新。

边界（honest）：缓存的是 store 层 payload（不含 request_id 信封）；
根治（目录查询索引与覆盖物化）见后端慢查询专项。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request

DISPLAY_CATALOG_TTL_SECONDS = 60.0
DISPLAY_CATALOG_STALE_MAX_SECONDS = 600.0
DISPLAY_CATALOG_WARM_INTERVAL_SECONDS = 45.0
DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS = 1800.0
DISPLAY_CACHE_FORCE_REFRESH_HEADER = "x-nhms-cache-warm"
_MAX_ENTRIES = 256

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}
# key -> (带 query 的请求 path, 最近访问时刻)；预热线程按活跃窗口回放。
_hot_paths: dict[str, tuple[str, float]] = {}
_warmer_started = False


def _display_readonly(request: Request) -> bool:
    config = getattr(getattr(getattr(request, "app", None), "state", None), "runtime_config", None)
    return bool(getattr(config, "display_readonly", False))


def _force_refresh(request: Request) -> bool:
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    return headers.get(DISPLAY_CACHE_FORCE_REFRESH_HEADER) == "refresh"


def _record_hot_path(request: Request, key: str) -> None:
    url = getattr(request, "url", None)
    if url is None:
        return
    query = getattr(url, "query", "") or ""
    path = f"{url.path}?{query}" if query else str(url.path)
    with _lock:
        if len(_hot_paths) >= _MAX_ENTRIES:
            _hot_paths.clear()
        _hot_paths[key] = (path, time.monotonic())


def _store_value(key: str, value: Any) -> None:
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _store.clear()
        _store[key] = (time.monotonic(), value)


def display_catalog_cached(request: Request, key: str, loader: Callable[[], Any]) -> Any:
    """display_readonly 下按 key 缓存 loader 结果（TTL + stale-while-revalidate）。"""
    if not _display_readonly(request):
        return loader()
    if _force_refresh(request):
        value = loader()
        _store_value(key, value)
        return value
    _record_hot_path(request, key)
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None:
            age = now - hit[0]
            if age < DISPLAY_CATALOG_STALE_MAX_SECONDS:
                # 新鲜直接命中；过期但未超 stale 上限也先回 stale（预热线程负责刷新），
                # 不让访客阻塞在 12s 级慢查询上。
                return hit[1]
    value = loader()
    _store_value(key, value)
    return value


def start_display_catalog_warmer(app: FastAPI) -> threading.Thread | None:
    """display_readonly 启动自预热线程（进程级单例；daemon，不阻塞退出）。"""
    global _warmer_started
    with _lock:
        if _warmer_started:
            return None
        _warmer_started = True
    thread = threading.Thread(target=_warm_loop, args=(app,), daemon=True, name="display-catalog-warmer")
    thread.start()
    return thread


def _warm_loop(app: FastAPI) -> None:
    while True:
        time.sleep(DISPLAY_CATALOG_WARM_INTERVAL_SECONDS)
        now = time.monotonic()
        with _lock:
            targets = [
                path
                for (path, last_access) in _hot_paths.values()
                if now - last_access < DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS
            ]
        if not targets:
            continue
        try:
            asyncio.run(_replay_targets(app, targets))
        except Exception:  # noqa: BLE001 - 预热失败保留 stale，下一轮再试
            continue


async def _replay_targets(app: FastAPI, targets: list[str]) -> None:
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://display-cache-warmer") as client:
        for path in targets:
            try:
                await client.get(path, headers={DISPLAY_CACHE_FORCE_REFRESH_HEADER: "refresh"}, timeout=120.0)
            except Exception:  # noqa: BLE001 - 单 path 失败不影响其余预热
                continue


def clear_display_catalog_cache() -> None:
    """测试钩子：清空缓存与热 path 记录。"""
    with _lock:
        _store.clear()
        _hot_paths.clear()
