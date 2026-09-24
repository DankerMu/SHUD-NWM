"""Evidence-only basins stay out of public discovery (#1729) -- fake-cursor seams.

``core.basin`` has no ``active_flag``, so the synthetic evidence fixture
``basin__evidence_cmfd_p02_synth`` (``basin_group = 'evidence-only'``) was
returned by ``GET /api/v1/basins`` and ``/basins/{id}/versions``. These tests
pin the SQL shape of the exclusion (clause present, before ``LIMIT/OFFSET``,
parameter order) and the 404 mapping; NULL retention and page arithmetic run
against a real database in ``tests/test_model_registry_evidence_only_integration.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.models import get_model_registry_store
from packages.common.forecast_store import QHH_LATEST_READY_RUN_STATUSES
from packages.common.model_registry import (
    EVIDENCE_ONLY_BASIN_GROUP,
    MissingResourceError,
    PsycopgModelRegistryStore,
)
from packages.common.model_registry_contracts import EVIDENCE_ONLY_BASIN_GROUP as CONTRACT_EVIDENCE_ONLY

EVIDENCE_BASIN_ID = "basin__evidence_cmfd_p02_synth"
# basin_id -> basin_group, as core.basin holds them.
_BASINS: dict[str, str | None] = {
    "basins_qhh": "Basins",
    "basins_ungrouped": None,
    EVIDENCE_BASIN_ID: "evidence-only",
}


def _normalized(statement: str) -> str:
    return " ".join(statement.split())


class _Cursor:
    """Answers the registry's basin probes from ``_BASINS``; records every statement."""

    def __init__(self, captured: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._captured = captured
        self._one: Any = None
        self._all: list[Any] = []

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        sql = _normalized(statement)
        self._captured.append((sql, tuple(parameters)))
        if sql == "SELECT 1 FROM core.basin WHERE basin_id = %s AND basin_group IS DISTINCT FROM %s":
            basin_id, excluded = parameters
            present = basin_id in _BASINS and _BASINS[basin_id] != excluded
            self._one = {"?column?": 1} if present else None
        elif sql == "SELECT 1 FROM core.basin WHERE basin_id = %s":
            # The group-blind ``_exists`` probe: an evidence row IS present to it.
            self._one = {"?column?": 1} if parameters[0] in _BASINS else None
        elif sql == "SELECT basin_group FROM core.basin WHERE basin_id = %s":
            (basin_id,) = parameters
            self._one = {"basin_group": _BASINS[basin_id]} if basin_id in _BASINS else None
        elif "FROM core.model_instance mi" in sql and "WHERE mi.model_id = %s" in sql:
            (model_id,) = parameters
            basin_id = model_id.removesuffix("_model")
            self._one = {
                "model_id": model_id,
                "basin_version_id": f"{basin_id}_v1",
                "basin_id": basin_id,
                "basin_name": basin_id,
                "active_flag": False,
                "resource_profile": {},
                "segment_count": 0,
                "mesh_properties_json": {},
            }
        elif sql.startswith("SELECT COUNT(*) AS total"):
            self._one = {"total": 0}
        else:
            self._one = None
            self._all = []

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._all


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> tuple[PsycopgModelRegistryStore, list[tuple[str, tuple[Any, ...]]]]:
    captured: list[tuple[str, tuple[Any, ...]]] = []

    class _Transaction:
        def __enter__(self) -> _Cursor:
            return _Cursor(captured)

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: _Transaction())
    return PsycopgModelRegistryStore("postgresql://example"), captured


def test_the_constant_is_one_value_reexported_by_the_facade() -> None:
    assert EVIDENCE_ONLY_BASIN_GROUP == "evidence-only"
    assert EVIDENCE_ONLY_BASIN_GROUP is CONTRACT_EVIDENCE_ONLY


@pytest.mark.parametrize("has_display_product", [False, True])
def test_list_basins_excludes_evidence_only_before_pagination(store: Any, has_display_product: bool) -> None:
    registry, captured = store

    registry.list_basins(limit=7, offset=14, has_display_product=has_display_product)

    ((sql, params),) = captured
    exclusion = sql.index("WHERE basin_group IS DISTINCT FROM %s")
    assert exclusion < sql.index("ORDER BY basin_name, basin_id") < sql.index("LIMIT %s OFFSET %s")
    if has_display_product:
        assert exclusion < sql.index("AND EXISTS (")
        assert params == (EVIDENCE_ONLY_BASIN_GROUP, list(QHH_LATEST_READY_RUN_STATUSES), 7, 14)
    else:
        assert "EXISTS" not in sql
        assert params == (EVIDENCE_ONLY_BASIN_GROUP, 7, 14)


def test_list_basin_versions_treats_an_evidence_only_basin_as_missing(store: Any) -> None:
    registry, captured = store

    with pytest.raises(MissingResourceError, match=f"basin_id not found: {EVIDENCE_BASIN_ID}"):
        registry.list_basin_versions(basin_id=EVIDENCE_BASIN_ID, limit=10, offset=0)

    # The versions (and their geometry) are never even queried.
    assert [sql for sql, _ in captured if "FROM core.basin_version" in sql] == []


@pytest.mark.parametrize("basin_id", ["basins_qhh", "basins_ungrouped"])
def test_list_basin_versions_keeps_grouped_and_null_group_basins(store: Any, basin_id: str) -> None:
    registry, captured = store

    assert registry.list_basin_versions(basin_id=basin_id, limit=10, offset=0) == []
    assert any("FROM core.basin_version WHERE basin_id = %s" in sql for sql, _ in captured)


def test_versions_route_answers_404_for_an_evidence_only_basin(store: Any) -> None:
    registry, _captured = store
    app.dependency_overrides[get_model_registry_store] = lambda: registry
    try:
        with TestClient(app) as client:
            evidence = client.get(f"/api/v1/basins/{EVIDENCE_BASIN_ID}/versions")
            missing = client.get("/api/v1/basins/basins_absent/versions")
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert evidence.status_code == 404
    # Indistinguishable from a basin that does not exist, apart from the echoed id.
    assert evidence.json()["error"]["code"] == missing.json()["error"]["code"]
    assert "geom" not in evidence.text


@pytest.mark.parametrize(("active", "leading"), [(None, []), (False, [False]), (True, [True])])
def test_list_models_excludes_evidence_only_basins_in_count_and_page(
    store: Any, active: bool | None, leading: list[Any]
) -> None:
    registry, captured = store

    registry.list_models(basin_version_id=None, active=active, limit=5, offset=10)

    (count_sql, count_params), (page_sql, page_params) = captured
    for sql in (count_sql, page_sql):
        assert "JOIN core.basin b ON b.basin_id = bv.basin_id" in sql
        assert "b.basin_group IS DISTINCT FROM %s" in sql
    assert list(count_params) == [*leading, EVIDENCE_ONLY_BASIN_GROUP]
    assert list(page_params) == [*leading, EVIDENCE_ONLY_BASIN_GROUP, 5, 10]


def test_get_model_hides_a_model_under_an_evidence_only_basin(store: Any) -> None:
    registry, _captured = store

    with pytest.raises(MissingResourceError, match="model_id not found"):
        registry.get_model(f"{EVIDENCE_BASIN_ID}_model")
    assert registry.get_model("basins_qhh_model")["model_id"] == "basins_qhh_model"
    assert registry.get_model("basins_ungrouped_model")["model_id"] == "basins_ungrouped_model"
    # The scheduler / lifecycle seam is unfiltered.
    assert registry.get_model_internal(f"{EVIDENCE_BASIN_ID}_model")["basin_id"] == EVIDENCE_BASIN_ID
