"""The pre-commit index-reason ownership table audit (#1619, #1611).

Partition of ``tests/test_scheduler_state_index_copyback_replay.py``; every case
and the expectation table below are verbatim moves. These cases take no fixture:
they read the replay module's tables and parse ``packages/common/state_manager.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from packages.common import state_manager
from scripts import scheduler_state_index_copyback_replay as replay


def test_pre_commit_index_reason_ownership_keys_match_allowlist() -> None:
    """#1619: the ownership table is exactly `_PRE_COMMIT_INDEX_REASONS`, no more.

    A reason on the allowlist without an owning function is unauditable, and an
    ownership entry for a reason that is not allowlisted is dead weight that
    would mask an accidental allowlist deletion.
    """

    assert set(replay.PRE_COMMIT_INDEX_REASON_OWNERS) == set(
        replay._PRE_COMMIT_INDEX_REASONS
    )
    # An allowlisted reason whose owner tuple was emptied (or truncated by a
    # careless edit) must fail here by name; the key-set check above cannot
    # see it and the literal check below is vacuous over an empty tuple.
    empty = [reason for reason, owners in replay.PRE_COMMIT_INDEX_REASON_OWNERS.items() if not owners]
    assert empty == [], f"reasons with no owning function: {empty}"


def test_pre_commit_index_reason_owners_contain_the_reason_literal() -> None:
    """#1619: every ownership entry names a real function holding the reason.

    Parses `packages/common/state_manager.py` and requires each owner
    function's own source to contain the reason literal, so a rename or a
    raise moved to another function reds here without depending on mutable
    line numbers.  A moved raise is exactly the audit event this table exists
    to flag: the new raise point must be re-audited for pre-commit-ness
    before the mapping is updated.
    """

    source = Path(state_manager.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in bodies:
            bodies[node.name] = ast.get_source_segment(source, node) or ""

    problems = []
    for reason, owners in sorted(replay.PRE_COMMIT_INDEX_REASON_OWNERS.items()):
        for owner in owners:
            body = bodies.get(owner)
            if body is None:
                problems.append(f"{owner} (owner of {reason}) is not a function in state_manager.py")
            elif f'"{reason}"' not in body:
                problems.append(f'{owner} does not raise "{reason}"')
    assert problems == []


#: Phase-2 P1: the reasons the replaced line-number index audited across more
#: than one function, with every one of those owners.  Independent of the
#: implementation table so a silent collapse to a single owner reds below.
_EXPECTED_MULTI_OWNER_REASONS: dict[str, frozenset[str]] = {
    "state_snapshot_index_entries_invalid": frozenset(
        {"_copyback_raw_entries", "_validate_state_snapshot_index"}
    ),
    "state_snapshot_index_entry_not_object": frozenset(
        {"_copyback_raw_entries", "_validate_state_snapshot_index"}
    ),
    "state_snapshot_index_size_limit_exceeded": frozenset(
        {"publish_state_snapshot_index", "_read_state_index_bytes"}
    ),
    "state_snapshot_index_object_unreadable": frozenset(
        {"_copyback_state_checkpoint", "_verify_state_index_object"}
    ),
    "state_snapshot_index_object_checksum_mismatch": frozenset(
        {"_copyback_state_checkpoint", "_verify_state_index_object"}
    ),
    "state_snapshot_index_object_unsafe_uri": frozenset(
        {
            "_copyback_state_checkpoint",
            "_ensure_copyback_state_parent",
            "_require_supported_state_object_reference",
            "_require_no_encoded_unsafe_object_key",
            "_state_index_destination_path",
        }
    ),
    "state_snapshot_index_object_unsupported_uri": frozenset(
        {"_require_supported_state_object_reference", "_state_index_control_object_path"}
    ),
}


def test_pre_commit_multi_owner_reasons_keep_every_audited_owner() -> None:
    """Phase-2 P1: multi-owner reasons must not lose an audited owner.

    The key-set test only proves every allowlisted reason has *some* owner; a
    collapse of one of these rows back to a single owner (the exact regression
    that shipped first) stays green there.  Each row here pins the full set
    translated from the old line-number index.
    """

    for reason, expected in _EXPECTED_MULTI_OWNER_REASONS.items():
        assert frozenset(replay.PRE_COMMIT_INDEX_REASON_OWNERS[reason]) == expected, (
            f"owner set changed for {reason}"
        )
