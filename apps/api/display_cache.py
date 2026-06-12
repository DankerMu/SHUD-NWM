"""display_readonly 进程内 TTL 缓存（目录类只读端点）。

背景：runs 列表（flood_product_ready 过滤）与 layers 目录依赖对
flood.return_period_result（61M+ 行）的逐 run LATERAL 聚合，只读副本上单次
12-14s；展示端同一匿名请求高度重复、数据节奏为小时级 cycle。在
display_readonly 角色下按 key 缓存 60s，把刷新/并发访客的目录请求拉回亚秒。

边界（honest）：
- 仅 display_readonly 启用；compute_control / dev_monolith 直通（运维端要求
  即时一致，且测试桩 store 不应被跨用例缓存污染）。
- 缓存的是 store 层 payload（不含 request_id 信封），过期即整体重算；
  根治（partial index / 质量汇总物化）见后端慢查询专项。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request

DISPLAY_CATALOG_TTL_SECONDS = 60.0
_MAX_ENTRIES = 256

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}


def _display_readonly(request: Request) -> bool:
    config = getattr(request.app.state, "runtime_config", None)
    return bool(getattr(config, "display_readonly", False))


def display_catalog_cached(request: Request, key: str, loader: Callable[[], Any]) -> Any:
    """display_readonly 下按 key 缓存 loader 结果 TTL 秒；其它角色直通。"""
    if not _display_readonly(request):
        return loader()
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and now - hit[0] < DISPLAY_CATALOG_TTL_SECONDS:
            return hit[1]
    value = loader()
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _store.clear()
        _store[key] = (time.monotonic(), value)
    return value


def clear_display_catalog_cache() -> None:
    """测试钩子：清空缓存。"""
    with _lock:
        _store.clear()
