"""The public layer catalog and the live-PostGIS MVT gate.

Split out of `apps/api/routes/hydro_display.py` (#2026). `_mvt_live_postgis_enabled`
is patched by tests through THIS module: both of its consumers
(`_default_layer_catalog` and `_require_live_postgis_mvt`) live here, so the facade
is no longer a patch target for it.
"""

from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.hydro_display_constants import PUBLIC_LAYER_DEFINITIONS
from apps.api.routes.hydro_display_models import Layer
from services.tiles.mvt import (
    MVT_VALID_TIME_SAMPLE_LIMIT,
    NATIONAL_DISCHARGE_DEFAULT_SOURCE,
    ValidTimeDiscovery,
    layer_metadata,
    national_discharge_cycles,
    national_discharge_source_version,
    national_discharge_valid_times,
)


def _mvt_live_postgis_enabled(session: Session) -> bool:
    return session.get_bind().dialect.name != "sqlite" and os.getenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _require_live_postgis_mvt(session: Session, layer_id: str) -> None:
    if _mvt_live_postgis_enabled(session):
        return
    raise ApiError(
        status_code=424,
        code="MVT_LIVE_POSTGIS_UNAVAILABLE",
        message="Live PostGIS MVT is required for canonical .pbf tile routes and is not enabled.",
        details={"layer_id": layer_id, "required_env": "NHMS_ENABLE_LIVE_POSTGIS_MVT=true"},
    )


def _default_layer_catalog(
    session: Session,
    *,
    run_id: str,
    source_version: str,
    basin_version_id: str,
    river_network_version_id: str,
    river_network_source_version: str,
    national_river_source_version: str,
    # Left `None` by `/api/v1/layers`, which cannot compute it: the discharge
    # entry's digest is scoped to the identity resolved BELOW. Callers that already
    # hold a digest (tests exercising this helper directly) pass it and the
    # identity-scoped query is skipped entirely.
    national_hydro_source_version: str | None = None,
    national: bool = False,
) -> list[Layer]:
    layers = []
    default_cycle: str | None = None
    for layer_id, name, layer_type, variables in PUBLIC_LAYER_DEFINITIONS:
        if layer_id == "discharge":
            # One national identity for the whole entry, independent of `run_id`:
            # the default cycle comes from the fail-closed intersection, and the
            # advertised list comes from the SAME function
            # `/api/v1/layers/discharge/valid-times?source=&cycle=` serves, so the
            # frontend can skip the round trip while the identity is the default.
            default_cycle = national_discharge_cycles(session, source=NATIONAL_DISCHARGE_DEFAULT_SOURCE)[
                "default_cycle"
            ]
            # `canonical_mvt_time` spelling only: seconds precision with a literal
            # `Z`, which `fromisoformat` reads back exactly. Parsed once and reused
            # by the valid-times call and the digest below.
            default_cycle_instant = None if default_cycle is None else datetime.fromisoformat(default_cycle)
            valid_time_sample = (
                _empty_valid_times()
                if default_cycle_instant is None
                else national_discharge_valid_times(
                    session,
                    source=NATIONAL_DISCHARGE_DEFAULT_SOURCE,
                    cycle=default_cycle_instant,
                )
            )
            # The two calls above are two full helper invocations, so they hold an
            # OUTER snapshot seam on top of the inner two-statement one each of them
            # owns (#2087 orders that inner pair and stays out of this one). The
            # intersection can empty out between the calls -- a network ACTIVATED
            # with no display-ready run for `default_cycle`, or a covered run's
            # status / coverage row being rewritten so it stops being display-ready.
            # Both are caught by the SECOND call's own pair of reads, which is why
            # the guard below is here. DEACTIVATION between the calls is not in that
            # list: the second call then reads a consistent smaller state (the gone
            # network is in neither its covered nor its active set) and confirms
            # `default_cycle` -- correctly, since a deactivated network does not need
            # rendering. It fails closed only when it lands INSIDE one call, between
            # that call's two reads, AND the departing network still has coverage
            # rows in the coverage read -- with zero rows that call is equally
            # consistent and confirms the cycle. The contract spells the empty intersection
            # `default_cycle = null` AND `valid_times = []` together; `(C, [])`
            # advertises a cycle whose timeline is empty and is forbidden.
            if not valid_time_sample.valid_times:
                default_cycle = None
                default_cycle_instant = None
            if national_hydro_source_version is None:
                # Digest the identity this entry ADVERTISES. The argument-free form
                # keeps one row per network across ALL sources and cycles, so in the
                # normal propagation state (some networks already on the next cycle,
                # or an `ifs` cycle newest) it observes no run of
                # `(default_source, default_cycle)` at all: a corrective re-run of the
                # advertised identity would leave `metadata.version` -- and therefore
                # the frontend's cache token and MapLibre source key -- unchanged, and
                # browsers would keep the superseded tiles. With `default_cycle` null
                # the entry advertises nothing addressable, so the argument-free digest
                # is the honest input.
                national_hydro_source_version = (
                    national_discharge_source_version(session)
                    if default_cycle_instant is None
                    else national_discharge_source_version(
                        session,
                        source=NATIONAL_DISCHARGE_DEFAULT_SOURCE,
                        cycle=default_cycle_instant,
                    )
                )
        else:
            valid_time_sample = _empty_valid_times()
        layers.append(
            Layer(
                layer_id=layer_id,
                layer_name=name,
                layer_type=layer_type,
                variables=variables,
                metadata=layer_metadata(
                    layer_id,
                    run_id=run_id,
                    valid_times=valid_time_sample.valid_times,
                    valid_time_limit=valid_time_sample.limit,
                    valid_time_observed_count=valid_time_sample.observed_count,
                    valid_times_truncated=valid_time_sample.truncated,
                    source_version=(
                        national_river_source_version
                        if national and layer_id == "river-network"
                        else national_hydro_source_version
                        if layer_id == "discharge"
                        else river_network_source_version
                        if layer_id in {"river-network", "met-stations"}
                        else source_version
                    ),
                    basin_version_id=basin_version_id,
                    river_network_version_id=river_network_version_id,
                    release_blocking=not _mvt_live_postgis_enabled(session),
                    national=layer_id == "discharge" or (national and layer_id == "river-network"),
                    default_cycle=default_cycle if layer_id == "discharge" else None,
                ),
            )
        )
    return layers


def _empty_valid_times(limit: int = MVT_VALID_TIME_SAMPLE_LIMIT) -> ValidTimeDiscovery:
    return ValidTimeDiscovery(valid_times=[], limit=limit, observed_count=0, truncated=False)
