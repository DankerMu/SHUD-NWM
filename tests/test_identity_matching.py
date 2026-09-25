"""#2484 D3: the shared UTC cycle-hour comparison of the production-closure lanes."""

from __future__ import annotations

import pytest

from services.production_closure import two_node_e2e_evidence
from services.production_closure.identity_matching import (
    cycle_time_identity_matches,
    normalized_cycle_time_identity,
)


@pytest.mark.parametrize(
    "spelling",
    [
        "2026-09-24T12:00:00Z",
        "2026-09-24T12:00:00+00:00",
        "2026-09-24T20:00:00+08:00",
        "2026-09-24T12:00:00",
        "2026092412",
    ],
)
def test_every_spelling_of_one_cycle_names_the_same_utc_cycle_hour(spelling: str) -> None:
    assert normalized_cycle_time_identity(spelling) == "2026092412"
    assert cycle_time_identity_matches(spelling, "2026-09-24T12:00:00+00:00")
    assert cycle_time_identity_matches("2026-09-24T12:00:00Z", spelling)


def test_a_naive_iso_timestamp_is_read_as_utc() -> None:
    assert normalized_cycle_time_identity("2026-09-24T00:00:00") == "2026092400"
    assert not cycle_time_identity_matches("2026-09-24T00:00:00", "2026-09-24T00:00:00+08:00")


def test_neighbouring_cycle_hours_do_not_match() -> None:
    assert not cycle_time_identity_matches("2026-09-24T13:00:00Z", "2026-09-24T12:00:00+00:00")
    assert not cycle_time_identity_matches("2026092400", "2026092412")


@pytest.mark.parametrize(
    "garbage", ["latest", "", "2026-13-40T99:00:00Z", "2026-09-24 noon", "2026-09-24T12:00:00+25:00"]
)
def test_garbage_does_not_normalise_and_matches_only_itself(garbage: str) -> None:
    assert normalized_cycle_time_identity(garbage) is None
    assert cycle_time_identity_matches(garbage, garbage)
    assert not cycle_time_identity_matches(garbage, "2026-09-24T12:00:00Z")
    assert not cycle_time_identity_matches("2026-09-24T12:00:00Z", garbage)


def test_the_two_node_lane_keeps_its_private_names_bound_to_the_shared_helper() -> None:
    assert two_node_e2e_evidence._cycle_time_identity_matches is cycle_time_identity_matches
    assert two_node_e2e_evidence._normalized_cycle_time_identity is normalized_cycle_time_identity
