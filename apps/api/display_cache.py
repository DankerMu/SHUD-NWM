"""display_readonly 进程内 TTL 缓存 + 自预热（目录类只读端点）。

背景：runs、layers、basins 等展示目录端点由只读 display API 高频访问；
目录数据节奏为小时级 cycle，同一匿名请求高度重复。

机制（display_readonly 角色专属，其它角色直通）：
- 新鲜（< TTL 60s）：直接命中。
- 过期但 < STALE_MAX 10min：先回 stale 不阻塞访客，预热线程负责刷新
  （stale-while-revalidate；展示数据小时级节奏下 10min 内陈旧诚实可接受）。
- 超过 STALE_MAX：阻塞重算（真冷路径，仅进程刚启动或长期无人访问后出现）。
- 自预热：记录最近访问的目录 GET path，后台线程每 45s 经 ASGI 回放
  （在 ASGI scope 里打进程内预热标记以旁路缓存），保持热 key 常新。
- 准入（#2078）：调用方可给 `cacheable` 谓词，loader 返回值不满足时只回给访客，
  既不写 `_store` 也不登记热 path，并把该 key 从两表移除——空结果的无界维度
  （runs 空页、valid-times 空列表）不再换来一条能被回放的缓存条目。
- 淘汰（#2078）：两表都是 `_MAX_ENTRIES` 封顶的 OrderedDict LRU，命中 `move_to_end`，
  到顶只 `popitem(last=False)` 淘汰最旧一条；除测试钩子外永不整表 clear——公网
  请求用很多个不同 key 最多挤掉最旧的条目，挤不空整表。
- 回放上界（#2078）：预热线程每 tick 只回放活跃窗口内按「命中计数降序、最近访问
  降序」排序的前 `DISPLAY_CATALOG_WARM_REPLAY_MAX` 条热 path。

边界（honest）：缓存的是 store 层 payload（不含 request_id 信封）；
根治（目录查询索引与覆盖物化）见后端慢查询专项。
"""

from __future__ import annotations

import asyncio
import hmac
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request

DISPLAY_CATALOG_TTL_SECONDS = 60.0
DISPLAY_CATALOG_STALE_MAX_SECONDS = 600.0
DISPLAY_CATALOG_WARM_INTERVAL_SECONDS = 45.0
DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS = 1800.0
# 每个 tick 的回放上界（#2078）：活跃窗口内按命中计数排序取前 K 条。封顶的是回放的
# 「量」不是「身份」——无鉴权下没有抗操纵的排序键，但被垃圾 key 占满时的代价也只是
# 「K 条冷查询 / tick」而不是整表回放。测试 monkeypatch 该模块全局。
DISPLAY_CATALOG_WARM_REPLAY_MAX = 32
# 强制刷新头。头的存在不再是特权：只有当值与 NHMS_DISPLAY_CACHE_WARM_TOKEN
# 配置的 token 相等（`hmac.compare_digest`）时才生效；token 未配置时任何值都不生效
# （#2079：display 侧 GET 无鉴权、nginx 不剥头，字面值 `refresh` 曾让公网任意客户端
# 逐请求把目录端点打回冷路径）。常量名保留：prewarm 与测试都引用它。
DISPLAY_CACHE_FORCE_REFRESH_HEADER = "x-nhms-cache-warm"
# 进程内预热身份：`_replay_targets` 在 ASGI scope["state"] 上打的标记。网络侧不可
# 伪造（uvicorn 自建 scope["state"]，客户端写不进去），也不需要任何配置。
_WARM_SCOPE_KEY = "nhms_display_cache_warm"
_MAX_ENTRIES = 256

_lock = threading.Lock()
# 两表都是 LRU：插入到尾、命中 move_to_end、到顶淘汰头部一条（`_lru_set`）。
_store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
# key -> (带 query 的请求 path, 最近访问时刻, 命中计数)；预热线程按活跃窗口 + 计数回放。
_hot_paths: OrderedDict[str, tuple[str, float, int]] = OrderedDict()
_warmer_started = False
# 预热线程停止信号 + 线程句柄（stop 钩子用；生产进程不调用，线程活到进程退出）。
_stop_event = threading.Event()
_warmer_thread: threading.Thread | None = None


