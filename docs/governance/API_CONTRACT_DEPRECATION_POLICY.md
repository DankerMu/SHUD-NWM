# API Contract Deprecation Policy

Generated: 2026-06-11

Scope: Governance-5 E3 issue #412 policy for the candidates inventoried in
`docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`. This is planning and
governance evidence only. It does not change API behavior, OpenAPI, generated
frontend types, frontend implementation, backend runtime code, tests, CI, live
receipts, response metadata, or deprecation headers.

## Current Decision

Issue #412 marks no current endpoint deprecated and no current endpoint
removal-ready.

Repository migration evidence is necessary but not sufficient for external API
deprecation. External consumers are unknown, so a later issue must not treat
"all repository consumers migrated" as proof that external clients have been
migrated or notified.

The #411 inventory remains the candidate source of truth for this policy. Later
issues may update the inventory with new evidence, but they must not contract
OpenAPI or generated frontend types until the migration and rollback gates below
are satisfied.

## Policy Vocabulary

- `retain compatibility`: keep the current contract active and compatible.
- `migrate consumers first`: move known consumers only after replacement
  behavior is documented and tested.
- `docs-only cleanup`: update stale documentation references without implying a
  runtime endpoint existed or needs runtime deprecation metadata.
- `explicit deferral`: leave the active contract in place because no replacement
  or consumer-migration evidence justifies contraction.

## Candidate Policy Matrix

| Candidate | Policy decision | Replacement / compatibility stance | Deprecation metadata | Migration order | Rollback expectations | Follow-up issue | Removal / defer gate |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/mvp/qhh/latest-product` | Retain compatibility; not deprecated and not removal-ready in #412. | No replacement endpoint is documented or implemented by #411/#412. Keep the current route, OpenAPI entry, generated types, response shape, identity semantics, tests, frontend bootstrap, and runbook compatibility intact until a replacement is selected and proven. | Now: not appropriate. Later: possible only after #413/#414/#415 produce replacement and migration evidence, and #416 records an external-consumer treatment. Response metadata remains inappropriate unless a later contract change proves it is compatible with existing clients. | #413 defines any backend/test migration if a replacement is chosen; #414 owns node-27 frontend/display bootstrap migration; #415 synchronizes OpenAPI, generated types, and docs only after migration evidence; #416 decides removal or explicit deferral. | Keep the old route and contract available throughout migration. If a replacement breaks backend tests, display bootstrap, generated types, or target-node validation, revert consumers to `latest-product` and do not contract OpenAPI. | #413, #414, #415, #416 | #416 may consider removal only after repository consumers are migrated, OpenAPI/types/docs are synchronized, compatibility tests pass, rollback is demonstrated, and unknown external consumers are handled by an explicit deprecation/notice decision. Otherwise defer. |
| `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series` | Retain as the canonical active forecast-series API; not deprecated and not removal-ready in #412. | This is the current canonical runtime/OpenAPI/generated contract and the replacement target for docs-only shorthand references. No alternate replacement is selected by #411/#412. | Now: not appropriate. Later in Governance-5 E3: not appropriate unless a new replacement is explicitly introduced by a later issue. | No #413/#414 consumer migration is required for this route under current #412 policy. #415 may align docs to the canonical path. #416 should explicitly defer any route contraction while consumers remain. | Preserve the route, OpenAPI path, generated frontend type, and existing consumer behavior. If docs synchronization causes ambiguity, restore the canonical path wording and leave runtime contracts untouched. | #415 for docs alignment; #416 for explicit deferral; #413/#414 only if a later replacement decision changes this policy. | Defer removal. A future removal gate would require a separately documented replacement, repository and external-consumer treatment, synchronized OpenAPI/types, and passing contract/display evidence. |
| Docs-only shorthand forecast-series family: `GET /api/v1/river-segments/{segment_id}/forecast-series`, `GET /api/v1/river-segments/{id}/forecast-series`, and relative `/river-segments/{segment_id}/forecast-series` | Docs-only cleanup or historical retention; not a runtime endpoint, not deprecated, and not removal-ready in #412. | The compatibility stance is documentation correction, not runtime API migration. The canonical replacement wording is `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`. | Runtime metadata: never appropriate because #411 found no current route/OpenAPI/generated contract to attach headers or response metadata to. Docs-only markers are appropriate later in #415 if a stale reference must be retained for historical context. | #413 has no backend route migration unless a hidden runtime consumer is discovered. #414 has no node-27 frontend migration unless a hidden frontend consumer is discovered. #415 updates stale docs or marks them historical. #416 records docs removal or explicit retention. | No runtime rollback is needed. If a docs cleanup removes useful historical context, restore it with an explicit historical or superseded marker and keep the canonical active path clear. | #415, #416; #413/#414 only if new consumer evidence appears. | Gate is documentation cleanup, not endpoint removal: #416 can close the row only after stale docs are updated, removed, or explicitly retained as historical. If a real runtime consumer is found, reclassify the candidate before acting. |

## Deprecation Metadata Rules

Do not add deprecation headers, response metadata, OpenAPI `deprecated: true`,
or frontend-generated deprecation markers in #412.

Future metadata is allowed only when all of these are true:

- A replacement endpoint or explicit compatibility policy is documented.
- Repository consumers have been migrated or intentionally retained with
  evidence.
- External-consumer risk has a documented treatment; unknown external consumers
  cannot be dismissed by repository-only evidence.
- OpenAPI, generated types, API tests, frontend checks, runbooks, and rollback
  notes are updated in the same implementation slice or in a documented staged
  sequence that keeps compatibility intact.

Docs-only shorthand references must use docs status markers or wording changes,
not runtime deprecation headers or response metadata.

## Migration Order

1. #413 handles backend/test migration only if a replacement is selected for an
   active route.
2. #414 handles node-27 frontend/display migration and must remain the owner of
   frontend implementation work.
3. #415 synchronizes OpenAPI, generated frontend types, API docs, and stale
   documentation references after consumer migration evidence exists.
4. #416 makes the removal or explicit deferral decision, including any
   external-consumer treatment.

No later issue should skip ahead from repository search evidence directly to
OpenAPI contraction.

## Rollback Policy

Active routes stay compatible unless #416 records a removal decision after all
gates pass. If #416 records explicit deferral, compatibility remains in force.
Rollback for active runtime contracts means reverting consumers to the current
route and leaving route behavior, OpenAPI, and generated types intact.

Rollback for docs-only shorthand references means restoring or rewording docs
with explicit historical context. It must not create runtime compatibility
claims for routes that #411 found absent from route definitions, OpenAPI, and
generated frontend types.

## Follow-Up Responsibilities

- #413: backend/test migration only when a replacement for an active route is
  selected. It must not remove active routes or contract OpenAPI first.
- #414: node-27 frontend migration. It remains the owner for display bootstrap,
  stores, generated client usage, and frontend verification.
- #415: OpenAPI/generated type/docs synchronization after migration evidence.
  It also owns stale shorthand forecast-series documentation cleanup or
  historical marking.
- #416: final removal or explicit deferral. It must record the external-consumer
  stance before treating a public HTTP contract as deprecated or removal-ready.
