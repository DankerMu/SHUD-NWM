"""Generic Docker collection-gate: opt-in does not unlock ordinary DB integration."""

from __future__ import annotations

from tests import conftest


class _FakeItem:
    def __init__(self, keywords: set[str]) -> None:
        self.keywords = keywords
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def _skip_markers(item: _FakeItem) -> list[object]:
    return [marker for marker in item.markers if getattr(marker, "name", None) == "skip"]


def test_docker_opt_in_does_not_unlock_ordinary_db_integration(monkeypatch) -> None:
    monkeypatch.setenv("NHMS_RUN_NODE27_DOCKER", "1")
    monkeypatch.delenv("NHMS_RUN_INTEGRATION", raising=False)
    monkeypatch.delenv("NHMS_INTEGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_ALLOW_DATABASE_URL_INTEGRATION", raising=False)

    assert conftest._node27_docker_skip_reason() is None
    assert conftest._integration_skip_reason() is not None

    assert conftest._is_node27_docker_keywords({"integration", "timescaledb_210", "node27_docker"}) is True
    assert conftest._is_node27_docker_keywords({"integration", "node27_docker"}) is False
    assert conftest._is_node27_docker_keywords({"integration", "timescaledb_210"}) is False
    assert conftest._is_node27_docker_keywords({"integration"}) is False

    docker_item = _FakeItem({"integration", "timescaledb_210", "node27_docker"})
    ordinary_item = _FakeItem({"integration"})
    conftest.pytest_collection_modifyitems(None, [docker_item, ordinary_item])

    assert _skip_markers(docker_item) == []
    assert len(_skip_markers(ordinary_item)) == 1

    monkeypatch.delenv("NHMS_RUN_NODE27_DOCKER", raising=False)
    assert conftest._node27_docker_skip_reason() is not None
    disabled_docker_item = _FakeItem({"integration", "timescaledb_210", "node27_docker"})
    conftest.pytest_collection_modifyitems(None, [disabled_docker_item])
    assert len(_skip_markers(disabled_docker_item)) == 1
