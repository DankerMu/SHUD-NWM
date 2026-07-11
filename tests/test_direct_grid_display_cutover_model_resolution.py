"""``model_id`` propagation audit — no display surface pins across a cutover.

Change: ``direct-grid-display-cutover`` — Epic #992 SUB-6 (§3.1 /
``active-model-dynamic-resolution`` ADDED requirement "No display surface
pins or caches ``model_id`` across a cutover").

This suite closes the ``model_id`` propagation paths identified by the
SUB-6 audit and locks each with a regression test. There is NO
server-side active-model resolution on the station-series path by design
— ``model_id`` is a required client-supplied filter — and this suite
locks that property rather than inventing a server-side resolver.

Path 1 — station-series endpoint required-filter contract
    The ``/api/v1/met/stations/{station_id}/series`` Query default for
    ``model_id`` is literal ``None``; the object-store read layer raises
    ``MISSING_REQUIRED_FILTER`` when the client omits it. No server-side
    "active-model" fallback lives on this route.

Path 2 — MVT tile cache key derivation
    ``apps/api/routes/hydro_display.py::_station_source_version`` derives
    the tile cache key exclusively from the ``active_flag=true`` station
    source identity — the SQL literal references no ``model_id`` column
    and the key self-invalidates when the SUB-1 flip flips
    ``active_flag``. The runtime lock is parametrized across the SQLite
    dev branch and the PostGIS prod branch, and across a row-filtered
    swap (Case A) and a same-count identity swap (Case B) — the latter
    catches constant-digest evasions that would still pass Case A.

Path 3 — explicit historical ``(cycle, model_id)`` route
    Covered by SUB-5's
    ``tests/test_direct_grid_display_cutover_history.py::
    test_active_station_with_pre_cutover_file_serves_series`` and
    ``::test_inactive_m0_legacy_station_with_pre_cutover_file_serves_series``
    (both lock the "explicit legal historical ``model_id``" answerability
    scenario). Not duplicated here.

Path 4 / Path 5 — frontend live ``model_id`` resolution and no-pin
    Frontend claims are closed by the extended
    ``M11StationForcingPopup.test.tsx`` suite (``product.model_id`` at
    request time; no reuse across two live requests when the latest
    product changes) plus the ``bootstrap.test.ts`` suite (identity
    cache invalidator + bounded TTL constant lock). Backend guardrail
    here: the object-store read path and the ``PsycopgStationLookup``
    MUST NOT reference any "active-model resolver" identifier — the
    frontend is the sole owner of live ``model_id`` sourcing.

Path 6 — frontend ``latestProductIdentityCache`` bounded closure
    ``apps/frontend/src/pages/hydroMet/bootstrap.ts`` runs a 120s
    TTL identity cache keyed on ``(basinId, source, cycle)``. Locked in
    the frontend suite (TTL constant byte-shape lock + explicit
    invalidator end-to-end proof + positive in-TTL coalescing baseline).

Path 7 — ``/api/v1/mvp/qhh/latest-product`` route
    ``apps/api/routes/forecast.py::get_qhh_latest_product`` takes
    ``model_id`` as an optional Query for the strict-identity handoff.
    Byte-shape locked below with the shared broadened regex — no
    server-side active-model resolver may sneak onto this route.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes import data_sources as data_sources_module
from apps.api.routes import forecast as forecast_module
from apps.api.routes.data_sources import get_met_station_series
from apps.api.routes.forecast import get_qhh_latest_product
from apps.api.routes.hydro_display import _station_source_version
from packages.common.forecast_store import ForecastStoreError
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationMetadata,
    raise_station_not_found,
    read_station_forcing_csv,
)

# --- shared regression-lock helpers ---------------------------------------

# Broadened banned-identifier surface. The Phase 4 test-coverage review
# flagged the earlier five-name literal list as too narrow: plausible
# mutations like ``active_model_default``, ``fallback_active_model_id``,
# ``pick_active_model``, ``pin_current_model``, ``derive_active_model_for_basin``,
# and ``current_model_for_basin`` all evaded the tuple check. The regex
# closes those escape hatches by boundary-anchoring on
# ``(?<![A-Za-z0-9])`` (NOT ``\b`` — ``\b`` treats ``_`` as a word char
# and so would MISS ``resolve_active_model`` / ``get_active_model`` /
# ``fallback_active_model_id`` where the dangerous prefix sits after an
# underscore-separated stem) and matching any
# ``<qualifier>[_-]?(?:active[_-]?|current[_-]?|latest[_-]?)?model...``
# identifier where ``<qualifier>`` is one of the semantically dangerous
# prefixes.
#
# Verified against the SUT surface at authoring time — the regex matches
# NO existing legitimate identifier in ``apps/api/routes/data_sources.py``,
# ``apps/api/routes/forecast.py``, ``apps/api/routes/hydro_display.py``,
# ``packages/common/object_store_forcing.py::read_station_forcing_csv``,
# or ``PsycopgStationLookup._lookup_with_cursor`` (``model_id``,
# ``model_instance``, ``PsycopgStationLookup``, ``read_station_forcing_csv``,
# and ``station_source_version`` are all unaffected). The single incidental
# match in ``object_store_forcing.py`` is the string ``active-model`` in a
# spec-name comment — handled by ``_strip_comments_and_docstrings`` below.
# Positive-lock and negative-lock parametrized tests below bind the
# docstring-claimed mutation list to the regex so any future weakening
# fails loudly.
BANNED_ACTIVE_MODEL_RESOLVER_REGEX = re.compile(
    r"(?<![A-Za-z0-9])(?:active|current|default|fallback|latest|pick|pin|derive|resolve|get|fetch|infer|choose)[_-]?(?:active[_-]?|current[_-]?|latest[_-]?)?model[A-Za-z0-9_-]*",
    re.IGNORECASE,
)


def _strip_comments_and_docstrings(source: str) -> str:
    """Strip ``#`` comments and triple-double-quoted docstrings from source.

    Test-coverage review pointed out that a naive ``re.search`` on raw
    Python source will trip on tokens that appear only inside comments
    or docstrings (e.g. ``# does not filter by model_id``, or the
    incidental ``active-model`` string in a spec-name comment in
    ``object_store_forcing.py``). Stripping both surfaces gives an
    honest byte-shape check that reflects executable identifiers only.
    """
    stripped = re.sub(r'"""(?:.|\n)*?"""', "", source)
    stripped = re.sub(r"#.*", "", stripped)
    return stripped


# --- fixtures shared across paths -----------------------------------------

HISTORICAL_STATION_ID = "m0_legacy_forc_042"
HISTORICAL_BASIN_VERSION_ID = "basins_heihe_vbasins"
HISTORICAL_SOURCE_ID = "ifs"
HISTORICAL_CYCLE_TIME = datetime(2026, 5, 15, 0, tzinfo=UTC)
HISTORICAL_FORCING_FILENAME = "X100.75Y37.65.csv"


class _FakeStationLookup:
    """In-memory ``StationLookup`` returning a single fixed row.

    Mirrors the SUB-4 / SUB-5 exemplar seams — no DB, no ``active_flag``
    filtering. The read path resolves the row and then resolves the disk
    asset by (source, cycle, basin_version_id, model_id, filename).
    """

    def __init__(self, station: StationMetadata) -> None:
        self._station = station

    def lookup(self, station_id: str) -> StationMetadata:
        if station_id != self._station.station_id:
            raise_station_not_found(station_id)
        return self._station


def _historical_station(*, active_flag: bool | None) -> StationMetadata:
    return StationMetadata(
        station_id=HISTORICAL_STATION_ID,
        basin_version_id=HISTORICAL_BASIN_VERSION_ID,
        station_name="HEIHE M0 legacy forcing station 042",
        longitude=100.75,
        latitude=37.65,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=active_flag,
        properties_json={"forcing_filename": HISTORICAL_FORCING_FILENAME},
    )


# =========================================================================
# Path 1 — station-series endpoint required-filter contract
# =========================================================================


def test_station_series_missing_model_id_returns_missing_required_filter_via_object_store_layer(
    tmp_path: Path,
) -> None:
    """(P1) ``read_station_forcing_csv`` raises ``MISSING_REQUIRED_FILTER`` on absent ``model_id``.

    The route's Query default is ``None`` (locked by the sibling test
    below); the object-store read layer is what raises the 422 error.
    Directly exercising ``read_station_forcing_csv`` here locks the leaf
    behavior independent of FastAPI dependency wiring.
    """
    station = _historical_station(active_flag=True)

    with pytest.raises(ForecastStoreError) as excinfo:
        read_station_forcing_csv(
            station_lookup=_FakeStationLookup(station),
            object_store_root=tmp_path,
            station_id=HISTORICAL_STATION_ID,
            model_id=None,  # type: ignore[arg-type]
            source_id=HISTORICAL_SOURCE_ID,
            cycle_time=HISTORICAL_CYCLE_TIME,
        )

    error = excinfo.value
    assert error.status_code == 422
    assert error.code == "MISSING_REQUIRED_FILTER"
    assert error.details == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }


def test_data_sources_route_has_no_server_side_active_model_default_for_model_id() -> None:
    """(P1) The station-series route's ``model_id`` Query default is literal ``None``.

    Byte-shape lock on ``apps/api/routes/data_sources.py`` — the route
    body MUST NOT resolve ``model_id`` from an "active-model" lookup and
    MUST NOT set a server-side default. Any future refactor that
    introduces ``resolve_active_model`` / ``active_model_for_basin`` /
    ``latest_active_model`` / ``active_model_default`` /
    ``fallback_active_model_id`` / ``pick_active_model`` /
    ``derive_active_model_for_basin`` / ``current_model_for_basin`` on
    this route would silently pin the display to a captured
    ``model_id`` across a cutover and break the SUB-6 invariant. The
    regex (defined once at module scope) tokenizes on ``\\b`` so any
    ``<qualifier>[_-]?model...`` identifier trips it.
    """
    source = _strip_comments_and_docstrings(inspect.getsource(get_met_station_series))

    # Query default lock — the exact ``model_id: str | None = Query(default=None``
    # signature the route currently carries.
    assert re.search(
        r"model_id:\s*str\s*\|\s*None\s*=\s*Query\(\s*default=None",
        source,
    ) is not None, (
        "expected 'model_id: str | None = Query(default=None, ...)' on "
        "the station-series route — a server-side default would pin "
        "model_id across a cutover"
    )

    # Negative predicate: no "active-model" resolver identifier appears
    # anywhere in the route body. The broadened regex covers the five
    # original literal names plus every plausible mutation surfaced by
    # the Phase 4 test-coverage review (see BANNED_ACTIVE_MODEL_RESOLVER_REGEX
    # docstring above).
    banned_match = BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(source)
    assert banned_match is None, (
        f"forbidden active-model resolver identifier in "
        f"get_met_station_series: {banned_match.group(0)!r} — the route "
        "must keep model_id a required client-supplied filter"
    )

    # Sibling lock: the module-level identifier surface also does not
    # import an "active-model" resolver. A local ``from ... import
    # resolve_active_model`` inside a nested helper would sneak past the
    # function-source check; the module-source check closes that hole.
    module_source = _strip_comments_and_docstrings(inspect.getsource(data_sources_module))
    banned_module_match = BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(module_source)
    assert banned_module_match is None, (
        f"forbidden active-model resolver identifier imported into "
        f"apps/api/routes/data_sources.py: {banned_module_match.group(0)!r}"
    )


def test_forecast_latest_product_route_has_no_server_side_active_model_default_for_model_id() -> None:
    """(P7) ``get_qhh_latest_product`` keeps ``model_id`` client-supplied.

    ``apps/api/routes/forecast.py::get_qhh_latest_product`` accepts
    ``model_id: str | None = Query(default=None, ...)`` as the strict
    identity handoff parameter. Same byte-shape contract as P1: the
    route MUST NOT introduce any "active-model" resolver that silently
    swaps a client-supplied ``model_id`` for a server-side "current"
    value — that would pin the ``/mvp/qhh/latest-product`` display to a
    captured ``model_id`` across a cutover and break the SUB-6
    invariant on the first-class model_id-carrying display surface.
    """
    source = _strip_comments_and_docstrings(inspect.getsource(get_qhh_latest_product))

    # Query default lock — the strict-identity ``model_id`` parameter
    # carries a literal ``None`` default and no server-side fallback.
    assert re.search(
        r"model_id:\s*str\s*\|\s*None\s*=\s*Query\(\s*\n?\s*default=None",
        source,
    ) is not None, (
        "expected 'model_id: str | None = Query(default=None, ...)' on "
        "get_qhh_latest_product — a server-side default would pin the "
        "latest-product display to a captured model_id across a cutover"
    )

    banned_match = BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(source)
    assert banned_match is None, (
        f"forbidden active-model resolver identifier in "
        f"get_qhh_latest_product: {banned_match.group(0)!r} — the "
        "latest-product route must keep model_id caller-supplied"
    )

    # Sibling module lock, same rationale as the P1 module-source check.
    module_source = _strip_comments_and_docstrings(inspect.getsource(forecast_module))
    banned_module_match = BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(module_source)
    assert banned_module_match is None, (
        f"forbidden active-model resolver identifier imported into "
        f"apps/api/routes/forecast.py: {banned_module_match.group(0)!r}"
    )


# =========================================================================
# Path 2 — MVT tile cache key derivation
# =========================================================================


def test_station_source_version_sql_does_not_reference_model_id() -> None:
    """(P2) ``_station_source_version`` SQL literal references no ``model_id`` column.

    The tile cache key is derived from station inventory identity alone
    (``station_id, basin_version_id, station_name, station_role,
    active_flag, geom, created_at``). Adding a ``model_id`` predicate or
    projected column would pin the tile key to a captured ``model_id``
    and break the flip's self-invalidation contract (SUB-1 test 7 covers
    the sibling ``basin_version_id + active_flag=true`` invariant; this
    test adds the "no ``model_id`` reference" positive claim).

    Case-insensitive matching guards against an uppercase-mutated
    ``MODEL_ID`` column reference (PostgreSQL folds unquoted identifiers
    to lowercase at parse time, so an uppercase form would compile but
    would evade a case-sensitive substring check).
    """
    raw_source = inspect.getsource(_station_source_version)

    # Positive anchors run against the RAW source so a rename hiding in
    # unusual formatting cannot slip past.
    assert "met.met_station" in raw_source, (
        "expected _station_source_version to query met.met_station "
        "(SUT anchor moved?)"
    )
    assert "basin_version_id = :basin_version_id" in raw_source, (
        "expected _station_source_version to key on basin_version_id "
        "(SUT anchor moved?)"
    )

    # Negative predicate on executable source only — strip docstrings +
    # ``#`` comments so a defensive comment like
    # ``# tile cache key omits model_id on purpose`` cannot trip the
    # check (Phase 4 test-coverage review Fold 6).
    executable_source = _strip_comments_and_docstrings(raw_source)
    assert re.search(r"model_id", executable_source, re.IGNORECASE) is None, (
        "_station_source_version must not reference model_id — the tile "
        "cache key is derived from station inventory identity alone "
        "(SUB-6 §3.1: 'MVT tile cache key derived from active_flag=true "
        "station source identity so it self-invalidates on flip')"
    )


class _FakeSessionResult:
    """Minimal ``session.execute(...)`` result stub returning fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeSessionResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    """Minimal ``session.get_bind()`` returning a dialect stub.

    Parametrized so the same fake can drive both the SQLite dev branch
    (``dialect.name == "sqlite"``) and the PostGIS prod branch of
    ``_station_source_version``. The SQL text differs between branches
    but the fake ``execute`` returns the constructor-supplied rows
    verbatim, so the test author is responsible for supplying rows that
    already reflect the branch's WHERE-clause filtering.
    """

    def __init__(self, dialect_name: str = "sqlite") -> None:
        self.dialect = _FakeDialect(dialect_name)


