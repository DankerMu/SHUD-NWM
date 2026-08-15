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


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker)
    assert start < end, f"{start_marker} no longer precedes {end_marker}"
    return source[start:end]


def test_national_digest_membership_shares_one_status_set_with_the_data_side_queries() -> None:
    mvt_source = MVT_SOURCE.read_text(encoding="utf-8")
    predicate = f"h.status IN ({DISPLAY_READY_STATUS_SET})"

    digest_source = _slice(
        mvt_source,
        "def national_discharge_source_version",
        "def national_river_network_source_version",
    )
    # Ends at the next function, NOT at `_national_source_digest` 350 lines later:
    # a slice that swallows the digest helpers makes the data-side assertions
    # below vacuous (they would be satisfied by the digest's own predicate).
    tile_sql_source = _slice(mvt_source, "def postgis_tile_sql", "def _mvt_public_tile_columns")
    valid_times_source = _slice(
        mvt_source,
        "def national_discharge_valid_times",
        "def _valid_time_discovery",
    )
    assert "def national_discharge_source_version" not in tile_sql_source
    assert "def _national_source_digest" not in tile_sql_source

    # The digest basis deliberately omits `status` (publish changes nothing the
    # national layer renders); that only holds while membership on both sides is
    # decided by the same three statuses.
    assert "h.updated_at" in digest_source
    assert '"status"' not in digest_source

    # Counts, not mere containment: the tile SQL carries the predicate twice (the
    # identity-stats EXISTS probe and the latest_runs CTE) and the whole module
    # five times (those two, display_ready_run, the digest membership query, the
    # national valid-time discovery). Deleting or loosening any single copy —
    # or adding an unguarded sixth query — moves a count and reddens here.
    assert tile_sql_source.count(predicate) == 2, (
        "both national data-side queries in postgis_tile_sql must filter runs by the "
        "display-ready status set the digest's membership query uses"
    )
    assert digest_source.count(predicate) == 1
    assert valid_times_source.count(predicate) == 1
    assert mvt_source.count(predicate) == 5

    # And the set itself is shared: no query may quietly use a different one.
    assert set(re.findall(r"status IN \(([^)]*)\)", mvt_source)) == {DISPLAY_READY_STATUS_SET}
