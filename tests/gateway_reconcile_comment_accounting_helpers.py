"""Shared helpers of the comment-accounting suite (#3177).

Split out of ``tests/test_gateway_reconcile_comment_accounting.py``; the two
fallback partitions (``tests/test_gateway_reconcile_comment_fallback_*.py``)
import from here. Not a test module: pytest does not collect it. The same-named
helpers of the claimant and binding-provenance suites have different bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

# --- #1565: bounded name-window fallback on an explicitly comment-less cluster ---
# The fallback renders query bounds and parses Submit in the host local
# timezone, so the command-shape tests pin the host TZ to UTC (same pattern as
# the page-bound tests); the host-local parse test pins Asia/Shanghai.


def _fallback_row(
    master_id: str,
    *,
    submit: str,
    user: str = "scheduler",
    account: str = "account",
    comment: str = "",
    job_name: str = "nhms_forecast",
    task_id: int | None = None,
) -> str:
    job_id = f"{master_id}_{task_id}" if task_id is not None else master_id
    return f"{job_id}|{job_name}|COMPLETED|0:0|{comment}|{user}|{account}|{submit}\n"


def _fallback_querier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: str,
    query_end: datetime,
) -> Any:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: commands.append(list(command)) or rows,
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )
    return query, commands
