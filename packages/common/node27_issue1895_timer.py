"""G8 timer-ordering, receipt identity, and durable-group reconciliation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_issue1895_types import Issue1895ReadinessError

COMPRESSION_TIMER = "nhms-node27-timeseries-compression.timer"
COMPRESSION_SERVICE = "nhms-node27-timeseries-compression.service"
REQUIRE_COUNT = 6


def assert_baseline_before_start(script: str) -> None:
    """Refuse a fence that captures LastTriggerUSec after systemctl start."""

    text = str(script)
    start_at = text.find("systemctl --user start")
    if start_at < 0:
        start_at = text.find("/usr/bin/systemctl --user start")
    baseline_timer = text.find("LastTriggerUSec")
    baseline_service = text.find("ExecMainStartTimestamp")
    if baseline_timer < 0 or baseline_service < 0:
        raise Issue1895ReadinessError(
            "G8 fence never captures the timer/service baseline",
            code="TIMER_BASELINE_MISSING",
            stage="timer",
        )
    if start_at < 0:
        return
    if baseline_timer > start_at or baseline_service > start_at:
        raise Issue1895ReadinessError(
            "timer baseline capture moved after systemctl start",
            code="TIMER_BASELINE_AFTER_START",
            stage="timer",
        )


def assert_natural_receipt_identity(
    receipt: Mapping[str, Any],
    *,
    reviewed_sha: str,
    expected_cutoff: str,
    expected_watermark: str,
    expected_per_tick_bound: int = 1,
    expected_unit: str = COMPRESSION_SERVICE,
    invoked_unit: str,
) -> None:
    if invoked_unit != expected_unit:
        raise Issue1895ReadinessError(
            "natural tick was not invoked by the compression service unit",
            code="TICK_UNIT_MISMATCH",
            stage="timer",
        )
    if receipt.get("head_sha") != reviewed_sha:
        raise Issue1895ReadinessError(
            "natural receipt head_sha is not REVIEWED_SHA",
            code="TICK_HEAD_MISMATCH",
            stage="timer",
        )
    if receipt.get("cutoff") != expected_cutoff:
        raise Issue1895ReadinessError(
            "natural receipt cutoff drifted",
            code="TICK_CUTOFF_MISMATCH",
            stage="timer",
        )
    if receipt.get("watermark") != expected_watermark:
        raise Issue1895ReadinessError(
            "natural receipt watermark drifted",
            code="TICK_WATERMARK_MISMATCH",
            stage="timer",
        )
    if receipt.get("per_tick_bound") != expected_per_tick_bound:
        raise Issue1895ReadinessError(
            "natural receipt per_tick_bound drifted",
            code="TICK_BOUND_MISMATCH",
            stage="timer",
        )
    outcome = receipt.get("outcome")
    if outcome not in {"clean", "no_op"}:
        raise Issue1895ReadinessError(
            "natural receipt outcome is not clean or no_op",
            code="TICK_OUTCOME_INVALID",
            stage="timer",
        )
    if receipt.get("schema_version") != "1.1":
        raise Issue1895ReadinessError(
            "natural receipt schema_version drifted",
            code="TICK_SCHEMA_INVALID",
            stage="timer",
        )


def group_member_identity(group: Mapping[str, Any]) -> dict[str, Any]:
    durable = group.get("durable")
    if not isinstance(durable, Mapping):
        raise Issue1895ReadinessError(
            "group durable identity is missing",
            code="GROUP_IDENTITY_INVALID",
            stage="census",
        )
    required = (
        "hypertable_schema",
        "hypertable_name",
        "origin_schema",
        "origin_name",
        "origin_oid",
        "range_start",
        "range_end",
    )
    missing = [field for field in required if field not in durable]
    if missing:
        raise Issue1895ReadinessError(
            "group durable identity is incomplete",
            code="GROUP_IDENTITY_INVALID",
            stage="census",
        )
    return {
        "key": group.get("key") or None,
        "durable": {field: durable[field] for field in required},
    }


def persist_baseline_groups(groups: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if len(groups) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "baseline census is not exactly six groups",
            code="BASELINE_COUNT_INVALID",
            stage="census",
        )
    keys = [str(group.get("key") or "") for group in groups]
    if len(set(keys)) != REQUIRE_COUNT or any(not key for key in keys):
        raise Issue1895ReadinessError(
            "baseline census keys are not unique",
            code="BASELINE_KEYS_INVALID",
            stage="census",
        )
    return tuple(group_member_identity(group) for group in groups)


def assert_exact_cold_groups(
    observed: Sequence[Mapping[str, Any]],
    *,
    baseline: Sequence[Mapping[str, Any]],
) -> None:
    """Prove exactly the baseline six groups are cold, with no extra/missing identity."""

    if baseline and "durable" in baseline[0] and "key" in baseline[0]:
        expected = persist_baseline_groups(baseline)
    else:
        expected = tuple(baseline)
    if len(expected) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "baseline identity set is not exactly six groups",
            code="BASELINE_COUNT_INVALID",
            stage="census",
        )
    observed_identities = [group_member_identity(group) for group in observed]
    if len(observed_identities) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "post-tick census is not exactly six groups",
            code="COLD_COUNT_INVALID",
            stage="census",
        )
    expected_keys = {item["key"] for item in expected}
    observed_keys = {item["key"] for item in observed_identities}
    if expected_keys != observed_keys:
        raise Issue1895ReadinessError(
            "post-tick census keys drifted from the baseline six",
            code="COLD_KEYS_DRIFTED",
            stage="census",
        )
    expected_by_key = {item["key"]: item for item in expected}
    for item in observed_identities:
        expected_item = expected_by_key[item["key"]]
        if item["durable"] != expected_item["durable"]:
            raise Issue1895ReadinessError(
                "post-tick durable identity drifted",
                code="COLD_IDENTITY_DRIFTED",
                stage="census",
            )
        residency = next(
            (group.get("residency") for group in observed if group.get("key") == item["key"]),
            None,
        )
        complete = next(
            (group.get("complete_target") for group in observed if group.get("key") == item["key"]),
            None,
        )
        if residency not in {"already_target", "complete_target"} and complete is not True:
            raise Issue1895ReadinessError(
                "baseline group is not fully cold",
                code="COLD_RESIDENCY_MIXED",
                stage="census",
            )


def _observation_durable_key(item: Mapping[str, Any]) -> str:
    from packages.common.node27_issue1895_receipt import durable_key

    durable = item.get("durable")
    if not isinstance(durable, Mapping):
        before = item.get("before")
        if isinstance(before, Mapping) and isinstance(before.get("durable"), Mapping):
            durable = before["durable"]
    if not isinstance(durable, Mapping):
        raise Issue1895ReadinessError(
            "observation durable identity is missing",
            code="TICK_SELECTION_INVALID",
            stage="timer",
        )
    return durable_key(durable)


def assert_natural_tick_selection(
    receipt: Mapping[str, Any],
    *,
    remaining_complete_source_keys: Sequence[str],
    newly_terminal_keys: Sequence[str],
    baseline_keys: Sequence[str] = (),
) -> None:
    """Bind subsequent-tick semantics to independent pre/post catalog sets.

    no_op: independent pre/post observations show no new complete-target key;
    selected may contain baseline already_cold only; deferred is empty.
    migrated: exactly one new complete-target durable key equals the unique
    receipt migrated key; remaining complete-source keys/reasons equal deferred.
    """

    outcome = receipt.get("outcome")
    selected = receipt.get("selected")
    deferred = receipt.get("deferred")
    if not isinstance(selected, list) or not isinstance(deferred, list):
        raise Issue1895ReadinessError(
            "natural receipt selected/deferred is missing",
            code="TICK_SELECTION_INVALID",
            stage="timer",
        )
    remaining = tuple(remaining_complete_source_keys)
    newly = tuple(newly_terminal_keys)
    if not newly:
        if remaining:
            raise Issue1895ReadinessError(
                "no_op claimed while complete-source groups remain",
                code="TICK_NOOP_NOT_EXHAUSTIVE",
                stage="timer",
            )
        if outcome != "no_op":
            raise Issue1895ReadinessError(
                "empty candidate set did not produce no_op",
                code="TICK_NOOP_REQUIRED",
                stage="timer",
            )
        if deferred:
            raise Issue1895ReadinessError(
                "no_op deferred is not empty",
                code="TICK_NOOP_NOT_EMPTY",
                stage="timer",
            )
        for item in selected:
            if not isinstance(item, Mapping) or item.get("outcome") != "already_cold":
                raise Issue1895ReadinessError(
                    "no_op selected items must be already_cold",
                    code="TICK_NOOP_NOT_EMPTY",
                    stage="timer",
                )
            key = _observation_durable_key(item)
            if baseline_keys and key not in set(baseline_keys):
                raise Issue1895ReadinessError(
                    "no_op selected a non-baseline key",
                    code="TICK_NOOP_NOT_EMPTY",
                    stage="timer",
                )
        return
    if len(newly) != 1:
        raise Issue1895ReadinessError(
            "natural tick observed more than one new complete-target key",
            code="TICK_NEW_TARGET_NOT_UNIQUE",
            stage="timer",
        )
    if outcome != "clean":
        raise Issue1895ReadinessError(
            "newly terminal group was not selected",
            code="TICK_SELECTION_MISSING",
            stage="timer",
        )
    migrated = [item for item in selected if isinstance(item, Mapping) and item.get("outcome") == "migrated"]
    already_cold = [item for item in selected if isinstance(item, Mapping) and item.get("outcome") == "already_cold"]
    other = [
        item
        for item in selected
        if isinstance(item, Mapping) and item.get("outcome") not in {"migrated", "already_cold"}
    ]
    if other or len(migrated) != 1:
        raise Issue1895ReadinessError(
            "natural tick did not contain exactly one migrated observation",
            code="TICK_SELECTION_INVALID",
            stage="timer",
        )
    migrated_key = _observation_durable_key(migrated[0])
    if migrated_key != newly[0]:
        raise Issue1895ReadinessError(
            "natural tick migrated a different key than the newly terminal candidate",
            code="TICK_SELECTION_MISMATCH",
            stage="timer",
        )
    for item in already_cold:
        key = _observation_durable_key(item)
        if baseline_keys and key not in set(baseline_keys):
            raise Issue1895ReadinessError(
                "already_cold observation is not a baseline key",
                code="TICK_SELECTION_INVALID",
                stage="timer",
            )
    deferred_keys: list[str] = []
    for item in deferred:
        if not isinstance(item, Mapping):
            raise Issue1895ReadinessError(
                "deferred observation is not an object",
                code="TICK_SELECTION_INVALID",
                stage="timer",
            )
        if item.get("reason") != "per_tick_bound":
            raise Issue1895ReadinessError(
                "deferred reason is not per_tick_bound",
                code="TICK_DEFERRED_REASON",
                stage="timer",
            )
        deferred_keys.append(_observation_durable_key(item))
    if tuple(deferred_keys) != remaining:
        raise Issue1895ReadinessError(
            "deferred set drifted from independently observed complete-source keys",
            code="TICK_DEFERRED_MISMATCH",
            stage="timer",
        )
