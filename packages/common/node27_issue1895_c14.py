"""Exact C1-C4 command strings from the checked-in bringup checklist contract."""

from __future__ import annotations

C1_START_WRAPPER = "scripts/ops/start-display-api.sh"
C1_DISPLAY_UNIT = "nhms-display-api.service"
C1_HEALTH_PATH = "/health"
C1_RUNTIME_CONFIG_PATH = "/api/v1/runtime/config"
C1_SERVICE_ROLE = "display_readonly"
C1_OWNER = "scripts/node27_issue1895_display_runtime.py"
C1_BINDER = "scripts/node27_issue1895_display_runtime_bind.py"
C1_SCHEMA = "schemas/node27_issue1895_c1_display_runtime_receipt.schema.json"

C2_VALIDATOR = "scripts/validate_readonly_db_boundary.py"
C2_DSN_ENV = "NHMS_DISPLAY_READONLY_DATABASE_URL"
C2_ACCEPT_OWNER = "scripts/node27_issue1895_readonly_accept.py"
C2_ACCEPT_BINDER = "scripts/node27_issue1895_readonly_accept_bind.py"
C2_SCHEMA = "schemas/node27_issue1895_c2_readonly_boundary_receipt.schema.json"

# The generic full-scope aggregator remains fail-closed and is intentionally
# absent from G7: an empty new directory cannot supply its producer-complete
# twelve-lane bundle. #1895 uses the narrower exact-SHA current C3 owner below.
C3_CURRENT_OWNER = "scripts/node27_issue1895_publication_current.py"
C3_CURRENT_BINDER = "scripts/node27_issue1895_publication_current_bind.py"
C3_CURRENT_SCHEMA = "schemas/node27_issue1895_c3_current_publication_display_receipt.schema.json"
C3_AGGREGATION_COMMAND = ""
C3_AGGREGATION_TOKENS: tuple[str, ...] = ()

C4_LIVE_DISPLAY_COMMAND = (
    'corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run test:e2e:live-c4-display'
)
C4_DISPLAY_BINDER_TOKENS: tuple[str, ...] = (
    'node "$REPO_ROOT/apps/frontend/scripts/c4-receipt-binder.mjs"',
    '--receipt "$C4_RECEIPT"',
    '--frontend-origin "$FRONTEND_ORIGIN"',
    '--api-origin "$API_ORIGIN"',
    '--basin-id "$PLAYWRIGHT_LIVE_C4_BASIN_ID"',
    '--segment-id "$PLAYWRIGHT_LIVE_C4_SEGMENT_ID"',
    '--cmd-start "$C4_CMD_START" --cmd-end "$C4_CMD_END"',
)
C4_DISPLAY_SCHEMA = "schemas/frontend_c4_live_evidence.schema.json"
C4_RIVER_CLICK_COMMAND = (
    'corepack pnpm@10.11.0 --dir "$REPO_ROOT/apps/frontend" run test:e2e:live-river-click'
)
C4_RIVER_CLICK_BINDER = (
    'node "$REPO_ROOT/apps/frontend/scripts/river-click-receipt-binder.mjs" '
    '--receipt "$RECEIPT" '
    '--frontend-origin "$PLAYWRIGHT_LIVE_BASE_URL" '
    '--api-origin "$PLAYWRIGHT_LIVE_API_BASE_URL" '
    '--basin-id "$PLAYWRIGHT_LIVE_RIVER_BASIN_ID" '
    '--segment-id "$PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID" '
    '--cmd-start "$CMD_START" --cmd-end "$CMD_END"'
)
C4_RIVER_CLICK_SCHEMA = "schemas/frontend_river_click_live_evidence.schema.json"

FORBIDDEN_G7_EMPTY_AGGREGATOR_TOKENS: tuple[str, ...] = (
    "scripts/validate_two_node_e2e_evidence.py",
    "--full-scope",
    "EVIDENCE_PARENT=\"$RUN_ROOT/receipts/c3\"",
)

FORBIDDEN_PLACEHOLDERS: tuple[str, ...] = (
    "<root>",
    "<id>",
    "<live>",
    "<receipt>",
    "PLAYWRIGHT_LIVE_BASE_URL=<live>",
    "PLAYWRIGHT_LIVE_API_BASE_URL=<live>",
)


def fence_forbids_empty_aggregator(text: str) -> None:
    from packages.common.node27_issue1895_types import Issue1895ReadinessError

    for token in FORBIDDEN_G7_EMPTY_AGGREGATOR_TOKENS:
        if token in text:
            raise Issue1895ReadinessError(
                "G7 must not claim an empty full-scope C3 aggregator bundle",
                code="C14_EMPTY_AGGREGATOR",
                stage="c14",
            )


def fence_forbids_placeholders(text: str) -> None:
    from packages.common.node27_issue1895_types import Issue1895ReadinessError

    for token in FORBIDDEN_PLACEHOLDERS:
        if token in text:
            raise Issue1895ReadinessError(
                "C1-C4 fence still contains a placeholder",
                code="C14_PLACEHOLDER",
                stage="c14",
            )
