#!/usr/bin/env python3
"""Bounded, receipted TimescaleDB compressed-chunk cold-residency runner for node-27.

Dry-run by default. ``--enforce`` is the only mutation authorization. Production
code consumes ``packages.common.compressed_chunk_cold_runtime`` and never imports
probe-private modules.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packages.common.compressed_chunk_cold_receipt import (
    ColdReceiptError,
    assert_publication_paths_disjoint,
    build_receipt,
    intent_path_for,
    public_authority_blocks_selection,
    publish_receipt,
    read_public_receipt,
    sidecar_present,
    sidecar_status,
    stable_error,
    unavailable_cluster,
    unavailable_inventory,
    unavailable_target,
)
from packages.common.compressed_chunk_cold_runtime import (
    CONTAINER_COLD_PATH,
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_MAX_MEMBERS,
    HOST_COLD_PATH,
    LIVE_CONTAINER_NAME,
)
from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError
from packages.common.compressed_chunk_cold_tick import run_tick as execute_tick
from packages.common.display_watermark import DisplayWatermarkError, fetch_display_watermark
from packages.common.node27_timeseries_lifecycle_lock import (
    LIFECYCLE_LOCK_PATH,
    LifecycleLockError,
    acquire_timeseries_lifecycle_lock,
    refuse_lifecycle_lock_env_override,
    release_timeseries_lifecycle_lock,
)
from packages.common.node27_timeseries_sequential_budget import (
    SequentialBudgetError,
    resolve_runner_budget,
    sequential_service_budget,
)
from packages.common.safe_fs import ensure_directory_no_follow, open_directory_no_follow

SCHEMA_VERSION = "1.0"
_APPLICATION_NAME = "nhms-ts-cold-residency"
_CONNECT_TIMEOUT_SECONDS = 10
_MAX_CATALOG_ROWS = 10_000
_MAX_CATALOG_BYTES = 16 * 1024**2
_DEFAULT_PER_TICK_BOUND = 1
_AUTHORITATIVE_BUDGET = sequential_service_budget()
_CLEANUP_MARGIN_SECONDS = 300
_SYSTEMD_MARGIN_SECONDS = _AUTHORITATIVE_BUDGET.systemd_margin_seconds
_DEFAULT_STATEMENT_TIMEOUT_MS = 3_600_000
_DEFAULT_WRAPPER_WALL_SECONDS = _AUTHORITATIVE_BUDGET.cold_wrapper_wall_seconds
_DEFAULT_COMPRESSION_WRAPPER_WALL_SECONDS = _AUTHORITATIVE_BUDGET.compression_wrapper_wall_seconds
_DEFAULT_SYSTEMD_WALL_SECONDS = _AUTHORITATIVE_BUDGET.service_wall_seconds
_COMPRESSION_LAG_DEFAULT = 604800


class ColdResidencyConfigError(RuntimeError):
    def __init__(self, message: str, *, error_class: str = "config", stage: str = "config") -> None:
        super().__init__(message)
        self.error_class = error_class
        self.stage = stage


@dataclass(frozen=True)
class RunnerConfig:
    database_url: str
    lag_seconds: int
    per_tick_bound: int
    receipt_path: Path
    intent_path: Path
    lock_path: Path
    lifecycle_lock_path: Path
    enforce: bool
    statement_timeout_ms: int
    wrapper_wall_seconds: int
    compression_wrapper_wall_seconds: int
    systemd_wall_seconds: int
    cold_reserve_bytes: int
    wal_reserve_bytes: int
    max_members: int
    lock_timeout: str
    expected_catalog_location: str
    expected_container_bind: str
    expected_host_path: str
    expected_container_name: str
    expected_device_identity: str
    inspect_target: Callable[[], Mapping[str, Any]] | None = None
    cold_free_bytes: int | None = None
    hot_free_bytes: int | None = None
    after_group_progress: Callable[[int, Mapping[str, Any]], None] | None = None
    clock: Callable[[], float] | None = None


def _observe_head(repo_root: Path | None = None) -> tuple[str | None, bool, bool]:
    root = Path(__file__).resolve().parents[1] if repo_root is None else repo_root
    try:
        parsed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        unstaged = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=root, check=False, timeout=10)
        staged = subprocess.run(["git", "diff", "--quiet", "--cached", "--"], cwd=root, check=False, timeout=10)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise ColdResidencyConfigError(
            "cannot bind receipt to repository HEAD",
            error_class="head",
            stage="freeze_head",
        ) from error
    head_sha = parsed.stdout.strip()
    observed = parsed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
    dirty = bool(unstaged.returncode or staged.returncode or (untracked.stdout or "").strip())
    if not observed:
        return None, False, dirty
    return head_sha, True, dirty


def _current_head_sha(*, require_clean: bool = False) -> str:
    head_sha, observed, dirty = _observe_head()
    if not observed or head_sha is None:
        raise ColdResidencyConfigError(
            "cannot bind receipt to repository HEAD",
            error_class="head",
            stage="freeze_head",
        )
    if require_clean and dirty:
        raise ColdResidencyConfigError(
            "runner worktree differs from repository HEAD",
            error_class="head",
            stage="freeze_head",
        )
    return head_sha


def _mask_dsn(dsn: str) -> str:
    try:
        parts = urlsplit(dsn)
    except Exception:
        return "postgresql://***@***/***"
    netloc = parts.hostname or "***"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username is not None or parts.password is not None:
        netloc = f"***@{netloc}"
    return urlunsplit((parts.scheme or "postgresql", netloc, parts.path or "", "", ""))


def _parse_positive_int(raw: str | None, *, name: str, minimum: int) -> int:
    if raw is None or raw == "":
        raise ColdResidencyConfigError(f"{name} must be set")
    stripped = raw.strip()
    if stripped == "" or stripped != raw:
        raise ColdResidencyConfigError(f"{name} must not contain leading/trailing whitespace")
    try:
        value = int(stripped)
    except ValueError as error:
        raise ColdResidencyConfigError(f"{name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise ColdResidencyConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _parse_positive_int_with_default(env: Mapping[str, str], name: str, *, default: int, minimum: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return _parse_positive_int(raw, name=name, minimum=minimum)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true", help="actually migrate eligible groups")
    parser.add_argument("--receipt-path", type=str, default=None)
    parser.add_argument("--lock-path", type=str, default=None)
    return parser


def identify_receipt_path(args: argparse.Namespace, env: Mapping[str, str]) -> Path:
    receipt_raw = args.receipt_path if args.receipt_path is not None else env.get("NODE27_COLD_RESIDENCY_RECEIPT_PATH")
    if not receipt_raw:
        raise ColdResidencyConfigError(
            "receipt path must be set via --receipt-path or NODE27_COLD_RESIDENCY_RECEIPT_PATH",
            error_class="receipt_path",
            stage="config",
        )
    receipt_path = Path(str(receipt_raw))
    if not receipt_path.is_absolute():
        raise ColdResidencyConfigError("receipt path must be absolute", error_class="receipt_path", stage="config")
    return receipt_path


def config_from_args(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> RunnerConfig:
    env = os.environ if env is None else env
    receipt_path = identify_receipt_path(args, env)
    lock_raw = args.lock_path if args.lock_path is not None else env.get("NODE27_COLD_RESIDENCY_LOCK_PATH")
    if not lock_raw:
        raise ColdResidencyConfigError(
            "lock path must be set via --lock-path or NODE27_COLD_RESIDENCY_LOCK_PATH",
            error_class="lock_path",
            stage="config",
        )
    lock_path = Path(str(lock_raw))
    if not lock_path.is_absolute():
        raise ColdResidencyConfigError("lock path must be absolute", error_class="lock_path", stage="config")
    intent_path = intent_path_for(receipt_path)
    try:
        refuse_lifecycle_lock_env_override(env)
    except LifecycleLockError as error:
        raise ColdResidencyConfigError(str(error), error_class="path_alias", stage="config") from error
    assert_publication_paths_disjoint(
        receipt_path=receipt_path,
        intent_path=intent_path,
        lock_path=lock_path,
        lifecycle_lock_path=LIFECYCLE_LOCK_PATH,
    )
    lag_raw = env.get("NODE27_COLD_RESIDENCY_LAG_SECONDS")
    if lag_raw is None or lag_raw == "":
        lag_raw = env.get("NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS")
        if lag_raw is None or lag_raw == "":
            lag_raw = str(_COMPRESSION_LAG_DEFAULT)
    lag_seconds = _parse_positive_int(lag_raw, name="NODE27_COLD_RESIDENCY_LAG_SECONDS", minimum=1)
    per_tick_bound = _parse_positive_int_with_default(
        env,
        "NODE27_COLD_RESIDENCY_PER_TICK_BOUND",
        default=_DEFAULT_PER_TICK_BOUND,
        minimum=1,
    )
    try:
        resolved_budget = resolve_runner_budget(env, lane="cold")
    except SequentialBudgetError as error:
        raise ColdResidencyConfigError(str(error)) from error
    statement_timeout_ms = resolved_budget.cold_statement_timeout_ms
    wrapper_wall_seconds = resolved_budget.budget.cold_wrapper_wall_seconds
    compression_wrapper_wall_seconds = resolved_budget.budget.compression_wrapper_wall_seconds
    systemd_wall_seconds = resolved_budget.budget.service_wall_seconds
    max_members = _parse_positive_int_with_default(
        env,
        "NODE27_COLD_RESIDENCY_MAX_MEMBERS",
        default=DEFAULT_MAX_MEMBERS,
        minimum=1,
    )
    cold_reserve_bytes = _parse_positive_int(
        env.get("NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES"),
        name="NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES",
        minimum=1,
    )
    wal_reserve_bytes = _parse_positive_int(
        env.get("NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES"),
        name="NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES",
        minimum=1,
    )
    database_url = env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise ColdResidencyConfigError("DATABASE_URL must be set")
    return RunnerConfig(
        database_url=database_url,
        lag_seconds=lag_seconds,
        per_tick_bound=per_tick_bound,
        receipt_path=receipt_path,
        intent_path=intent_path,
        lock_path=lock_path,
        lifecycle_lock_path=LIFECYCLE_LOCK_PATH,
        enforce=bool(args.enforce),
        statement_timeout_ms=statement_timeout_ms,
        wrapper_wall_seconds=wrapper_wall_seconds,
        compression_wrapper_wall_seconds=compression_wrapper_wall_seconds,
        systemd_wall_seconds=systemd_wall_seconds,
        cold_reserve_bytes=cold_reserve_bytes,
        wal_reserve_bytes=wal_reserve_bytes,
        max_members=max_members,
        lock_timeout=env.get("NODE27_COLD_RESIDENCY_LOCK_TIMEOUT") or DEFAULT_LOCK_TIMEOUT,
        expected_catalog_location=env.get("NODE27_COLD_RESIDENCY_CATALOG_LOCATION") or CONTAINER_COLD_PATH,
        expected_container_bind=env.get("NODE27_COLD_RESIDENCY_CONTAINER_BIND") or HOST_COLD_PATH,
        expected_host_path=env.get("NODE27_COLD_RESIDENCY_HOST_PATH") or HOST_COLD_PATH,
        expected_container_name=LIVE_CONTAINER_NAME,
        expected_device_identity=env.get("NODE27_COLD_RESIDENCY_DEVICE_IDENTITY") or "",
    )


def acquire_lock(path: Path) -> int | None:
    if not path.is_absolute():
        raise ColdResidencyConfigError("lock path must be absolute")
    ensure_directory_no_follow(path.parent)
    common_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = open_directory_no_follow(path.parent)
    fd: int | None = None
    try:
        try:
            fd = os.open(path.name, common_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            fd = os.open(path.name, common_flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ColdResidencyConfigError("lock file must be a mode-0600 regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    except ColdResidencyConfigError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ColdResidencyConfigError(f"cannot acquire lock file: {error}") from error
    finally:
        os.close(parent_fd)


def _attributed_connect(*args: Any, **kwargs: Any) -> Any:
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(*args, fallback_application_name=_APPLICATION_NAME, **kwargs)


def _connect_factory(database_url: str, statement_timeout_ms: int) -> Callable[[], Any]:
    def _connect() -> Any:
        import psycopg2.extras  # type: ignore[import-untyped]

        connection = _attributed_connect(
            database_url,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        return connection

    return _connect

def _budget(config: RunnerConfig) -> dict[str, int]:
    return {
        "statement_timeout_ms": config.statement_timeout_ms,
        "wrapper_wall_seconds": config.wrapper_wall_seconds,
        "compression_wrapper_wall_seconds": config.compression_wrapper_wall_seconds,
        "systemd_wall_seconds": config.systemd_wall_seconds,
        "cleanup_margin_seconds": _CLEANUP_MARGIN_SECONDS,
        "systemd_margin_seconds": _SYSTEMD_MARGIN_SECONDS,
    }


def _emit_stderr(*, status: str, error_class: str, stage: str, dsn: str | None = None) -> None:
    payload = {"status": status, "class": error_class, "stage": stage}
    if dsn is not None:
        payload["dsn"] = _mask_dsn(dsn)
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _publication_paths(
    args: argparse.Namespace,
    receipt_path: Path,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    env = os.environ if env is None else env
    intent_path = intent_path_for(receipt_path)
    lock_raw = args.lock_path or env.get("NODE27_COLD_RESIDENCY_LOCK_PATH")
    lock_path = Path(str(lock_raw)) if lock_raw else receipt_path.parent / "unused.lock"
    if not lock_path.is_absolute():
        lock_path = receipt_path.parent / "unused.lock"
    return intent_path, lock_path, LIFECYCLE_LOCK_PATH


def _tombstone(
    config: RunnerConfig,
    *,
    now: datetime,
    head_sha: str | None,
    error_class: str,
    stage: str,
    reason: str,
    outcome: str = "failed",
    state: str = "idle",
    config_observed: bool | None = None,
) -> dict[str, Any]:
    observed = True if config_observed is None else bool(config_observed)
    return build_receipt(
        mode="enforce" if config.enforce else "dry-run",
        outcome=outcome,
        state=state,
        head_sha=head_sha,
        generated_at=now,
        watermark=None,
        lag_seconds=config.lag_seconds if observed else None,
        cutoff=None,
        per_tick_bound=config.per_tick_bound if observed else None,
        max_members=config.max_members if observed else None,
        budget=_budget(config) if observed else None,
        cluster=unavailable_cluster(application_name=_APPLICATION_NAME),
        target=unavailable_target(),
        inventory=unavailable_inventory(),
        capacity=None,
        selected=[],
        deferred=[],
        skipped=[],
        error=stable_error(error_class=error_class, stage=stage, reason=reason),
        config_observed=observed,
    )


def _publish_tombstone(config: RunnerConfig, receipt: Mapping[str, Any]) -> None:
    publish_receipt(config.receipt_path, receipt)


def run_tick(
    config: RunnerConfig,
    *,
    now_utc: datetime,
    head_sha: str,
    connect: Callable[[], Any] | None = None,
    fetch_watermark: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    watermark_fn = fetch_watermark or (
        lambda: fetch_display_watermark(config.database_url, connect=_attributed_connect)
    )
    return execute_tick(
        config,
        now_utc=now_utc,
        head_sha=head_sha,
        connect=connect or _connect_factory(config.database_url, config.statement_timeout_ms),
        fetch_watermark=watermark_fn,
        attributed_connect=_attributed_connect,
        application_name=_APPLICATION_NAME,
        cleanup_margin_seconds=_CLEANUP_MARGIN_SECONDS,
        systemd_margin_seconds=_SYSTEMD_MARGIN_SECONDS,
        max_catalog_rows=_MAX_CATALOG_ROWS,
        max_catalog_bytes=_MAX_CATALOG_BYTES,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    now_utc: datetime | None = None,
    connect: Callable[[], Any] | None = None,
) -> int:
    now = now_utc or datetime.now(UTC)
    args = _parser().parse_args(argv)
    try:
        config = config_from_args(args)
    except ColdResidencyConfigError as error:
        _emit_stderr(status="failed", error_class=error.error_class, stage=error.stage)
        if error.error_class not in {"receipt_path", "path_alias", "lock_path"}:
            try:
                receipt_path = identify_receipt_path(args, os.environ)
                intent_path, lock_path, lifecycle_lock_path = _publication_paths(args, receipt_path)
                assert_publication_paths_disjoint(
                    receipt_path=receipt_path,
                    intent_path=intent_path,
                    lock_path=lock_path,
                    lifecycle_lock_path=lifecycle_lock_path,
                )
                placeholder = RunnerConfig(
                    database_url="postgresql://unavailable/unavailable",
                    lag_seconds=0,
                    per_tick_bound=0,
                    receipt_path=receipt_path,
                    intent_path=intent_path,
                    lock_path=lock_path,
                    lifecycle_lock_path=lifecycle_lock_path,
                    enforce=bool(getattr(args, "enforce", False)),
                    statement_timeout_ms=0,
                    wrapper_wall_seconds=0,
                    compression_wrapper_wall_seconds=0,
                    systemd_wall_seconds=0,
                    cold_reserve_bytes=0,
                    wal_reserve_bytes=0,
                    max_members=0,
                    lock_timeout=DEFAULT_LOCK_TIMEOUT,
                    expected_catalog_location=CONTAINER_COLD_PATH,
                    expected_container_bind=HOST_COLD_PATH,
                    expected_host_path=HOST_COLD_PATH,
                    expected_container_name=LIVE_CONTAINER_NAME,
                    expected_device_identity="",
                )
                payload = _tombstone(
                    placeholder,
                    now=now,
                    head_sha=None,
                    error_class=error.error_class,
                    stage=error.stage,
                    reason=str(error),
                    outcome="refused_config",
                    config_observed=False,
                )
                payload["head_observed"] = False
                _publish_tombstone(placeholder, payload)
            except Exception:
                pass
        return 1
    except ColdReceiptError as error:
        _emit_stderr(status="failed", error_class=error.error_class, stage=error.stage)
        return 1
    head_sha, observed, dirty = _observe_head()
    if not observed or dirty:
        _emit_stderr(status="failed", error_class="head", stage="freeze_head", dsn=config.database_url)
        try:
            payload = _tombstone(
                config,
                now=now,
                head_sha=head_sha,
                error_class="head",
                stage="freeze_head",
                reason=(
                    "repository HEAD unavailable"
                    if not observed
                    else "runner worktree differs from repository HEAD"
                ),
                outcome="refused_config",
            )
            payload["head_observed"] = observed
            payload["worktree_dirty"] = dirty
            _publish_tombstone(config, payload)
        except ColdReceiptError as publish_error:
            _emit_stderr(
                status="failed",
                error_class=publish_error.error_class,
                stage=publish_error.stage,
                dsn=config.database_url,
            )
        return 1
    try:
        sidecar_status(config.intent_path)
    except ColdReceiptError as error:
        _emit_stderr(status="failed", error_class=error.error_class, stage=error.stage, dsn=config.database_url)
        return 1
    try:
        lifecycle_fd = acquire_timeseries_lifecycle_lock()
    except LifecycleLockError as error:
        _emit_stderr(status="failed", error_class="lifecycle_lock", stage="lifecycle_lock")
        try:
            _publish_tombstone(
                config,
                _tombstone(
                    config,
                    now=now,
                    head_sha=head_sha,
                    error_class="lifecycle_lock",
                    stage="lifecycle_lock",
                    reason=str(error),
                ),
            )
        except ColdReceiptError as publish_error:
            _emit_stderr(
                status="failed",
                error_class=publish_error.error_class,
                stage=publish_error.stage,
                dsn=config.database_url,
            )
        return 1
    if lifecycle_fd is None:
        receipt = _tombstone(
            config,
            now=now,
            head_sha=head_sha,
            error_class="lifecycle_lock",
            stage="lifecycle_lock",
            reason="lock-contended",
            outcome="refused_lock",
        )
        try:
            _publish_tombstone(config, receipt)
        except ColdReceiptError as error:
            _emit_stderr(status="failed", error_class=error.error_class, stage=error.stage, dsn=config.database_url)
            return 1
        _emit_stderr(
            status="refused_lock",
            error_class="lifecycle_lock",
            stage="lifecycle_lock",
            dsn=config.database_url,
        )
        return 0
    lane_fd: int | None = None
    try:
        try:
            lane_fd = acquire_lock(config.lock_path)
        except ColdResidencyConfigError as error:
            _emit_stderr(status="failed", error_class=error.error_class, stage="acquire_lock", dsn=config.database_url)
            try:
                _publish_tombstone(
                    config,
                    _tombstone(
                        config,
                        now=now,
                        head_sha=head_sha,
                        error_class=error.error_class,
                        stage="acquire_lock",
                        reason=str(error),
                    ),
                )
            except ColdReceiptError as publish_error:
                _emit_stderr(
                    status="failed",
                    error_class=publish_error.error_class,
                    stage=publish_error.stage,
                    dsn=config.database_url,
                )
            return 1
        if lane_fd is None:
            receipt = _tombstone(
                config,
                now=now,
                head_sha=head_sha,
                error_class="lane_lock",
                stage="acquire_lock",
                reason="lock-contended",
                outcome="refused_lock",
            )
            _publish_tombstone(config, receipt)
            _emit_stderr(status="refused_lock", error_class="lane_lock", stage="acquire_lock", dsn=config.database_url)
            return 0
        try:
            receipt = run_tick(config, now_utc=now, head_sha=head_sha, connect=connect)
        except DisplayWatermarkError as error:
            _emit_stderr(status="failed", error_class="watermark", stage="display_watermark", dsn=config.database_url)
            try:
                _publish_tombstone(
                    config,
                    _tombstone(
                        config,
                        now=now,
                        head_sha=head_sha,
                        error_class="watermark",
                        stage="display_watermark",
                        reason=str(error),
                    ),
                )
            except ColdReceiptError as publish_error:
                _emit_stderr(
                    status="failed",
                    error_class=publish_error.error_class,
                    stage=publish_error.stage,
                    dsn=config.database_url,
                )
            return 1
        except (ColdRuntimeError, ColdReceiptError, ColdResidencyConfigError) as error:
            _emit_stderr(
                status="failed",
                error_class=getattr(error, "error_class", "runtime"),
                stage=getattr(error, "stage", "runtime"),
                dsn=config.database_url,
            )
            if not sidecar_present(config.intent_path) and not public_authority_blocks_selection(
                read_public_receipt(config.receipt_path)
            ):
                try:
                    _publish_tombstone(
                        config,
                        _tombstone(
                            config,
                            now=now,
                            head_sha=head_sha,
                            error_class=getattr(error, "error_class", "runtime"),
                            stage=getattr(error, "stage", "runtime"),
                            reason=str(error),
                        ),
                    )
                except ColdReceiptError as publish_error:
                    _emit_stderr(
                        status="failed",
                        error_class=publish_error.error_class,
                        stage=publish_error.stage,
                        dsn=config.database_url,
                    )
            return 1
        except Exception as error:
            _emit_stderr(status="failed", error_class="runtime", stage="runner", dsn=config.database_url)
            if not sidecar_present(config.intent_path) and not public_authority_blocks_selection(
                read_public_receipt(config.receipt_path)
            ):
                try:
                    _publish_tombstone(
                        config,
                        _tombstone(
                            config,
                            now=now,
                            head_sha=head_sha,
                            error_class="runtime",
                            stage="runner",
                            reason=type(error).__name__,
                        ),
                    )
                except ColdReceiptError:
                    pass
            return 1
        if not config.enforce or (
            not sidecar_present(config.intent_path)
            and not public_authority_blocks_selection(read_public_receipt(config.receipt_path))
        ):
            try:
                if not sidecar_present(config.receipt_path) or receipt.get("outcome") != "in_progress":
                    publish_receipt(config.receipt_path, receipt)
            except ColdReceiptError as error:
                _emit_stderr(status="failed", error_class=error.error_class, stage=error.stage, dsn=config.database_url)
                return 1
        return 0 if receipt["outcome"] in {"clean", "no_op"} else 1
    finally:
        if lane_fd is not None:
            try:
                os.close(lane_fd)
            except OSError:
                pass
        release_timeseries_lifecycle_lock(lifecycle_fd)


if __name__ == "__main__":
    raise SystemExit(main())
