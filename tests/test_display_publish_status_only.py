"""Publish is a status-only transition (#1120, display-coverage-freshness).

``hydro.hydro_run.updated_at`` means "run data changed" (register, parse). The
autopipe publish phase used to bump it too, which re-staled every run whose
coverage the same tick had just refreshed (staleness is
``coverage.refreshed_at < run.updated_at``) and made the cron backstop recompute
each freshly published run for nothing. Requirements covered here:

* the publish UPDATE touches ``status`` only;
* display caches still rotate on publish: the run-scoped MVT revision digest
  includes ``status``, so parsed -> published changes the tile version even with
  ``updated_at`` frozen;
* the national discharge digest may keep ignoring ``status`` only while its
  membership query and the national data-side queries agree on one status set
  (a run enters the national layer at parse time, not at publish time).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from apps.api.routes.hydro_display import _run_source_version

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOPIPELINE = REPO_ROOT / "scripts" / "node27_autopipeline.py"
MVT_SOURCE = REPO_ROOT / "services" / "tiles" / "mvt.py"

DISPLAY_READY_STATUS_SET = "'succeeded', 'parsed', 'published'"


def _publish_display_runs_source() -> str:
    source = AUTOPIPELINE.read_text(encoding="utf-8")
    start = source.index("def _publish_display_runs")
    return source[start : source.index("\ndef ", start)]


def _run_row(status: str) -> dict[str, object]:
    return {
        "run_id": "qhh_gfs_2026050700",
        "basin_version_id": "basins_qhh_vbasins",
        "river_network_version_id": "basins_qhh_rivnet_vbasins",
        "source_id": "gfs",
        "cycle_time": datetime(2026, 5, 7, tzinfo=UTC),
        "status": status,
        "updated_at": datetime(2026, 5, 7, 3, tzinfo=UTC),
    }


def test_publish_update_sets_status_only() -> None:
    publish_source = _publish_display_runs_source()
    statement = publish_source[
        publish_source.index("cur.execute(") : publish_source.index("return cur.rowcount")
    ]

    assert "SET status = 'published'" in statement
    assert "updated_at" not in statement, (
        "publish must not advance hydro_run.updated_at: it would re-stale the coverage "
        "refreshed during the same tick's ingest phase"
    )
    assert "WHERE h.status = 'parsed'" in statement


def test_run_scoped_mvt_revision_rotates_on_publish_without_an_updated_at_bump() -> None:
    parsed_version = _run_source_version(_run_row("parsed"))
    published_version = _run_source_version(_run_row("published"))

    assert parsed_version != published_version
    # Only the digest suffix moves; the base version (the network identity) does not.
    assert parsed_version.split(";")[0] == published_version.split(";")[0]
    assert _run_source_version(_run_row("parsed")) == parsed_version


def test_national_digest_membership_shares_one_status_set_with_the_data_side_queries() -> None:
    mvt_source = MVT_SOURCE.read_text(encoding="utf-8")
    digest_source = mvt_source[
        mvt_source.index("def national_discharge_source_version") : mvt_source.index(
            "def national_river_network_source_version"
        )
    ]
    tile_sql_source = mvt_source[
        mvt_source.index("def postgis_tile_sql") : mvt_source.index("def _national_source_digest")
    ]
    valid_times_source = mvt_source[
        mvt_source.index("def national_discharge_valid_times") : mvt_source.index(
            "def _valid_time_discovery"
        )
    ]

    # The digest basis deliberately omits `status` (publish changes nothing the
    # national layer renders); that only holds while membership on both sides is
    # decided by the same three statuses.
    assert "h.updated_at" in digest_source
    assert '"status"' not in digest_source
    for source in (digest_source, tile_sql_source, valid_times_source):
        assert f"h.status IN ({DISPLAY_READY_STATUS_SET})" in source
    assert set(re.findall(r"status IN \(([^)]*)\)", mvt_source)) == {DISPLAY_READY_STATUS_SET}