class _FakeSession:
    """In-memory ``sqlalchemy.orm.Session`` seam for ``_station_source_version``.

    The SUT calls ``session.get_bind().dialect.name`` (routes SQLite vs
    PostGIS) and ``session.execute(text(...), params).mappings().all()``.
    We satisfy both surfaces and return the constructor-supplied rows.
    """

    def __init__(self, rows: list[dict[str, Any]], dialect_name: str = "sqlite") -> None:
        self._rows = rows
        self._dialect_name = dialect_name

    def get_bind(self) -> _FakeBind:
        return _FakeBind(self._dialect_name)

    def execute(self, _stmt: Any, _params: Any) -> _FakeSessionResult:
        return _FakeSessionResult(self._rows)


def _station_row(
    *,
    station_id: str,
    active_flag: int,
    basin_version_id: str = HISTORICAL_BASIN_VERSION_ID,
) -> dict[str, Any]:
    return {
        "station_id": station_id,
        "basin_version_id": basin_version_id,
        "station_name": f"{station_id} name",
        "station_role": "forcing_grid",
        "active_flag": active_flag,
        "geom": "0101000020e6100000",  # opaque hex placeholder; identity-relevant only
        "created_at": datetime(2026, 5, 1, 0, tzinfo=UTC),
    }


@pytest.mark.parametrize("dialect_name", ["sqlite", "postgresql"])
@pytest.mark.parametrize(
    ("scenario", "pre_rows_factory", "post_rows_factory", "expected_pre_len", "expected_post_len"),
    [
        pytest.param(
            "row_filtered_flip",
            lambda: [
                _station_row(station_id="alpha", active_flag=1),
                _station_row(station_id="beta", active_flag=1),
            ],
            # Post-flip: "beta" was flipped to inactive; the SUT WHERE
            # clause filters ``active_flag = 1`` (SQLite) / ``= true``
            # (PostGIS), so the fake ``execute`` MUST already reflect
            # the WHERE-filtered rows — post-set is just [alpha].
            lambda: [
                _station_row(station_id="alpha", active_flag=1),
            ],
            2,
            1,
            id="case_a_row_filtered",
        ),
        pytest.param(
            "same_count_identity_swap",
            lambda: [
                _station_row(station_id="alpha", active_flag=1),
                _station_row(station_id="beta", active_flag=1),
            ],
            # Same row count, different station identity — the real
            # production cutover swap where a beta station is deactivated
            # AND a gamma station is activated in the same flip. A
            # constant-digest mutation (e.g. hashing only
            # ``basin_version_id``) would still pass Case A because the
            # ``:<len>`` suffix would differ, but MUST NOT pass Case B:
            # only the digest carries the row identity change here.
            lambda: [
                _station_row(station_id="alpha", active_flag=1),
                _station_row(station_id="gamma", active_flag=1),
            ],
            2,
            2,
            id="case_b_same_count_swap",
        ),
    ],
)
def test_station_source_version_key_self_invalidates_when_active_flag_flips(
    dialect_name: str,
    scenario: str,
    pre_rows_factory: Any,
    post_rows_factory: Any,
    expected_pre_len: int,
    expected_post_len: int,
) -> None:
    """(P2) Tile cache key changes when the station-source inventory flips.

    Parametrized runtime lock covering:

    * Case A — a ``beta`` station is deactivated; the WHERE clause drops
      it from the projected row set. The row-count suffix differs on its
      own, but so should the digest.
    * Case B — a ``beta`` deactivation is paired with a ``gamma``
      activation. The projected row count is unchanged; only the digest
      encodes the identity change. This case catches constant-digest
      evasions (e.g. a mutation to
      ``hashlib.sha256(basin_version_id.encode()).hexdigest()[:16]``
      that would still pass Case A).

    Each case runs against BOTH the SQLite dev branch and the PostGIS
    prod branch, closing the branch-coverage gap flagged in the Phase 4
    test-coverage review.
    """
    pre_key = _station_source_version(
        _FakeSession(pre_rows_factory(), dialect_name=dialect_name),  # type: ignore[arg-type]
        HISTORICAL_BASIN_VERSION_ID,
    )
    post_key = _station_source_version(
        _FakeSession(post_rows_factory(), dialect_name=dialect_name),  # type: ignore[arg-type]
        HISTORICAL_BASIN_VERSION_ID,
    )

    assert pre_key != post_key, (
        f"[{dialect_name}/{scenario}] expected tile cache key to change "
        "when the station-source inventory flips (self-invalidates on "
        "flip); a stable key would serve stale tiles across a cutover"
    )

    # Key format lock — ``met-stations:<digest>:<basin_version_id>:<len>``.
    pre_parts = pre_key.split(":")
    post_parts = post_key.split(":")
    assert pre_parts[0] == "met-stations" == post_parts[0]
    assert pre_parts[2] == HISTORICAL_BASIN_VERSION_ID == post_parts[2]
    assert int(pre_parts[3]) == expected_pre_len
    assert int(post_parts[3]) == expected_post_len

    # Digest-segment lock — the substantive claim is that the digest
    # itself carries the inventory identity, not that the length suffix
    # happens to change. Even in Case B where the length is stable, the
    # digest segment MUST differ; this catches a mutation that only
    # varies the ``:<len>`` suffix.
    assert pre_parts[1] != post_parts[1], (
        f"[{dialect_name}/{scenario}] expected digest segment to differ; "
        f"pre={pre_parts[1]} post={post_parts[1]}. A constant-digest "
        "mutation would still pass Case A on row-count change but "
        "silently serve stale tiles across a same-count identity swap."
    )


