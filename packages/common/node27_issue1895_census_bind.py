"""G5 pre-movement census binder: load both JSON paths before indexing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from packages.common.node27_issue1895_private_receipt import read_held_private_json, read_held_private_text
from packages.common.node27_issue1895_probe import assert_report_within_command_bracket, parse_bracket_instant
from packages.common.node27_issue1895_types import Issue1895ReadinessError

REQUIRE_COUNT = 6


def _load_object(path: str | Path, *, label: str) -> dict:
    target = Path(path)
    try:
        _raw, payload, _facts = read_held_private_json(
            target,
            label=f"{label} census",
            stage="census",
            unreadable_code="CENSUS_JSON_INVALID",
            identity_code="CENSUS_JSON_INVALID",
            toctou_code="CENSUS_JSON_INVALID",
            json_code="CENSUS_JSON_INVALID",
        )
    except Issue1895ReadinessError as error:
        if error.code.startswith("READINESS_INPUT_"):
            raise Issue1895ReadinessError(
                f"{label} census is not a private identity-bound JSON object",
                code="CENSUS_JSON_INVALID",
                stage="census",
            ) from error
        raise
    if not isinstance(payload, dict):
        raise Issue1895ReadinessError(
            f"{label} census is not an object",
            code="CENSUS_JSON_INVALID",
            stage="census",
        )
    return payload


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise Issue1895ReadinessError(
            "census instant is missing",
            code="CENSUS_INSTANT_INVALID",
            stage="census",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise Issue1895ReadinessError(
            "census instant is not calendar-valid and timezone-aware",
            code="CENSUS_INSTANT_INVALID",
            stage="census",
        ) from None


def bind_pre_movement_census(
    *,
    current_path: str | Path,
    original_path: str | Path,
    expected_digest: str,
    bracket_path: str | Path,
    reviewed_sha: str,
) -> dict:
    """Load current and original census JSON, then bind G5 pre-movement identity."""

    current = _load_object(current_path, label="current")
    original = _load_object(original_path, label="original")
    try:
        bracket_text = read_held_private_text(
            Path(bracket_path),
            label="census bracket",
            stage="census",
            unreadable_code="CENSUS_BRACKET_INVALID",
            identity_code="CENSUS_BRACKET_INVALID",
            toctou_code="CENSUS_BRACKET_INVALID",
        )
    except Issue1895ReadinessError as error:
        if error.code.startswith("READINESS_INPUT_"):
            raise Issue1895ReadinessError(
                "pre-movement census bracket is not a private identity-bound current-run",
                code="CENSUS_BRACKET_INVALID",
                stage="census",
            ) from error
        raise
    lines = [line.strip() for line in bracket_text.splitlines() if line.strip()]
    if len(lines) != 3 or lines[2] != "0":
        raise Issue1895ReadinessError(
            "pre-movement census bracket is not a successful current-run",
            code="CENSUS_BRACKET_INVALID",
            stage="census",
        )
    if current.get("verdict") != "GO":
        raise Issue1895ReadinessError(
            "pre-movement census verdict is not GO",
            code="CENSUS_VERDICT_NO_GO",
            stage="census",
        )
    if current.get("head_sha") != reviewed_sha:
        raise Issue1895ReadinessError(
            "pre-movement census head_sha is not REVIEWED_SHA",
            code="CENSUS_HEAD_MISMATCH",
            stage="census",
        )
    generated = _utc(current.get("generated_at"))
    assert_report_within_command_bracket(
        report_mtime=generated.timestamp(),
        start=parse_bracket_instant(lines[0]),
        end=parse_bracket_instant(lines[1]),
    )
    if current.get("census_digest") != expected_digest:
        raise Issue1895ReadinessError(
            "pre-movement census digest drifted",
            code="CENSUS_DIGEST_DRIFT",
            stage="census",
        )
    current_keys = current.get("group_keys")
    original_keys = original.get("group_keys")
    if not isinstance(current_keys, list) or not isinstance(original_keys, list):
        raise Issue1895ReadinessError(
            "census group_keys are missing",
            code="CENSUS_KEYS_INVALID",
            stage="census",
        )
    if set(current_keys) != set(original_keys) or len(current_keys) != REQUIRE_COUNT:
        raise Issue1895ReadinessError(
            "pre-movement census is not the same six durable groups",
            code="CENSUS_KEYS_DRIFT",
            stage="census",
        )
    if current.get("residency_counts") != {"all_source": REQUIRE_COUNT}:
        raise Issue1895ReadinessError(
            "pre-movement census is not complete-source",
            code="CENSUS_RESIDENCY_DRIFT",
            stage="census",
        )
    current_groups = {group.get("key"): group for group in current.get("groups") or [] if isinstance(group, Mapping)}
    original_groups = {group.get("key"): group for group in original.get("groups") or [] if isinstance(group, Mapping)}
    if set(current_groups) != set(original_groups) or set(current_groups) != set(current_keys):
        raise Issue1895ReadinessError(
            "pre-movement group map drifted",
            code="CENSUS_GROUP_MAP_DRIFT",
            stage="census",
        )
    for key, group in current_groups.items():
        before = original_groups[key]
        if group.get("residency") != "all_source" or before.get("residency") != "all_source":
            raise Issue1895ReadinessError(
                "pre-movement group is not complete-source",
                code="CENSUS_RESIDENCY_DRIFT",
                stage="census",
            )
        for field in (
            "group_digest",
            "parity",
            "inventory_digest",
            "before_compression_total_bytes",
            "retained_source_bytes",
        ):
            if group.get(field) != before.get(field):
                raise Issue1895ReadinessError(
                    "complete-source preimage drifted",
                    code="CENSUS_PREIMAGE_DRIFT",
                    stage="census",
                )
    current_policy = current.get("capacity_policy") or {}
    original_policy = original.get("capacity_policy") or {}
    if current_policy.get("S") != original_policy.get("S") or current_policy.get("E") != original_policy.get("E"):
        raise Issue1895ReadinessError(
            "capacity policy inputs drifted",
            code="CENSUS_CAPACITY_DRIFT",
            stage="census",
        )
    return current
