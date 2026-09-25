"""#2516 D3: both narrow station legs fence the membership EXISTS with ``OFFSET 0``.

Offline text pins of the revised D3 shape (design.md D3). Candidate D (a
de-duplicated ``req`` relation) did not keep ``variable`` in
``interp_weight_qhh_latest_membership_idx``'s Index Cond on node-27: the planner
still pulled the EXISTS up into a semi-join. The fence stops the pull-up, so the
probe runs as a correlated SubPlan whose outer values -- ``fst.variable_e::text``
included -- are index-scan parameters. The narrow variants of the QHH
latest-product station leg and of the display-coverage station leg must be
master's narrow text plus exactly that one clause, placed last inside the
membership EXISTS; the legacy variants must be master's legacy text, unchanged.

The frozen master text is ``tests/station_membership_fence_oracle.py``. Whether
the rows are the same, and what the seeded planner does, is the real-database
question in ``test_station_membership_fence_integration.py``.
"""

from __future__ import annotations

import pytest

from packages.common import display_coverage, forecast_store
from tests.station_membership_fence_oracle import (
    MEMBERSHIP_FENCE,
    flat,
    membership_exists_body,
    pre_2516_leg,
    rendered_leg,
)

LEGS = {
    "forecast_store.latest_product_station_source": forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES,
    "display_coverage.station_sample_rows": display_coverage._STATION_SAMPLE_ROWS_TEMPLATES,
}


@pytest.mark.parametrize("key", tuple(LEGS))
def test_the_narrow_membership_exists_ends_with_the_fence(key):
    body = membership_exists_body(rendered_leg(LEGS[key], "narrow"))
    assert body == membership_exists_body(pre_2516_leg("narrow")) + f" {MEMBERSHIP_FENCE}"
    # The comparison stays text with text through the cast, as on master; the
    # fence is what makes it an index-scan parameter.
    assert "iw.variable = fst.variable_e::text" in body


@pytest.mark.parametrize("key", tuple(LEGS))
def test_the_narrow_leg_is_master_plus_only_the_fence(key):
    """Nothing but the one clause moved: parameter names, joins, casts, conjuncts."""
    new = flat(rendered_leg(LEGS[key], "narrow"))
    old = flat(pre_2516_leg("narrow"))
    assert new.count(MEMBERSHIP_FENCE) == 1
    fenced_tail = f"LOWER(iw.source_id) = LOWER(cr.source_id) {MEMBERSHIP_FENCE} )"
    assert new.endswith(fenced_tail)
    assert new == old.removesuffix("LOWER(iw.source_id) = LOWER(cr.source_id) )") + fenced_tail
    assert "fst.variable_e = ANY(%(variables)s::met.forcing_variable[])" in new


@pytest.mark.parametrize("key", tuple(LEGS))
def test_the_legacy_leg_is_master_unchanged(key):
    legacy = rendered_leg(LEGS[key], "legacy")
    assert flat(legacy) == flat(pre_2516_leg("legacy"))
    assert MEMBERSHIP_FENCE not in legacy


def test_the_two_narrow_legs_are_the_same_statement_modulo_indentation():
    """design.md D3 fences the display-coverage copy identically."""
    rendered = {key: flat(rendered_leg(pair, "narrow")) for key, pair in LEGS.items()}
    assert len(set(rendered.values())) == 1
