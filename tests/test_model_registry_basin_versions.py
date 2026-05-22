from __future__ import annotations

from typing import Any

import pytest

from packages.common.model_registry import PsycopgModelRegistryStore


def test_list_basin_versions_public_projection_redacts_source_uri_and_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self._result: Any = None

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            if "SELECT 1 FROM core.basin" in statement:
                self._result = {"exists": 1}
            else:
                self._result = [
                    {
                        "basin_version_id": "basins_qhh_vbasins",
                        "basin_id": "basins_qhh",
                        "version_label": "vbasins",
                        "geom": {"type": "MultiPolygon", "coordinates": []},
                        "active_flag": True,
                        "valid_from": None,
                        "valid_to": None,
                        "source_uri": "/volume/data/nwm/Basins/qhh/gis/domain.shp",
                        "checksum": "checksum-secret",
                        "created_at": "2026-05-14T00:00:00Z",
                    }
                ]

        def fetchone(self) -> dict[str, Any] | None:
            return self._result

        def fetchall(self) -> list[dict[str, Any]]:
            return self._result

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    versions = store.list_basin_versions(basin_id="basins_qhh", limit=10, offset=0)

    assert versions[0]["source_uri"] is None
    assert versions[0]["checksum"] is None
