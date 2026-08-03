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
# 预热线程停止信号 + 线程句柄（stop 钩子用；生产进程不调用，线程活到进程退出）。
_stop_event = threading.Event()
_warmer_thread: threading.Thread | None = None


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
    global _warmer_started, _warmer_thread
    with _lock:
        if _warmer_started:
            return None
        _warmer_started = True
        _stop_event.clear()
        thread = threading.Thread(target=_warm_loop, args=(app,), daemon=True, name="display-catalog-warmer")
        _warmer_thread = thread
    thread.start()
    return thread


def stop_display_catalog_warmer(timeout: float = 5.0) -> bool:
    """停止自预热线程并 join；成功返回 True，join 超时返回 False（不重置状态）。

    契约：

    - 置位 `_stop_event` 结束线程当前的 `wait`，随后在 `timeout` 内 join
      名为 ``display-catalog-warmer`` 的线程；join 成功（或本来就没有线程）
      后才重置 `_warmer_started` / `_warmer_thread` 并 clear 事件，返回 True。
      从未启动过时是 no-op 且返回 True；成功停止后可再次 start 出新线程。
    - join 超时返回 False 且**不重置任何状态**（事件保持置位，卡住的线程仍会
      在下一次 wait 处退出）——重置会放行第二个静默线程，比响一点更糟。
    - 锁纪律（死锁陷阱）：句柄只在**短暂**持有 `_lock` 时读取，**绝不**在持锁
      期间 join——预热线程的回放路径会取同一把锁（`_store_value` / 热 path
      快照），持锁 join 必然僵到超时。同形先例见
      `services/orchestrator/scheduler_lease.py:85-117`。
    - 接受的级联（by design，别误判成 stop 坏了）：线程若卡在一次 ASGI 回放里
      （`_replay_targets` 每 path 的 httpx 超时是 120s），本次以及**之后每一次**
      teardown 都会响，直到 ``display-catalog-warmer`` 线程自己退出为止；真正
      要查的是那个线程在回放什么，而不是 stop()。
    - 生产进程不调用本钩子：display_readonly 下预热线程本就该活到进程退出。
    """
    global _warmer_started, _warmer_thread
    _stop_event.set()
    with _lock:
        thread = _warmer_thread
    if thread is not None:
        thread.join(timeout)
        if thread.is_alive():
            return False
    with _lock:
        _warmer_started = False
        _warmer_thread = None
        _stop_event.clear()
    return True


def _warm_loop(app: FastAPI) -> None:
    # 用 Event.wait 而不是 time.sleep 计时：可被 stop 立即打断，且不受测试对
    # time.sleep 的 no-op monkeypatch 影响；间隔每轮从模块全局读取（便于压缩）。
    while not _stop_event.wait(DISPLAY_CATALOG_WARM_INTERVAL_SECONDS):
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
