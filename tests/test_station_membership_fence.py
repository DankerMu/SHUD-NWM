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

import hashlib
import textwrap

import pytest

from packages.common import display_coverage, forecast_store
from tests.station_membership_fence_oracle import (
    MEMBERSHIP_FENCE,
    PRE_2516_LEGACY_STATION_LEG,
    PRE_2516_NARROW_STATION_LEG,
    flat,
    membership_exists_body,
    pre_2516_leg,
    rendered_leg,
)

LEGS = {
    "forecast_store.latest_product_station_source": forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES,
    "display_coverage.station_sample_rows": display_coverage._STATION_SAMPLE_ROWS_TEMPLATES,
}

#: sha256 (UTF-8) of each station template AS MASTER ``64f47adee`` BUILT IT, at its
#: own indentation and after ``FORCING_TABLE_TOKEN`` interpolation. Recomputed from
#: master's bytes, not from this tree: load ``git show 64f47adee:packages/common/
#: forecast_store.py`` / ``.../display_coverage.py`` as modules and hash
#: ``_LATEST_PRODUCT_STATION_SOURCE_TEMPLATES.<store>`` (16-space indent) and
#: ``_STATION_SAMPLE_ROWS_TEMPLATES.<store>`` (12-space indent). The oracle keeps
#: ONE dedented copy per variant; re-indented, it must hash to all four.
MASTER_STATION_LEG_SHA256 = {
    ("forecast_store.latest_product_station_source", "legacy", 16): (
        "464273ab7d053dbbfe25ca1d662f0d710638dd757a5a573449afc78eca6a710e"
    ),
    ("forecast_store.latest_product_station_source", "narrow", 16): (
        "371a8156b1eb70d42b186f4d5756866e1fc0b51b4be170a86e423eb45242dc82"
    ),
    ("display_coverage.station_sample_rows", "legacy", 12): (
        "c31e5d05389ed077a27e5f5cfb36702dd5c0c55bd1638086cabd1d7d0fd928a7"
    ),
    ("display_coverage.station_sample_rows", "narrow", 12): (
        "a2ccf3041cdd5525548704d109e5afa65ab89e35d00bbdab8856ffc1d4336972"
    ),
}
ORACLE_LEGS = {"legacy": PRE_2516_LEGACY_STATION_LEG, "narrow": PRE_2516_NARROW_STATION_LEG}


@pytest.mark.parametrize(("key", "store", "indent"), tuple(MASTER_STATION_LEG_SHA256))
def test_the_frozen_oracle_is_byte_for_byte_each_master_template(key, store, indent):
    """An edit to the frozen master text (whitespace included) is red here."""
    reindented = textwrap.indent(ORACLE_LEGS[store], " " * indent)
    digest = hashlib.sha256(reindented.encode()).hexdigest()
    assert digest == MASTER_STATION_LEG_SHA256[(key, store, indent)]


def test_the_frozen_narrow_oracle_cannot_collapse_into_the_fenced_leg():
    assert MEMBERSHIP_FENCE not in PRE_2516_NARROW_STATION_LEG
    for pair in LEGS.values():
        assert flat(PRE_2516_NARROW_STATION_LEG) != flat(pair.narrow)


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
