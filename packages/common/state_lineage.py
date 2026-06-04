"""Stable rejection codes for warm-start initial-state lineage and QC checks.

These codes are recorded in cycle/run evidence when a candidate state snapshot is
rejected during forecast initial-state selection. They are stable string constants;
do not rename existing values (downstream evidence and dashboards depend on them).
"""

from __future__ import annotations

# A candidate state was produced by a different source than the target cycle's
# source/cycle lineage (e.g. GFS state offered to an IFS cycle).
LINEAGE_SOURCE_MISMATCH = "LINEAGE_SOURCE_MISMATCH"

# A candidate state was produced by a model package version (or checksum) that does
# not match the model package the target cycle will run with.
LINEAGE_PACKAGE_VERSION_MISMATCH = "LINEAGE_PACKAGE_VERSION_MISMATCH"

# A candidate state was produced at a forecast lead beyond the configured max_lead
# policy for warm-start chaining.
LINEAGE_MAX_LEAD_EXCEEDED = "LINEAGE_MAX_LEAD_EXCEEDED"

# A candidate state failed SHUD state-variable QC (row counts / range / non-negative
# / water-balance checks) and is therefore unusable.
STATE_QC_FAILED = "STATE_QC_FAILED"


REJECTION_CODES = frozenset(
    {
        LINEAGE_SOURCE_MISMATCH,
        LINEAGE_PACKAGE_VERSION_MISMATCH,
        LINEAGE_MAX_LEAD_EXCEEDED,
        STATE_QC_FAILED,
    }
)
