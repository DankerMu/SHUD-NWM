"""G5 pre-movement census binder: load both JSON paths before indexing."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from packages.common.node27_cold_residency_census_policy import (
    CensusPolicyError,
    capacity_policy,
    validate_reviewed_count,
)
from packages.common.node27_issue1895_private_receipt import read_held_private_json, read_held_private_text
from packages.common.node27_issue1895_probe import assert_report_within_command_bracket, parse_bracket_instant
from packages.common.node27_issue1895_receipt import durable_key
from packages.common.node27_issue1895_types import Issue1895ReadinessError


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


def validate_original_census(document: object, *, expected_count: int, reviewed_sha: str) -> tuple[dict, int]:
    """Validate original all-source authority before any identity map is constructed."""
    from packages.common.node27_issue1895_post_target import require_baseline_window_parity

    def refuse(message: str, code: str = "CENSUS_KEYS_INVALID") -> None:
        raise Issue1895ReadinessError(message, code=code, stage="census")

    try:
        count = validate_reviewed_count(expected_count)
    except CensusPolicyError:
        refuse("reviewed census count is invalid")
    if not isinstance(document, dict):
        refuse("original census is not an object")
    if document.get("verdict") != "GO":
        refuse("original census is not GO", "CENSUS_VERDICT_NO_GO")
    if (
        not isinstance(reviewed_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", reviewed_sha)
        or document.get("head_sha") != reviewed_sha
    ):
        refuse("original census head is not reviewed", "CENSUS_HEAD_MISMATCH")
    _utc(document.get("generated_at"))
    config = document.get("config")
    inventory = document.get("inventory")
    if not isinstance(inventory, Mapping) or any(
        not isinstance(inventory.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", inventory[field])
        for field in ("digest", "river_digest", "forcing_digest")
    ):
        refuse("original inventory is invalid", "CENSUS_PREIMAGE_DRIFT")
    for value in (
        document.get("required_group_count"),
        document.get("resolved_group_count"),
        config.get("require_count") if isinstance(config, Mapping) else None,
    ):
        if type(value) is not int or value != count:
            refuse("original count echoes disagree")
    keys, groups = document.get("group_keys"), document.get("groups")
    if not isinstance(keys, list) or not isinstance(groups, list) or len(keys) != count or len(groups) != count:
        refuse("original raw cardinality disagrees")
    if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != count:
        refuse("original keys are not unique")
    if any(not isinstance(group, Mapping) for group in groups):
        refuse("original group is not an object")
    if [group.get("key") for group in groups] != keys:
        refuse("original ordered groups disagree")
    if (
        document.get("residency_counts") != {"all_source": count}
        or type(document["residency_counts"].get("all_source")) is not int
    ):
        refuse("original is not complete-source", "CENSUS_RESIDENCY_DRIFT")
    expansions, retained = [], []
    for key, group in zip(keys, groups, strict=True):
        identity = group.get("durable")
        if not isinstance(identity, Mapping) or durable_key(identity) != key:
            refuse("original durable identity disagrees")
        if group.get("residency") != "all_source" or group.get("is_compressed") is not True:
            refuse("original group is not complete-source", "CENSUS_RESIDENCY_DRIFT")
        members = group.get("members")
        if not isinstance(members, list) or not members or group.get("member_count") != len(members):
            refuse("original members are incomplete")
        if not group.get("compressed") or any(
            not isinstance(member, Mapping) or member.get("tablespace") != "pg_default" for member in members
        ):
            refuse("original members are not complete-source")
        if any(type(member.get("bytes")) is not int or member["bytes"] < 0 for member in members):
            refuse("original measured members are invalid", "CENSUS_CAPACITY_DRIFT")
        if sum(member["bytes"] for member in members) != group.get("retained_source_bytes"):
            refuse("original retained bytes disagree with measured members", "CENSUS_CAPACITY_DRIFT")
        parity = require_baseline_window_parity(group.get("parity"))
        if any(parity[field] != identity[field] for field in ("range_start", "range_end")):
            refuse("original parity window disagrees", "CENSUS_PREIMAGE_DRIFT")
        if parity["inventory_digest"] != group.get("inventory_digest"):
            refuse("original inventory disagrees", "CENSUS_PREIMAGE_DRIFT")
        expansions.append(group.get("before_compression_total_bytes"))
        retained.append(group.get("retained_source_bytes"))
    try:
        policy = capacity_policy(expansions=expansions, retained=retained, group_count=count)
    except CensusPolicyError:
        refuse("original measured capacity is invalid", "CENSUS_CAPACITY_DRIFT")
    if document.get("capacity_policy") != policy:
        refuse("original capacity policy disagrees", "CENSUS_CAPACITY_DRIFT")
    bound = [{"key": group["key"], "group_digest": group.get("group_digest")} for group in groups]
    digest = hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if document.get("census_digest") != digest:
        refuse("original semantic digest disagrees", "CENSUS_DIGEST_DRIFT")
    if (
        document.get("census_key_set_digest")
        != hashlib.sha256(json.dumps(sorted(keys), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ):
        refuse("original key-set digest disagrees", "CENSUS_DIGEST_DRIFT")
    return document, count


def load_original_census(
    path: str | Path,
    *,
    expected_original_sha256: str,
    reviewed_sha: str,
) -> tuple[dict, int]:
    """Compare the held byte digest with G1 authority, never reopened expected bytes."""
    _raw, document, facts = read_held_private_json(
        Path(path),
        label="original census",
        stage="census",
        unreadable_code="CENSUS_JSON_INVALID",
        identity_code="CENSUS_JSON_INVALID",
        toctou_code="CENSUS_JSON_INVALID",
        json_code="CENSUS_JSON_INVALID",
    )
    if not isinstance(expected_original_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_original_sha256):
        raise Issue1895ReadinessError("original hash authority is invalid", code="CENSUS_HASH_INVALID", stage="census")
    if facts["sha256"] != expected_original_sha256:
        raise Issue1895ReadinessError("original frozen bytes changed", code="CENSUS_HASH_DRIFT", stage="census")
    return validate_original_census(
        document,
        expected_count=document.get("required_group_count") if isinstance(document, dict) else None,
        reviewed_sha=reviewed_sha,
    )


def bind_pre_movement_census(
    *,
    current_path: str | Path,
    original_path: str | Path,
    expected_digest: str,
    bracket_path: str | Path,
    reviewed_sha: str,
    expected_original_sha256: str,
) -> dict:
    """Load current and original census JSON, then bind G5 pre-movement identity."""

    original, count = load_original_census(
        original_path,
        expected_original_sha256=expected_original_sha256,
        reviewed_sha=reviewed_sha,
    )
    current = _load_object(current_path, label="current")
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
    if current_keys != original_keys or len(current_keys) != count:
        raise Issue1895ReadinessError(
            "pre-movement census is not the same ordered durable groups",
            code="CENSUS_KEYS_DRIFT",
            stage="census",
        )
    if current.get("residency_counts") != {"all_source": count}:
        raise Issue1895ReadinessError(
            "pre-movement census is not complete-source",
            code="CENSUS_RESIDENCY_DRIFT",
            stage="census",
        )
    validate_original_census(current, expected_count=count, reviewed_sha=reviewed_sha)
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
