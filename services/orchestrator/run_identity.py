"""Canonical run-id shapes, shared by the journal and retention (#1405).

The journal has always adjudicated run ids by canonical shape; retention used
to split a run id on ``_`` and take the first token that happened to parse as
``%Y%m%d%H``. That divergence let any stray ``runs/`` directory carrying a
ten-digit token (a manual salvage capture, a debugging snapshot, a foreign
writer's output) be adjudicated as an expired run workspace, and could bind a
forecast run to the wrong embedded timestamp. Both sides now read the shapes
from here.

The shapes deliberately do NOT validate the source segment against a closed
source set, so a name that imitates a full canonical shape (``fcst_salvage_
<cycle>_x``) is still recognized. The over-acceptance surface is narrowed, not
closed.

This module has no ``services`` imports on purpose: retention and the journal
both depend on it, and neither may pull the other in.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

# ``fcst_{source}_{cycle}_{model_id}`` — the model id may itself contain
# underscores, and may itself look like a timestamp.
FORECAST_RUN_ID_RE = re.compile(r"^fcst_([^_]+)_(\d{10})_(.+)$")
# ``cycle_{source}_{cycle}`` with an optional trailing segment. Retention must
# keep recycling cohort workspaces that carry the suffix, so this is the loose
# variant; the journal keeps its own strict ``cycle_{source}_{cycle}`` regex
# for the consumer that needs an exact cohort id.
CYCLE_COHORT_RUN_ID_RE = re.compile(r"^cycle_([^_]+)_(\d{10})(?:_.+)?$")
# ``analysis_{source}_{start}_{end}_{model_id}`` — the cycle is the START
# timestamp, matching the analysis lane's own ``cycle_time=start_time``
# (chain_analysis.py).
ANALYSIS_RUN_ID_RE = re.compile(r"^analysis_([^_]+)_(\d{10})_(\d{10})_(.+)$")

_CYCLE_GROUP = 2
_CANONICAL_RUN_ID_RES = (FORECAST_RUN_ID_RE, CYCLE_COHORT_RUN_ID_RE, ANALYSIS_RUN_ID_RE)


def parse_run_cycle(run_id: str) -> datetime | None:
    """Return the UTC cycle of a canonical run id, or None.

    None means "not a recognized run workspace": either the name matches none
    of the canonical shapes, or its cycle token sits at the canonical position
    but is not a real ``%Y%m%d%H`` timestamp. There is no fallback to another
    token — a caller that deletes on this answer must not be handed a
    timestamp the run id never claimed.
    """
    for pattern in _CANONICAL_RUN_ID_RES:
        matched = pattern.fullmatch(run_id)
        if matched is None:
            continue
        try:
            return datetime.strptime(matched.group(_CYCLE_GROUP), "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None