def _display_readonly(request: Request) -> bool:
    config = getattr(getattr(getattr(request, "app", None), "state", None), "runtime_config", None)
    return bool(getattr(config, "display_readonly", False))


def _force_refresh(request: Request) -> bool:
    """两种身份才能强制冷路径：进程内预热标记，或持有配置 token 的调用方。

    绝不抛：本函数跑在每条目录 GET 的最前面，任何 `TypeError`/`AttributeError`
    都会变成 500。特别地 `hmac.compare_digest` 对含非 ASCII 的 `str` 抛
    `TypeError`，而 Starlette 按 latin-1 解头值——所以两侧一律先 encode 成
    bytes 再比。`surrogateescape` 兜住 `os.environ` 里一个非法 UTF-8 字节
    （会解成 `\\udcXX`）的 token。
    """
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        state = scope.get("state")
        if isinstance(state, dict) and state.get(_WARM_SCOPE_KEY) is True:
            return True

    config = getattr(getattr(getattr(request, "app", None), "state", None), "runtime_config", None)
    token = getattr(config, "display_cache_warm_token", None)
    if not isinstance(token, str) or not token.strip():
        return False
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    header_value = headers.get(DISPLAY_CACHE_FORCE_REFRESH_HEADER)
    if not isinstance(header_value, str):
        return False
    return hmac.compare_digest(
        header_value.encode("utf-8", "surrogateescape"),
        token.encode("utf-8", "surrogateescape"),
    )