# =========================================================================
# Path 3 — explicit historical ``(cycle, model_id)`` route
# =========================================================================
#
# Deliberately not covered in this file. SUB-5 already locks the
# "explicit legal historical ``model_id`` resolves the immutable
# pre-cutover asset" contract end-to-end via
# ``tests/test_direct_grid_display_cutover_history.py``:
#   * ``test_active_station_with_pre_cutover_file_serves_series``
#   * ``test_inactive_m0_legacy_station_with_pre_cutover_file_serves_series``
# Re-asserting it here would duplicate the SUB-5 fixture/contract without
# adding new regression surface.


# =========================================================================
# Path 4 / Path 5 — backend guardrail (frontend claims live in
# apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx
# and apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts)
# =========================================================================


def test_object_store_forcing_never_calls_active_model_resolver() -> None:
    """(P4/P5 backend guardrail) The read path never resolves an "active model".

    The frontend is the sole owner of live ``model_id`` sourcing (via
    ``fetchHydroMetLatestProduct`` at request time — locked by the
    extended M11StationForcingPopup suite plus the bootstrap.test.ts
    identity-cache locks). The backend read path MUST keep ``model_id``
    a caller-supplied argument and MUST NOT introduce an "active-model
    resolver" that would silently pin a captured value across a cutover.

    Tokenized boundary: the banned-identifier scope is now regex-based
    (see ``BANNED_ACTIVE_MODEL_RESOLVER_REGEX`` at module scope), so
    plausible mutations that a five-name literal tuple would miss
    (``active_model_default``, ``fallback_active_model_id``,
    ``pick_active_model``, ``pin_current_model``,
    ``derive_active_model_for_basin``, ``current_model_for_basin``, …)
    all trip the lock. Comments and docstrings are stripped before the
    check so a spec-name reference in a comment (e.g. the
    ``active-model-dynamic-resolution`` change-name mentioned elsewhere
    in the module) cannot cause a false positive.
    """
    for target in (read_station_forcing_csv, PsycopgStationLookup._lookup_with_cursor):
        source = _strip_comments_and_docstrings(inspect.getsource(target))
        banned_match = BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(source)
        assert banned_match is None, (
            f"forbidden active-model resolver identifier in "
            f"{target.__qualname__}: {banned_match.group(0)!r} — the "
            "backend read path must keep model_id caller-supplied"
        )


