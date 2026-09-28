"""Test-only seed for a pre-existing held shape (c) row (#2674).

Not a collectible test module (pytest's ``python_files`` ignores this name).
Since #2674 no journal writer persists a ``reserved`` forecast cohort master
that carries ``cohort_members`` but no accepted-submit contract marker (shape
(c)); ``file_journal_legacy_unversioned_reserved_forecast_master`` refuses it.
Such a row can therefore only be data written before that change.  The reconcile
(``legacy_unversioned_read_only``), bind (``legacy_unversioned_unsupported``) and
listing (``escalate``) suites still need one, so they seed it here: the
module-level predicate is disabled for the seeding call only and the row goes
through the normal writer, exactly as a pre-change writer would have written
it.  No production seed parameter exists.

Journal writers only: ``file_orchestration_migration`` binds the predicate by
name at import, so the historical import's pre-scan still refuses (c) inside
this context.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar
from unittest import mock

from services.orchestrator import file_orchestration_journal

_T = TypeVar("_T")


@contextmanager
def pre_existing_legacy_rows() -> Iterator[None]:
    """Write as a pre-#2674 journal writer would have, for the ``with`` body only."""

    with mock.patch.object(
        file_orchestration_journal,
        "_is_legacy_unversioned_reserved_forecast_master",
        lambda _row: False,
    ):
        yield


def seed_pre_existing_legacy_row(write: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Call one journal writer with the #2674 shape (c) refusal disabled."""

    with pre_existing_legacy_rows():
        return write(*args, **kwargs)
