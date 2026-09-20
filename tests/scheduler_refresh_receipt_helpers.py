"""Receipt and classification corpora shared by the refresh suites.

Non-collectible support module (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). These are the static payload
builders and validators the receipt-contract partitions share: the
classification stubs, the reconciliation formula helper, the enforced
cutover-gate block, the dry-run / id-only / full mode classifications, the
`_classify_registry` driver and the JSON-Schema validator factory.

`_receipt_schema_validator` is the module's one non-mechanical carry-over. The
monolith defined that name TWICE at module scope -- `:6959` (with a
`jsonschema.FormatChecker`) and `:9514` (without). Python binds the later one,
so all 24 call sites ran the `:9514` body and the `:6959` body was dead. The
`:9514` body is what moved here verbatim; keeping the dead twin would either
change behaviour (if it won a partition's binding) or trip ruff F811.

The sibling module `tests/scheduler_refresh_helpers.py` imports this one; this
module imports nothing from it, so the two form a DAG rather than a cycle.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from scripts import scheduler_file_provider_refresh as refresh


def _classification_stub() -> dict[str, object]:
    return {
        "previous_registry_sha256": None,
        "new_registry_sha256": None,
        # R2-N1: partition counts are required; empty classification pins
        # both to zero (matches the empty added/unchanged/etc buckets).
        "previous_model_count": None,
        "prospective_model_count": 0,
        "added": {"items": [], "total": 0, "truncated": False},
        "unchanged": {"items": [], "total": 0, "truncated": False},
        "removed": {"items": [], "total": 0, "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }


def _assert_classification_reconciles(
    classification: dict[str, object],
    *,
    previous_count: int | None,
    prospective_count: int | None,
) -> None:
    """Assert every reconciliation formula from spec.md:397-403 (T3 / C-E4).

    * ``unchanged + package_changed + removed == previous_count`` when a
      previous canonical registry existed;
    * ``added + unchanged + package_changed == prospective_count`` unless
      the caller passes ``prospective_count=None`` (dry_run id-only mode);
    * ``declared_cutovers`` model_ids ⊆ ``package_changed`` model_ids;
    * ``refused`` ⊇ ``removed`` ∪ (``package_changed`` \\ ``declared_cutovers``).
    """
    def total(group: str) -> int:
        return int(classification[group]["total"])  # type: ignore[index]

    def ids(group: str) -> set[str]:
        return {
            str(item["model_id"])  # type: ignore[index]
            for item in classification[group]["items"]  # type: ignore[index]
            if isinstance(item, dict)
        }

    added = total("added")
    unchanged = total("unchanged")
    removed = total("removed")
    package_changed = total("package_changed")
    refused = total("refused")
    declared = total("declared_cutovers")

    if previous_count is not None:
        assert unchanged + package_changed + removed == previous_count, (
            f"previous reconciliation broken: unchanged={unchanged} "
            f"package_changed={package_changed} removed={removed} "
            f"previous_count={previous_count}"
        )
    if prospective_count is not None:
        assert added + unchanged + package_changed == prospective_count, (
            f"prospective reconciliation broken: added={added} "
            f"unchanged={unchanged} package_changed={package_changed} "
            f"prospective_count={prospective_count}"
        )
    assert ids("declared_cutovers") <= ids("package_changed")
    # refused >= removed + (package_changed - declared_cutovers)
    assert refused >= removed + max(package_changed - declared, 0)


def _dry_run_classification(
    *,
    previous_sha: str | None,
    previous_count: object,
    added: list[str],
    unchanged: list[str],
    removed: list[str],
    prospective_count: int,
    new_registry_sha256: str | None = None,
) -> dict[str, object]:
    """#1135: dry_run classification payload shaped like ``_classify_registry``'s
    id-only output — ``package_changed``/``refused``/``declared_cutovers`` are
    empty by construction.  Every #1135 test below keeps
    ``added + unchanged == prospective_count`` so the PRE-existing dry_run
    checks accept the payload and only the tampered field can reject it."""
    return {
        "previous_registry_sha256": previous_sha,
        "new_registry_sha256": new_registry_sha256,
        "previous_model_count": previous_count,
        "prospective_model_count": prospective_count,
        "added": {"items": added, "total": len(added), "truncated": False},
        "unchanged": {
            "items": unchanged,
            "total": len(unchanged),
            "truncated": False,
        },
        "removed": {"items": removed, "total": len(removed), "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }


def _enforced_cutover_gate(*, declaration_present: bool) -> dict[str, Any]:
    """The audit block a run that reached the gate construction must persist."""
    return {
        "mode": "enforced",
        "declaration_env": refresh.CUTOVER_DECLARATION_ENV,
        "declaration_present": declaration_present,
    }


# ---------------------------------------------------------------------------
# #1140: reconciliation keys on the classification mode, not the receipt outcome
# ---------------------------------------------------------------------------


# The synthetic marker `_registry_precommit_gate` appends after
# `_classify_registry` when the declaration file itself fails to load — the ONLY
# refusal an id-only classification can carry.
_DECLARATION_REFUSAL_ITEM: dict[str, Any] = {
    "model_id": "__declaration__",
    "old_checksum": None,
    "new_checksum": None,
    "reason": "registry_cutover_declaration_invalid",
}


def _mode_classification(
    *,
    mode: str | None = "id_only",
    refused_items: list[dict[str, Any]] | None = None,
    **shape: Any,
) -> dict[str, Any]:
    """#1140: `_dry_run_classification`'s id-only shape plus the `mode` key.

    ``mode=None`` reproduces a legacy (pre-#1140) on-disk receipt, which is the
    corpus the outcome-keyed fallback must keep handling verbatim.
    """
    classification = _dry_run_classification(**shape)
    items = list(refused_items or [])
    classification["refused"] = {
        "items": items,
        "total": len(items),
        "truncated": False,
    }
    if mode is not None:
        classification["mode"] = mode
    return classification


def _classification_receipt(
    classification: dict[str, Any], *, outcome: str, reason: str
) -> dict[str, Any]:
    """The receipt slice `_validate_registry_classification_field` reads.

    Its signature takes the whole receipt (outcome/reason drive the branch), not
    the classification block.
    """
    return {
        "outcome": outcome,
        "reason": reason,
        "registry_classification": classification,
    }


# A classification legal under BOTH branches (bootstrap, everything zero except
# one added row): whatever rejects the payloads below is the mode/outcome
# cross-pin itself, never an upstream arm.
_CROSS_PIN_SHAPE: dict[str, Any] = {
    "previous_sha": None,
    "previous_count": None,
    "added": ["basin-101"],
    "unchanged": [],
    "removed": [],
    "prospective_count": 1,
}


def _receipt_with_classification(
    classification: Any, *, outcome: str, reason: str
) -> dict[str, Any]:
    """Fully-shaped receipt (provider triple included) carrying `classification`."""
    provider = {
        "name": "registry",
        "before_sha256": "1" * 64,
        "before_inode": None,
        "before_schema_version": "v1",
        "before_generated_at": "2026-07-14T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }
    return refresh._receipt(
        run_id="refresh_classification_mode",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome=outcome,
        reason=reason,
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=classification,
        # #1144: `published`/`dry_run` receipts must carry the audit block, so
        # the mode corpus keeps discriminating on `mode` alone.
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )


# ---------------------------------------------------------------------------
# #1433: declared retirement channel — fixtures for the `_classify_registry`
# decision grid.  #1101 put the grid itself in
# tests/test_scheduler_refresh_retirement_reconciliation.py, and moved only the
# payload builders below into this helper.
# ---------------------------------------------------------------------------


_CLASSIFY_GENERATED_AT = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)


def _declaration_payload(
    *, generation: str, entries: list[dict[str, object]]
) -> dict[str, object]:
    """In-memory declaration exactly as `_load_cutover_declaration` returns it."""
    return {
        "schema_version": "nhms.scheduler.registry_package_cutover.v1",
        "generated_at": "2026-07-14T12:00:00Z",
        "generation": generation,
        "entries": entries,
    }


def _classify(
    *,
    previous: list[dict[str, object]] | None,
    prospective: list[dict[str, object]],
    entries: list[dict[str, object]] | None = None,
    generation: str | None = None,
    dry_run: bool = False,
    skipped_models: dict[str, dict[str, object]] | None = None,
) -> tuple[Any, str | None]:
    """Direct `_classify_registry` call with a matching-generation declaration."""
    resolved_generation = refresh._prospective_registry_generation(
        prospective, generated_at=_CLASSIFY_GENERATED_AT
    )
    declaration = (
        None
        if entries is None
        else _declaration_payload(
            generation=generation if generation is not None else resolved_generation,
            entries=entries,
        )
    )
    return refresh._classify_registry(
        previous=previous,
        prospective=prospective,
        previous_sha256=None if previous is None else "1" * 64,
        new_sha256="2" * 64,
        generation=resolved_generation,
        declaration=declaration,
        dry_run=dry_run,
        skipped_models=skipped_models,
    )


def _receipt_schema_validator() -> Any:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    return jsonschema.Draft202012Validator(schema)