# =========================================================================
# Regex-lock — bind the docstring claim to the executable check
# =========================================================================


@pytest.mark.parametrize(
    "identifier",
    [
        # Pre-fold literal tuple (must remain caught)
        "resolve_active_model",
        "active_model_for_basin",
        "latest_active_model",
        "resolve_current_model",
        "get_active_model",
        # Docstring-claimed mutations (broadened regex must catch these too)
        "active_model_default",
        "fallback_active_model_id",
        "pick_active_model",
        "pin_current_model",
        "derive_active_model_for_basin",
        "current_model_for_basin",
    ],
)
def test_banned_active_model_resolver_regex_matches_known_mutation_names(identifier: str) -> None:
    """Positive-lock: docstring-claimed mutations MUST match the broadened regex.

    Reviewer P1 (Phase 6.5) found the pre-fix regex used ``\\b`` boundaries
    which treat ``_`` as a word char — so ``active_model_default`` etc. evaded.
    This parametrized positive test binds the docstring claim to the code:
    every identifier in this list MUST be caught by the regex, or the fold
    contract is broken.
    """
    fabricated_source = f"def route(): x = {identifier}('basin_x'); return x\n"
    assert BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(fabricated_source) is not None, (
        f"BANNED_ACTIVE_MODEL_RESOLVER_REGEX failed to catch known-name mutation {identifier!r}"
    )


@pytest.mark.parametrize(
    "identifier",
    [
        "model_id",
        "model_instance",
        "PsycopgStationLookup",
        "read_station_forcing_csv",
        "station_source_version",
        "model_registry",
        "grid_snapshot_id",
    ],
)
def test_banned_active_model_resolver_regex_does_not_match_legitimate_identifiers(identifier: str) -> None:
    """Negative-lock: legitimate SUT identifiers MUST NOT trip the regex.

    Verifies the ``(?<![A-Za-z0-9])`` boundary does not create false positives
    on symbols that legitimately appear in production SUT sources.
    """
    fabricated_source = f"def route(): x = {identifier}('basin_x'); return x\n"
    assert BANNED_ACTIVE_MODEL_RESOLVER_REGEX.search(fabricated_source) is None, (
        f"BANNED_ACTIVE_MODEL_RESOLVER_REGEX unexpectedly matched legitimate identifier {identifier!r}"
    )
