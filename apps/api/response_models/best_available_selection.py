"""Response model for ``apps/api/routes/best_available.py`` (#2348).

The route returns a bare list (no ``_ok`` envelope): each item is
``_selection_row_response`` (``packages/common/best_available.py``), whose
timestamps are already ``_format_time`` strings.
"""

from __future__ import annotations

from apps.api.response_models.envelope import OpenModel


class BestAvailableSelection(OpenModel):
    forcing_version_id: str
    valid_time: str
    variable: str
    selected_source: str
    source_cycle_time: str
    fallback_order: list[str]
    quality_flag: str


BestAvailableSelectionList = list[BestAvailableSelection]
