"""Registry model identity fields and the identity comparison helpers.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Registry rows compared byte-for-byte on these top-level fields to decide
# "unchanged" vs "package_changed".  Deviations in ANY of these fields escalate.
#
# The whitelist is the deliberate union of the identity fields the spec names
# (see openspec design D7): the three URI/checksum fields plus every documented
# identity field emitted by scheduler_registry_row_from_sources.  It stays a
# tuple so the classifier iterates in declaration order and the identical
# tuple also drives the regression test that guards the whitelist itself.
REGISTRY_MODEL_IDENTITY_FIELDS = (
    "model_package_uri",
    "manifest_uri",
    "package_checksum",
    "basin_version_id",
    "river_network_version_id",
    "shud_code_version",
    "segment_count",
    "output_segment_count",
    "lifecycle_state",
)

# Nested identity fields; classified by (top_level_field, nested_path) pairs.
# Every path is a tuple of successive Mapping keys.  A missing top-level or
# intermediate mapping in either row counts as inequality (escalates to
# package_changed) so drift in a rebuilt resource profile cannot ride through
# silently.
REGISTRY_MODEL_NESTED_IDENTITY_FIELDS = (
    ("resource_profile", ("source_inventory_checksum",)),
)

# Runtime enforcement of the model_id regex must match the schema
# (see schemas/scheduler_file_provider_refresh_receipt.schema.json:204 and
# schemas/scheduler_registry_package_cutover.schema.json:16) so
# `_validate_receipt` and `jsonschema.Draft202012Validator` reject the same
# corpus.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

MAX_MODEL_ID_LENGTH = 128

# The prospective registry generation as recorded on a classification receipt
# (#1433 round-1 F-A).  Same corpus as the declaration schema's `generation`
# field (schemas/scheduler_registry_package_cutover.schema.json:16) so an
# operator can copy the receipt value straight into a declaration.
GENERATION_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

MAX_GENERATION_LENGTH = 128

_MISSING_IDENTITY = object()

def _extract_nested_identity(row: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Traverse ``row`` by ``path`` returning ``_MISSING_IDENTITY`` on any gap.

    Missing top-level or intermediate mapping is materially different from a
    JSON ``null`` value; conflating the two would let a rebuilt profile drop
    its ``source_inventory_checksum`` silently and stay ``unchanged``.
    """
    current: Any = row
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING_IDENTITY
        current = current[key]
    return current

def _rows_have_identical_identity(
    row: Mapping[str, Any], previous_row: Mapping[str, Any]
) -> bool:
    """Return True when ``row`` and ``previous_row`` match on every identity field.

    Compares the flat ``REGISTRY_MODEL_IDENTITY_FIELDS`` plus every nested
    ``(top_level, nested_path)`` pair in ``REGISTRY_MODEL_NESTED_IDENTITY_FIELDS``.
    Any deviation escalates the caller to ``package_changed``.
    """
    for field_name in REGISTRY_MODEL_IDENTITY_FIELDS:
        if row.get(field_name) != previous_row.get(field_name):
            return False
    for top_level, nested_path in REGISTRY_MODEL_NESTED_IDENTITY_FIELDS:
        if _extract_nested_identity(row, (top_level, *nested_path)) != (
            _extract_nested_identity(previous_row, (top_level, *nested_path))
        ):
            return False
    return True
