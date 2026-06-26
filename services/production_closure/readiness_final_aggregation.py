from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

SUMMARY_ARTIFACT_REFS = [
    "preflight.json",
    "live_proof_receipts.json",
    "readiness_items.json",
    "release_blockers.json",
    "environment.json",
    "summary.json",
]

SUMMARY_INTERPRETATION = (
    "Deterministic readiness evidence is useful for review but is not live production proof. "
    "Final production readiness remains false until every required live proof item is accepted."
)


def _release_blockers(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers = []
    for item in items:
        status = str(item["status"])
        if status not in {"failed", "blocked", "release_blocked"} and not (
            item["required_for_final"] and not item["live_proof_accepted"]
        ):
            continue
        blockers.append(
            {
                "blocker_id": f"m19-{item['item_id']}",
                "surface": item["surface"],
                "status": status,
                "execution_mode": item["execution_mode"],
                "owner": item["owner"],
                "action": item["action"],
                "residual_risk": item["residual_risk"],
                "removal_criteria": item["removal_criteria"],
                "artifact_refs": list(item["artifact_refs"]),
                "required_for_final": item["required_for_final"],
                "live_proof_accepted": item["live_proof_accepted"],
            }
        )
    return blockers


def _final_ready(items: Sequence[Mapping[str, Any]]) -> bool:
    for item in items:
        if item["status"] in {"failed", "blocked", "release_blocked"}:
            return False
        if item["required_for_final"] and (item["status"] != "passed" or item["live_proof_accepted"] is not True):
            return False
    return True


def _summary_exclusions(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    exclusions = []
    for item in items:
        for exclusion in item.get("exclusions", []):
            exclusions.append(
                {
                    "surface": item["surface"],
                    "status": item["status"],
                    **dict(exclusion),
                }
            )
    return exclusions


def _release_blocker_payload(
    config: Any,
    items: Sequence[Mapping[str, Any]],
    *,
    release_blockers: Sequence[Mapping[str, Any]],
    final_ready: Callable[[Sequence[Mapping[str, Any]]], bool],
    summary_exclusions: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "schema": "nhms.production_readiness.release_blockers.v1",
        "issue": 181,
        "run_id": config.run_id,
        "generated_at": _now(),
        "final_production_readiness_claimed": final_ready(items),
        "blockers": list(release_blockers),
        "exclusions": summary_exclusions(items),
    }


def _summary_payload(
    config: Any,
    items: Sequence[Mapping[str, Any]],
    *,
    release_blockers: Sequence[Mapping[str, Any]],
    final_ready: Callable[[Sequence[Mapping[str, Any]]], bool],
    summary_exclusions: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]],
    path_for_evidence: Callable[..., str],
) -> dict[str, Any]:
    ready = final_ready(items)
    return {
        "schema": "nhms.production_readiness.summary.v1",
        "issue": 181,
        "run_id": config.run_id,
        "status": "ready" if ready else "release_blocked",
        "evidence_dir": path_for_evidence(config.lane_dir, config=config),
        "generated_at": _now(),
        "final_production_readiness_claimed": ready,
        "deterministic_item_count": sum(1 for item in items if item["execution_mode"] != "live_proof"),
        "live_proof_item_count": sum(1 for item in items if item["execution_mode"] == "live_proof"),
        "required_live_proof_count": sum(1 for item in items if item["required_for_final"]),
        "accepted_live_proof_count": sum(
            1 for item in items if item["required_for_final"] and item["live_proof_accepted"]
        ),
        "release_blockers": list(release_blockers),
        "exclusions": summary_exclusions(items),
        "artifact_refs": list(SUMMARY_ARTIFACT_REFS),
        "interpretation": SUMMARY_INTERPRETATION,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