def _mark_warm_scope(app: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """把 app 包一层，在 http scope 的 `state` 上打进程内预热标记。

    拷贝 scope 与 state（不就地改写调用方的字典）；非 http scope 原样透传。
    """

    async def _marked(scope: Any, receive: Any, send: Any) -> None:
        if isinstance(scope, dict) and scope.get("type") == "http":
            scope = dict(scope)
            state = dict(scope.get("state") or {})
            state[_WARM_SCOPE_KEY] = True
            scope["state"] = state
        await app(scope, receive, send)

    return _marked


def _request_path(request: Request) -> str | None:
    """带 query 的请求 path（预热回放的目标）；request 没有 `.url` 时返回 None。"""
    url = getattr(request, "url", None)
    if url is None:
        return None
    query = getattr(url, "query", "") or ""
    return f"{url.path}?{query}" if query else str(url.path)


def _lru_set(mapping: OrderedDict[str, Any], key: str, value: Any) -> None:
    """LRU 写入（调用方必须持 `_lock`）。

    只有**新** key 才可能触发淘汰：已存在的 key 直接改值 + `move_to_end`（`OrderedDict`
    的赋值不移动位置，漏掉 `move_to_end` 就等于没有 LRU），否则表满时重写一条既有
    条目会莫名其妙淘汰掉另一条。到顶时 `popitem(last=False)` 只淘汰最旧一条——绝不
    整表 `clear()`：那正是公网请求能把整份缓存冲空的原因（#2078）。
    """
    if key in mapping:
        mapping[key] = value
        mapping.move_to_end(key)
        return
    if len(mapping) >= _MAX_ENTRIES:
        mapping.popitem(last=False)
    mapping[key] = value


def _record_hot_path(key: str, path: str, hits: int) -> None:
    with _lock:
        _lru_set(_hot_paths, key, (path, time.monotonic(), hits))


def _store_value(key: str, value: Any) -> None:
    with _lock:
        _lru_set(_store, key, (time.monotonic(), value))


def _forget(key: str) -> None:
    """把 key 从两表移除（不可缓存的结果不该留下条目，也不该继续被回放）。"""
    with _lock:
        _store.pop(key, None)
        _hot_paths.pop(key, None)


def display_catalog_cached(
    request: Request,
    key: str,
    loader: Callable[[], Any],
    *,
    cacheable: Callable[[Any], bool] | None = None,
) -> Any:
    """display_readonly 下按 key 缓存 loader 结果（TTL + stale-while-revalidate）。

    `cacheable` 是可选的准入谓词，只看 loader 的返回值：为假时结果照常回给访客，但
    不入 `_store`、不登记 `_hot_paths`，并把该 key 从两表移除（force-refresh 分支同样
    适用——回放拿到空结果说明旧值已过期或该 key 本就是垃圾）。默认 `None` = 一律可
    缓存。非 display 角色直通，谓词不被求值。
    """
    if not _display_readonly(request):
        return loader()
    if _force_refresh(request):
        # 回放不登记热 path：登记会让预热线程每 tick 给自己的目标续上活跃窗口与命中
        # 计数，1800 s 窗口永不过期，排序前提也就没了。
        value = loader()
        if cacheable is None or cacheable(value):
            _store_value(key, value)
        else:
            _forget(key)
        return value
    path = _request_path(request)
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None:
            age = now - hit[0]
            if age < DISPLAY_CATALOG_STALE_MAX_SECONDS:
                # 新鲜直接命中；过期但未超 stale 上限也先回 stale（预热线程负责刷新），
                # 不让访客阻塞在 12s 级慢查询上。
                _store.move_to_end(key)
                if path is not None:
                    previous = _hot_paths.get(key)
                    _lru_set(_hot_paths, key, (path, now, previous[2] + 1 if previous else 1))
                return hit[1]
    value = loader()
    if cacheable is not None and not cacheable(value):
        _forget(key)
        return value
    _store_value(key, value)
    if path is not None:
        _record_hot_path(key, path, 1)
    return value


def start_display_catalog_warmer(app: FastAPI) -> threading.Thread | None:
    """display_readonly 启动自预热线程（进程级单例；daemon，不阻塞退出）。

    `thread.start()` 失败（OS 起不了线程）时回滚模块状态并原样抛出，调用方只看到那
    一次真实错误；否则会留下"已登记但从未启动"的句柄，之后每次 stop() 都在 join 处炸
    ``cannot join thread before it is started``，级联无法自愈。
    """
    global _warmer_started, _warmer_thread
    with _lock:
        if _warmer_started:
            return None
        _warmer_started = True
        _stop_event.clear()
        thread = threading.Thread(target=_warm_loop, args=(app,), daemon=True, name="display-catalog-warmer")
        _warmer_thread = thread
    try:
        thread.start()
    except BaseException:
        with _lock:
            _warmer_started = False
            _warmer_thread = None
        raise
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
            # 锁内只做快照（排序与回放都在锁外）：回放路径自己要取同一把锁。
            active = [
                (hits, last_access, path)
                for (path, last_access, hits) in _hot_paths.values()
                if now - last_access < DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS
            ]
        # 命中计数降序、最近访问降序；只取前 K 条（上界每轮从模块全局读，便于压缩）。
        active.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        targets = [path for (_hits, _last_access, path) in active[:DISPLAY_CATALOG_WARM_REPLAY_MAX]]
        if not targets:
            continue
        try:
            asyncio.run(_replay_targets(app, targets))
        except Exception:  # noqa: BLE001 - 预热失败保留 stale，下一轮再试
            continue


async def _replay_targets(app: FastAPI, targets: list[str]) -> None:
    import httpx

    # 身份走 ASGI scope 标记而不是头：预热是进程内的，没必要（也不应该）依赖一个
    # 网络可伪造、且需要部署 token 才生效的头。
    transport = httpx.ASGITransport(app=_mark_warm_scope(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://display-cache-warmer") as client:
        for path in targets:
            try:
                await client.get(path, timeout=120.0)
            except Exception:  # noqa: BLE001 - 单 path 失败不影响其余预热
                continue


def clear_display_catalog_cache() -> None:
    """测试钩子：清空缓存与热 path 记录。"""
    with _lock:
        _store.clear()
        _hot_paths.clear()
