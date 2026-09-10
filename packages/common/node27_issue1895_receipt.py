"""G6 sequential per-tick receipt semantics for ordered baseline keys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.node27_issue1895_types import Issue1895ReadinessError

REQUIRE_COUNT = 6
PER_TICK_BOUND = 1
DURABLE_FIELDS: tuple[str, ...] = (
    "hypertable_schema",
    "hypertable_name",
    "origin_schema",
    "origin_name",
    "origin_oid",
    "range_start",
    "range_end",
)


def durable_key(durable: Mapping[str, Any]) -> str:
    missing = [field for field in DURABLE_FIELDS if field not in durable]
    if missing:
        raise Issue1895ReadinessError(
            "durable identity is incomplete",
            code="RECEIPT_DURABLE_INVALID",
            stage="receipt",
        )
    return (
        f"{durable['hypertable_schema']}.{durable['hypertable_name']}"
        f"|{durable['origin_schema']}.{durable['origin_name']}"
        f"|{durable['range_start']}|{durable['range_end']}|{durable['origin_oid']}"
    )


def unique_migrated_observation(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """Locate the unique migrated observation. Never use selected[0] as a proxy."""

    selected = receipt.get("selected")
    if not isinstance(selected, list):
        raise Issue1895ReadinessError(
            "receipt selected is missing",
            code="RECEIPT_SELECTED_INVALID",
            stage="receipt",
        )
    migrated = [item for item in selected if isinstance(item, Mapping) and item.get("outcome") == "migrated"]
    if len(migrated) != 1:
        raise Issue1895ReadinessError(
            "receipt does not contain exactly one migrated observation",
            code="RECEIPT_MIGRATED_NOT_UNIQUE",
            stage="receipt",
        )
    return migrated[0]


def _observation_key(item: Mapping[str, Any]) -> str:
    durable = item.get("durable")
    if not isinstance(durable, Mapping):
        before = item.get("before")
        if isinstance(before, Mapping) and isinstance(before.get("durable"), Mapping):
            durable = before["durable"]
    if not isinstance(durable, Mapping):
        raise Issue1895ReadinessError(
            "observation durable identity is missing",
            code="RECEIPT_DURABLE_INVALID",
            stage="receipt",
        )
    return durable_key(durable)


def assert_sequential_tick_receipt(
    receipt: Mapping[str, Any],
    *,
    ordered_keys: Sequence[str],
    call_index: int,
    per_tick_bound: int = PER_TICK_BOUND,
    migrate_outcome: str = "migrated",
) -> Mapping[str, Any]:
    """Bind call i of six sequential ticks to shipping selected/deferred shape.

    With ordered keys K1..K6 and per_tick_bound=1, call i contains exactly one
    migrate observation for Ki. Prior K1..K(i-1) may appear as already_cold.
    Deferred equals suffix K(i+1)..K6 with reason per_tick_bound. The sixth
    suffix is empty; earlier calls must not require empty deferred.
    """

    keys = tuple(ordered_keys)
    if len(keys) != REQUIRE_COUNT or len(set(keys)) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "baseline ordered keys are not exactly six unique identities",
            code="RECEIPT_KEYS_INVALID",
            stage="receipt",
        )
    if call_index < 1 or call_index > REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "sequential call index is out of range",
            code="RECEIPT_CALL_INVALID",
            stage="receipt",
        )
    if per_tick_bound != PER_TICK_BOUND or receipt.get("per_tick_bound") != PER_TICK_BOUND:
        raise Issue1895ReadinessError(
            "per_tick_bound drifted from 1",
            code="RECEIPT_BOUND_INVALID",
            stage="receipt",
        )
    selected = receipt.get("selected")
    deferred = receipt.get("deferred")
    if not isinstance(selected, list) or not isinstance(deferred, list):
        raise Issue1895ReadinessError(
            "receipt selected/deferred is missing",
            code="RECEIPT_SELECTED_INVALID",
            stage="receipt",
        )
    expected_key = keys[call_index - 1]
    prior = keys[: call_index - 1]
    suffix = keys[call_index:]
    migrate_items = [item for item in selected if isinstance(item, Mapping) and item.get("outcome") == migrate_outcome]
    already_cold = [item for item in selected if isinstance(item, Mapping) and item.get("outcome") == "already_cold"]
    other = [
        item
        for item in selected
        if isinstance(item, Mapping) and item.get("outcome") not in {migrate_outcome, "already_cold"}
    ]
    if other:
        raise Issue1895ReadinessError(
            "receipt selected contains an unexpected outcome",
            code="RECEIPT_OUTCOME_INVALID",
            stage="receipt",
        )
    if len(migrate_items) != 1:
        raise Issue1895ReadinessError(
            "receipt does not contain exactly one migrate observation for this tick",
            code="RECEIPT_MIGRATED_NOT_UNIQUE",
            stage="receipt",
        )
    migrated_key = _observation_key(migrate_items[0])
    if migrated_key != expected_key:
        raise Issue1895ReadinessError(
            "migrated observation is not the expected sequential key",
            code="RECEIPT_MIGRATED_KEY_MISMATCH",
            stage="receipt",
        )
    already_keys = [_observation_key(item) for item in already_cold]
    if len(set(already_keys)) != len(already_keys):
        raise Issue1895ReadinessError(
            "already_cold observations are duplicated",
            code="RECEIPT_DUPLICATE_KEY",
            stage="receipt",
        )
    if set(already_keys) - set(prior):
        raise Issue1895ReadinessError(
            "already_cold observation is not a prior sequential key",
            code="RECEIPT_PRIOR_INVALID",
            stage="receipt",
        )
    selected_keys = [migrated_key, *already_keys]
    if len(set(selected_keys)) != len(selected_keys):
        raise Issue1895ReadinessError(
            "selected observations duplicate a key",
            code="RECEIPT_DUPLICATE_KEY",
            stage="receipt",
        )
    deferred_keys: list[str] = []
    for item in deferred:
        if not isinstance(item, Mapping):
            raise Issue1895ReadinessError(
                "deferred observation is not an object",
                code="RECEIPT_DEFERRED_INVALID",
                stage="receipt",
            )
        if item.get("reason") != "per_tick_bound":
            raise Issue1895ReadinessError(
                "deferred reason is not per_tick_bound",
                code="RECEIPT_DEFERRED_REASON",
                stage="receipt",
            )
        deferred_keys.append(_observation_key(item))
    if tuple(deferred_keys) != suffix:
        raise Issue1895ReadinessError(
            "deferred suffix drifted from the remaining ordered keys",
            code="RECEIPT_DEFERRED_SUFFIX",
            stage="receipt",
        )
    extra = (set(selected_keys) | set(deferred_keys)) - set(keys)
    if extra:
        raise Issue1895ReadinessError(
            "receipt contains a key outside the baseline six",
            code="RECEIPT_EXTRA_KEY",
            stage="receipt",
        )
    missing = set(keys) - (set(selected_keys) | set(deferred_keys))
    if missing - set(prior):
        raise Issue1895ReadinessError(
            "receipt is missing a sequential key",
            code="RECEIPT_MISSING_KEY",
            stage="receipt",
        )
    return migrate_items[0]
