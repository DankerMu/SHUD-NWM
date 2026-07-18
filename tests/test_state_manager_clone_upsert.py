from __future__ import annotations

import inspect

from packages.common.state_manager import PsycopgStateSnapshotRepository


def test_clone_upsert_retry_preserves_usable_flag() -> None:
    source = inspect.getsource(PsycopgStateSnapshotRepository.upsert_state_snapshot)
    assert "usable_flag = EXCLUDED.usable_flag" in source
    assert "usable_flag = false" not in source
