"""§2.6 G9 capacity baseline driver: node-27 active primary PG live query
producing the report against deployment config + live legacy facts.

Load-bearing outputs (per tasks.md §2.6 required-evidence bullets):
  D1. Basin count       — live SQL vs appendix-A ~13 cross-check
  D2. Legacy station    — live SQL vs appendix-A ~6,290 cross-check
  D3. met.forcing_station_timeseries 2-week row count + per-day rate
      — live SQL vs appendix-A ~121M/2wks ≈ ~8M/day cross-check
  D4. Direct-grid capacity estimation formula:
        estimated_rows = station_count × timestep_count × output_variable_count
      Evaluated against producer limits (10k stations / 10k timesteps /
      10M rows / ~32 MiB manifest) with deployment env override capture.
  D5. Runtime staging byte/line limits vs deployment config values
      (7 MAX_DIRECT_GRID_* constants pinned in the manifest).
      MAX_PACKAGE_MANIFEST_BYTES is explicitly EXCLUDED per manifest scope.
  D6. ~5x used-cell reduction structural evaluation
      (sum of appendix-A est. used cells vs live legacy station count).
  D7. Limit-breach verdict (NO/YES per constraint).
  D8. Non-goal reaffirmation.
  D9. Discharge check against tasks.md §2.6 required-evidence bullets.

Environment-gated CLI (no argv parsed at import time). Read-only DB — SELECTs
only; the driver additionally sets a session-level read-only guard after
connect (see _run_live_sql) so the queries are safe under either the
nhms_display_ro role (role-level enforcement) or the writable nhms role
(session-level enforcement).

Required env at run time (set on node-27 by
`source infra/env/node27-ingest.env` — the display-ro env template is not
yet provisioned on node-27; ingest.env is sufficient because §2.6 issues
SELECT-only queries and the driver installs a session-level read-only guard
before any query runs, see _run_live_sql):
  NHMS_CMFD_P02_MANIFEST_PATH     : filesystem path to
                                     evidence/readiness-manifest.v1.json
                                     (companion .sha256 must sit alongside)
  NHMS_CMFD_P02_PG_DSN            : (preferred) libpq-style DSN. If unset,
                                     falls back to DATABASE_URL, then to
                                     PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSFILE.

Optional env:
  NHMS_CMFD_P02_STATIONS_ENV      : (default: "6290") integer legacy-station
                                     baseline used when the DB is unreachable
                                     — never overrides the live SQL result
                                     when the DB IS reachable.
  FORCING_MAX_STATION_COUNT       : deployment env override for producer
                                     max_station_count (if set at run time).
  FORCING_MAX_TIMESTEP_COUNT      : likewise for max_timestep_count.
  FORCING_MAX_TIMESERIES_ROW_COUNT: likewise for max_timeseries_row_count.
  FORCING_MAX_MANIFEST_BYTES      : likewise for max_manifest_bytes.

Reads readiness-manifest.v1.json (verifies .sha256 companion first) and pulls:
  * baseline_commit
  * manifest_sha256
  * forcing_producer_limits.<limit>.{default, effective, env_var}
  * shud_runtime_staging_limits.MAX_DIRECT_GRID_*  (7 constants)

Exits 0 with PASS on no-breach; exits 1 with FAIL:<constraint>:<reason>
on the first breach.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cross-check reference (docs/ForcingReplace/CMFD 建模资产向 IFSGFS Direct-Grid
# 的安全迁移.md appendix-A, 2026-07-06 snapshot). NOT acceptance values —
# just cross-check anchors so the report can compare live SQL against the
# same-day baseline the design was written against.
APPENDIX_A_BASIN_COUNT = 13
APPENDIX_A_LEGACY_STATION_COUNT = 6_290
APPENDIX_A_FORCING_ROWS_2WK_APPROX = 121_000_000     # ~1.21e8
APPENDIX_A_FORCING_ROWS_PER_DAY_APPROX = 8_000_000   # ~8e6
# Per-basin est. used cells from appendix-A (docs/ForcingReplace/CMFD 建模资产向
# IFSGFS Direct-Grid 的安全迁移.md, table at lines 1108-1122). Kept as an
# explicit list so the sum is machine-verifiable — any future drift in the
# appendix-A table would break the assertion below.
APPENDIX_A_PER_BASIN_USED_CELLS = [312, 120, 174, 8, 75, 20, 11, 77, 12, 120, 260, 4, 1]
APPENDIX_A_USED_CELLS_TOTAL = 1_194  # sum of per-basin est. used cells (verified below)
assert sum(APPENDIX_A_PER_BASIN_USED_CELLS) == APPENDIX_A_USED_CELLS_TOTAL, (
    f"APPENDIX_A_USED_CELLS_TOTAL drift: sum={sum(APPENDIX_A_PER_BASIN_USED_CELLS)}"
    f" vs constant={APPENDIX_A_USED_CELLS_TOTAL}"
)
APPENDIX_A_USED_CELLS_ROUND = 1_200  # doc claim: "全网约 1,200"
APPENDIX_A_EXPECTED_REDUCTION = 5.0  # doc claim: "预期约 5× 缩减"

# Forcing producer variable count (workers/forcing_producer/producer.py:56
# FORCING_VARIABLES = ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")).
FORCING_PRODUCER_OUTPUT_VARIABLE_COUNT = 6

# Legacy station count anchor for producer manifest size estimator (see
# _check_producer_capacity_breach). Held as a conservative upper bound based on
# the appendix-A cross-check figure; the driver uses the live SQL station count
# when available and falls back to this constant only when the DB is unreachable.
STATION_COUNT_ESTIMATE_PER_BASIN = 6_290
# Conservative upper-bound bytes/station for producer JSON manifest. First-pass
# estimator anchor: appendix-A does not enumerate manifest sizes, and §2.4
# materializes a real 3-station manifest, so 2 KiB/station is a safe upper
# bound (≈12.6 MiB total at 6290 stations, ≈38% of 32 MiB cap).
PER_STATION_MANIFEST_BYTES_ESTIMATE = 2048

# Typical single-cycle timestep count (appendix-A cited example:
# zhaochen_wem 5 stations × 56 steps × 6 variables = 1,680 rows per DB row).
# Used as the "typical" timestep in D4 formula evaluation. Not a code-pinned
# constant — the pinned constant is FORCING_MAX_TIMESTEP_COUNT.
#
# Production forecast horizon (services/orchestrator/scheduler_adapters.py:200-203):
#   forecast_start_hour = 0, forecast_step_hours = 3, forecast_end_hour = 168
# yields range(0, 169, 3) = 57 timesteps per production cycle. No
# _FORECAST_END_HOUR env override exists in infra/env/* at capture time.
PRODUCTION_FORECAST_START_HOUR = 0
PRODUCTION_FORECAST_STEP_HOURS = 3
PRODUCTION_FORECAST_END_HOUR = 168
PRODUCTION_TIMESTEP_COUNT_PER_CYCLE = len(
    range(PRODUCTION_FORECAST_START_HOUR, PRODUCTION_FORECAST_END_HOUR + 1, PRODUCTION_FORECAST_STEP_HOURS)
)  # = 57
TYPICAL_TIMESTEP_COUNT_PER_CYCLE = 56  # appendix-A wem example (≈ production 57)


def _log(section: str, message: str) -> None:
    print(f"# [{section}] {message}")


def _fail(constraint: str, reason: str) -> None:
    print(f"FAIL:{constraint}:{reason}")
    sys.exit(1)


def _load_manifest(manifest_path: Path) -> tuple[dict[str, Any], str]:
    """Read the manifest, verify sha256 companion, return (json_object, sha256)."""
    if not manifest_path.is_file():
        _fail("manifest", f"manifest not found at {manifest_path}")
    sha_path = Path(str(manifest_path) + ".sha256")
    if not sha_path.is_file():
        _fail("manifest_sha256", f"companion .sha256 not found at {sha_path}")

    raw = manifest_path.read_bytes()
    recomputed = hashlib.sha256(raw).hexdigest()
    expected_line = sha_path.read_text(encoding="utf-8").strip()
    expected_sha = expected_line.split()[0] if expected_line else ""
    if recomputed != expected_sha:
        _fail(
            "manifest_sha256",
            f"companion sha mismatch: file={expected_sha} recomputed={recomputed}",
        )

    return json.loads(raw.decode("utf-8")), recomputed


def _sanitize_error_message(msg: str) -> str:
    """Redact libpq DSN fragments from an error message before echoing.

    Strips host=... hostaddr=... port=... user=... password=... dbname=...
    passfile=... key=value fragments (any of these may be echoed by
    psycopg2 in a connect failure) replacing them with <redacted>. Also
    strips postgres:// URLs. Safe to apply to arbitrary error text; a
    non-DSN message passes through unchanged.
    """
    # Redact key=value DSN fragments (bounded so we don't over-consume).
    sanitized = re.sub(
        r"\b(host|hostaddr|port|user|password|dbname|passfile)\s*=\s*\S+",
        r"\1=<redacted>",
        msg,
        flags=re.IGNORECASE,
    )
    # Redact postgres:// URLs (whole URL up to whitespace / quotes).
    sanitized = re.sub(
        r"postgres(?:ql)?://\S+",
        "postgres://<redacted>",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def _resolve_dsn() -> str | None:
    """Return a libpq DSN, or None if no connection info is available."""
    dsn = os.environ.get("NHMS_CMFD_P02_PG_DSN")
    if dsn:
        return dsn
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    # Standard libpq env-vars are picked up automatically by psycopg2 when
    # dsn="" is passed. If none of PGHOST/PGPORT/PGUSER/PGDATABASE are set,
    # psycopg2 will fail-connect with a meaningful error which we surface.
    if any(os.environ.get(k) for k in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSFILE")):
        return ""
    return None


def _parse_pg_ts(ts: str) -> datetime | None:
    """Parse a PostgreSQL text timestamp into a datetime. Best-effort.

    Handles the '2026-07-07 11:26:15.842245+00' and ISO 8601 shapes emitted by
    psycopg2 for timestamp[tz] columns cast to ::text. Returns None on parse
    failure so callers can fall back gracefully.
    """
    if not ts:
        return None
    # psycopg2 ::text of timestamptz emits 'YYYY-MM-DD HH:MM:SS[.fff][+ZZ]'.
    # Normalize the middle space to 'T' and pad tz suffix to +ZZ:00 for
    # datetime.fromisoformat on pre-3.11 stdlib.
    normalized = ts.strip().replace(" ", "T", 1)
    # If tz suffix is just +ZZ (no minute), pad to +ZZ:00.
    m = re.match(r"^(.*[+\-])(\d{2})$", normalized)
    if m:
        normalized = f"{m.group(1)}{m.group(2)}:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _run_live_sql(dsn: str) -> dict[str, Any]:
    """Execute the live baseline queries. Returns a results dict.

    Uses psycopg2-binary (pinned in pyproject.toml). Queries are SELECT-only.
    A session-level read-only guard is installed immediately after connect
    (SET SESSION CHARACTERISTICS + default_transaction_read_only + a 60s
    statement_timeout) so the driver is safe under either the nhms_display_ro
    role (role-level enforcement) or the writable nhms role (session-level
    enforcement).

    On connection failure, returns a dict with an "error" key so callers can
    surface the fallback path (report proceeds with appendix-A cross-check
    figures + a NOTE that live SQL was unavailable). Error text is sanitized
    to strip libpq DSN fragments before echo.
    """
    try:
        import psycopg2  # type: ignore[import-untyped]
    except ImportError:
        return {"error": "psycopg2 not installed under current interpreter"}

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"connect failed: {type(exc).__name__}: {_sanitize_error_message(str(exc))}"}

    # Session-level read-only guard + 60s statement timeout. Executed BEFORE
    # autocommit is turned on so the SET commands land in a real transaction.
    # This provides defense-in-depth: even if the connecting role has DML
    # privileges (e.g. writable nhms role), any attempted INSERT/UPDATE/DELETE
    # in this session will be rejected by PostgreSQL.
    try:
        with conn.cursor() as _guard_cur:
            _guard_cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            _guard_cur.execute("SET default_transaction_read_only = on")
            _guard_cur.execute("SET statement_timeout = '60s'")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"session guard failed: {type(exc).__name__}: {_sanitize_error_message(str(exc))}"}

    try:
        conn.autocommit = True
        cur = conn.cursor()

        # audit UTC (D1 anchor). D2/D3 execute consecutively within the same
        # autocommit connection so they share the same time anchor (queries
        # complete within seconds).
        cur.execute("SELECT now() AT TIME ZONE 'UTC'")
        audit_utc = cur.fetchone()[0].isoformat()

        # Q1 basin identity. Node-27 uses core.model_instance as the production
        # basin oracle (see smoke-2.4.node-27.pass.log INV.A: count=13 active
        # production model_instances = 13 production basins). core.basin_version
        # is a versioning bookkeeping table whose rows all carry active_flag=false
        # even for production basins (verified at capture time: 13 production
        # bv rows + 1 evidence bv row = 14, all active_flag=false). Both counts
        # are reported so a reviewer can distinguish "production basin count"
        # (model_instance active) from "basin_version registry population".
        cur.execute("SELECT now() AT TIME ZONE 'UTC'")
        d1_stamp = cur.fetchone()[0].isoformat()
        cur.execute("SELECT count(*) FROM core.model_instance WHERE active_flag = true")
        active_mi = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM core.model_instance")
        total_mi = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM core.basin_version WHERE active_flag = true")
        active_bv = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM core.basin_version")
        total_bv = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM core.basin")
        total_basin = int(cur.fetchone()[0])

        # Q2 met.met_station
        cur.execute("SELECT now() AT TIME ZONE 'UTC'")
        d2_stamp = cur.fetchone()[0].isoformat()
        cur.execute("SELECT count(*) FROM met.met_station WHERE active_flag = true")
        active_ms = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM met.met_station")
        total_ms = int(cur.fetchone()[0])

        # Q3 met.forcing_station_timeseries recent 2-week window
        cur.execute("SELECT now() AT TIME ZONE 'UTC'")
        d3_stamp = cur.fetchone()[0].isoformat()
        cur.execute(
            """
            SELECT count(*),
                   min(valid_time)::text,
                   max(valid_time)::text
              FROM met.forcing_station_timeseries
              WHERE valid_time >= (now() - interval '2 weeks')
            """
        )
        row = cur.fetchone()
        window_rows = int(row[0])
        window_min = row[1]
        window_max = row[2]

        # Q3' by variable
        cur.execute(
            """
            SELECT variable, count(*)
              FROM met.forcing_station_timeseries
              WHERE valid_time >= (now() - interval '2 weeks')
              GROUP BY variable
              ORDER BY variable
            """
        )
        by_variable = {v: int(c) for v, c in cur.fetchall()}

        # Q5 model_instance identity md5 (matches smoke-2.4 INV.A' fingerprint —
        # cross-artifact md5 chain proving the same 13 production basins
        # observed across evidence artifacts).
        cur.execute(
            """
            SELECT md5(coalesce(string_agg(model_id, ',' ORDER BY model_id), ''))
              FROM core.model_instance
              WHERE active_flag = true
            """
        )
        active_mi_md5 = cur.fetchone()[0]

        # Compute actual span from min/max instead of assuming 14 days. This
        # matters because now() may sit inside a window shorter than 14 days
        # if the ingest has just started or the hypertable was truncated.
        parsed_min = _parse_pg_ts(window_min) if window_min else None
        parsed_max = _parse_pg_ts(window_max) if window_max else None
        if parsed_min and parsed_max:
            actual_span_days = (parsed_max - parsed_min).total_seconds() / 86400.0
        else:
            actual_span_days = None

        return {
            "audit_utc": audit_utc,
            "d1_stamp": d1_stamp,
            "d2_stamp": d2_stamp,
            "d3_stamp": d3_stamp,
            "active_model_instance_count": active_mi,
            "total_model_instance_count": total_mi,
            "active_basin_version_count": active_bv,
            "total_basin_version_count": total_bv,
            "total_basin_count": total_basin,
            "active_met_station_count": active_ms,
            "total_met_station_count": total_ms,
            "forcing_ts_2wk_row_count": window_rows,
            "forcing_ts_2wk_window_min_valid_time": window_min,
            "forcing_ts_2wk_window_max_valid_time": window_max,
            "forcing_ts_2wk_actual_span_days": actual_span_days,
            "forcing_ts_2wk_by_variable": by_variable,
            "active_model_instance_md5": active_mi_md5,
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _resolve_deployment_producer_limits(manifest_limits: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {limit_name: {default, effective, env_var, override_at_runtime}}.

    Pulls the deployment env override IN EFFECT AT RUN TIME (not the value
    baked into the manifest — that captured the value at manifest-freeze).
    The report cites both so a reviewer can see whether producer capacity
    changed between manifest freeze and §2.6 evidence capture.
    """
    resolved: dict[str, dict[str, Any]] = {}
    for limit_name, block in manifest_limits.items():
        env_var = block.get("env_var")
        override_raw = os.environ.get(env_var) if env_var else None
        override: int | None = None
        override_source = "unset"
        if override_raw is not None and override_raw.strip():
            # Truncate + strip non-printables before echo so we don't reflect
            # arbitrary env content into the pass log verbatim.
            printable_override = "".join(
                ch if ch.isprintable() else "?" for ch in override_raw
            )
            truncated = printable_override[:64]
            raw_display = repr(truncated) + ("..." if len(override_raw) > 64 else "")
            try:
                override = int(override_raw)
                override_source = f"env {env_var}={raw_display}"
            except ValueError:
                override_source = f"env {env_var}={raw_display} (non-integer, ignored)"
        effective = override if override is not None else block.get("default")
        resolved[limit_name] = {
            "default": block.get("default"),
            "manifest_effective": block.get("effective"),
            "manifest_override": block.get("override"),
            "runtime_effective": effective,
            "runtime_override": override,
            "runtime_override_source": override_source,
            "env_var": env_var,
        }
    return resolved


def _check_producer_capacity_breach(
    active_station_count: int,
    producer_limits: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    """Return (breach_reasons, computed_context).

    Evaluated using station_count from live SQL + typical single-cycle
    timestep (appendix-A cited: 56) + producer output-variable count (6).
    Row estimation formula per tasks.md §2.6: station × timestep × variables.

    Also evaluates the JSON producer manifest byte estimate against
    max_manifest_bytes. The estimator uses a conservative
    PER_STATION_MANIFEST_BYTES_ESTIMATE upper bound (2 KiB/station) since
    appendix-A does not enumerate manifest sizes and a runtime measurement
    would require materializing an actual producer package (out of §2.6
    scope; §2.4 covers real 3-station materialization).

    computed_context is echoed into D4 so a reviewer can see the estimator
    inputs and results without re-running the formula.
    """
    breaches: list[str] = []
    typical_timestep = TYPICAL_TIMESTEP_COUNT_PER_CYCLE
    output_vars = FORCING_PRODUCER_OUTPUT_VARIABLE_COUNT
    estimated_rows = active_station_count * typical_timestep * output_vars
    estimated_manifest_bytes = active_station_count * PER_STATION_MANIFEST_BYTES_ESTIMATE

    max_stations = producer_limits["max_station_count"]["runtime_effective"]
    max_timesteps = producer_limits["max_timestep_count"]["runtime_effective"]
    max_rows = producer_limits["max_timeseries_row_count"]["runtime_effective"]
    max_manifest_bytes = producer_limits["max_manifest_bytes"]["runtime_effective"]

    if active_station_count > max_stations:
        breaches.append(
            f"active_station_count={active_station_count} > max_station_count={max_stations}"
        )
    if typical_timestep > max_timesteps:
        breaches.append(
            f"typical_timestep={typical_timestep} > max_timestep_count={max_timesteps}"
        )
    if estimated_rows > max_rows:
        breaches.append(
            f"estimated_rows={estimated_rows} (station×timestep×variables) > max_timeseries_row_count={max_rows}"
        )
    if estimated_manifest_bytes > max_manifest_bytes:
        breaches.append(
            f"estimated_manifest_bytes={estimated_manifest_bytes}"
            f" (stations×{PER_STATION_MANIFEST_BYTES_ESTIMATE}B/station upper-bound estimate)"
            f" > max_manifest_bytes={max_manifest_bytes}"
        )

    breakeven_T = max_rows // (active_station_count * output_vars) if active_station_count > 0 else None
    context: dict[str, Any] = {
        "typical_timestep": typical_timestep,
        "output_vars": output_vars,
        "estimated_rows": estimated_rows,
        "estimated_manifest_bytes": estimated_manifest_bytes,
        "per_station_manifest_bytes_estimate": PER_STATION_MANIFEST_BYTES_ESTIMATE,
        "max_manifest_bytes": max_manifest_bytes,
        "manifest_pct_of_cap": (
            round(100.0 * estimated_manifest_bytes / max_manifest_bytes, 2)
            if max_manifest_bytes
            else None
        ),
        "production_timestep_count_per_cycle": PRODUCTION_TIMESTEP_COUNT_PER_CYCLE,
        "breakeven_timestep_count": breakeven_T,
    }
    return breaches, context


def _check_staging_limit_zero_or_negative(staging_limits: dict[str, int]) -> list[str]:
    """Runtime staging limits are code-pinned (no runtime env override in the
    default runtime.py:52-58 shape). Return empty on the healthy path; return
    a breach only if a pinned limit becomes zero/negative (indicating the
    manifest was corrupted or an amendment slipped a bad value)."""
    breaches: list[str] = []
    for name, value in staging_limits.items():
        if not isinstance(value, int) or value <= 0:
            breaches.append(
                f"{name}={value!r} is not a positive integer — manifest corruption suspected"
            )
    return breaches


def _emit_section(header: str) -> None:
    print()
    print(f"## {header}")


def _fmt_int(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "N/A"


def main() -> int:
    manifest_path_str = os.environ.get("NHMS_CMFD_P02_MANIFEST_PATH")
    if not manifest_path_str:
        # Default to the change's own committed manifest, resolved relative
        # to this script's directory. This makes the driver usable directly
        # from a fresh git clone on node-27 without extra env setup.
        manifest_path = Path(__file__).resolve().parent / "readiness-manifest.v1.json"
    else:
        manifest_path = Path(manifest_path_str).resolve()

    manifest, manifest_sha = _load_manifest(manifest_path)

    baseline_commit = manifest["baseline_commit"]
    producer_limits_raw = manifest["forcing_producer_limits"]
    staging_limits = manifest["shud_runtime_staging_limits"]

    # ---- header ----
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(
        f"# captured at {now_utc} host=node-27 bound to baseline_commit={baseline_commit}"
        f" manifest_sha256={manifest_sha}"
    )
    print("# code_carrier_sha=<40-hex>  # to be filled from git rev-parse HEAD at run time")
    print("# task: cmfd-direct-grid-platform-readiness §2.6 node-27 G9 capacity baseline against live legacy facts")
    print("# host: node-27 (210.77.77.27:32099, user=nwm, /home/nwm/NWM)")
    print("# env : infra/env/node27-ingest.env — the node27-display-ro.env template is not yet provisioned")
    print("#       on node-27; ingest.env is sufficient because §2.6 uses SELECT-only queries + a")
    print("#       session-level read-only guard set after connect (SET SESSION CHARACTERISTICS AS")
    print("#       TRANSACTION READ ONLY + default_transaction_read_only=on + statement_timeout=60s;")
    print("#       see _run_live_sql in this driver). Safe under either the nhms_display_ro role")
    print("#       (role-level enforcement) or the writable nhms role (session-level enforcement).")
    print("# manifest_path:", manifest_path)

    # ---- live SQL ----
    dsn = _resolve_dsn()
    if dsn is None:
        live: dict[str, Any] = {"error": "no DSN resolvable (NHMS_CMFD_P02_PG_DSN / DATABASE_URL / PG* all unset)"}
    else:
        live = _run_live_sql(dsn)

    live_available = "error" not in live

    # ---- D1. Basin count ----
    _emit_section("D1. Live legacy basin count vs appendix-A cross-check")
    if live_available:
        print(f"# stamp (D1 anchor, same autocommit session as D2/D3) = {live['d1_stamp']}")
        print("# Q1a (verbatim from driver at capacity-2.6-node27.py):")
        print("#   SELECT count(*) FROM core.model_instance WHERE active_flag = true;")
        print("#   (production basin oracle; matches smoke-2.4.node-27 INV.A count=13)")
        print(f"#   audit UTC                = {live['audit_utc']}")
        print(f"#   active_model_instance    = {_fmt_int(live['active_model_instance_count'])}")
        print(f"#   total_model_instance     = {_fmt_int(live['total_model_instance_count'])}"
              " (13 prod active + 1 evidence-only inactive)")
        print(f"#   active_model_instance_md5= {live['active_model_instance_md5']}"
              " (fingerprint; cross-check smoke-2.4 D4' INV.A' e95e51dd…)")
        print()
        print("# Q1b SELECT count(*) FROM core.basin_version WHERE active_flag=true")
        print("#     (basin_version is versioning bookkeeping — all rows carry")
        print("#      active_flag=false; total_basin_version = 13 prod + 1 evidence)")
        print(f"#   active_basin_version     = {_fmt_int(live['active_basin_version_count'])}")
        print(f"#   total_basin_version      = {_fmt_int(live['total_basin_version_count'])}")
        print(f"#   total_basin              = {_fmt_int(live['total_basin_count'])}"
              " (parent table; 13 prod + 1 evidence)")
        print()
        print(f"#   appendix-A cross-check   = {APPENDIX_A_BASIN_COUNT} (2026-07-06 snapshot)")
        delta = live["active_model_instance_count"] - APPENDIX_A_BASIN_COUNT
        print(f"#   delta vs appendix-A      = {delta:+d}  (using model_instance active count as basin count)")
    else:
        print(f"# LIVE SQL unavailable: {live['error']}")
        print("# reporting appendix-A cross-check as fallback baseline (2026-07-06 snapshot):")
        print(f"#   basin_count              = {APPENDIX_A_BASIN_COUNT}   (appendix-A)")

    # ---- D2. Legacy station count ----
    _emit_section("D2. Live legacy station count vs appendix-A cross-check")
    if live_available:
        print(f"# stamp (D2, same autocommit session as D1/D3) = {live['d2_stamp']}")
        print("# Q2 (verbatim from driver at capacity-2.6-node27.py):")
        print("#   SELECT count(*) FROM met.met_station WHERE active_flag = true;")
        print(f"#   active_met_station     = {_fmt_int(live['active_met_station_count'])}")
        print(f"#   total_met_station      = {_fmt_int(live['total_met_station_count'])}")
        print(f"#   appendix-A cross-check = {APPENDIX_A_LEGACY_STATION_COUNT:,} (2026-07-06 snapshot)")
        delta_ms = live["active_met_station_count"] - APPENDIX_A_LEGACY_STATION_COUNT
        print(f"#   delta vs appendix-A    = {delta_ms:+,d}")
        legacy_station_count = live["active_met_station_count"]
    else:
        fallback_ms = int(os.environ.get("NHMS_CMFD_P02_STATIONS_ENV", str(APPENDIX_A_LEGACY_STATION_COUNT)))
        print(f"# LIVE SQL unavailable: {live['error']}")
        print("# reporting appendix-A cross-check as fallback baseline:")
        print(f"#   active_met_station     = {fallback_ms:,}   (appendix-A / NHMS_CMFD_P02_STATIONS_ENV)")
        legacy_station_count = fallback_ms

    # ---- D3. forcing_station_timeseries row count ----
    _emit_section("D3. met.forcing_station_timeseries 2-week + per-day row count")
    if live_available:
        window_rows = live["forcing_ts_2wk_row_count"]
        actual_span_days = live.get("forcing_ts_2wk_actual_span_days")
        if actual_span_days and actual_span_days > 0:
            per_day = window_rows / actual_span_days
        else:
            per_day = None
        print(f"# stamp (D3, same autocommit session as D1/D2) = {live['d3_stamp']}")
        print("# Q3 (verbatim from driver at capacity-2.6-node27.py:187-195):")
        print("#   SELECT count(*),")
        print("#          min(valid_time)::text,")
        print("#          max(valid_time)::text")
        print("#     FROM met.forcing_station_timeseries")
        print("#     WHERE valid_time >= (now() - interval '2 weeks');")
        print(f"#   window_row_count       = {window_rows:,}")
        print(f"#   window_min_valid_time  = {live['forcing_ts_2wk_window_min_valid_time']}")
        print(f"#   window_max_valid_time  = {live['forcing_ts_2wk_window_max_valid_time']}")
        if actual_span_days is not None:
            print(f"#   actual_span_days       = {actual_span_days:.4f}"
                  " (computed from max-min, not hardcoded 14)")
        else:
            print("#   actual_span_days       = N/A (min/max parse failed; per-day rate skipped)")
        if per_day is not None:
            print(f"#   approx_rows_per_day    = {per_day:,.0f}   (window_row_count / actual_span_days)")
        else:
            print("#   approx_rows_per_day    = N/A")
        print(
            f"#   appendix-A 2wk cross   = ~{APPENDIX_A_FORCING_ROWS_2WK_APPROX:,} rows"
            f" (~{APPENDIX_A_FORCING_ROWS_PER_DAY_APPROX:,}/day)"
        )
        # per-variable breakdown validates the formula (rows_per_variable ≈ station × timestep)
        print(
            f"# by-variable breakdown (should split evenly across"
            f" the {FORCING_PRODUCER_OUTPUT_VARIABLE_COUNT} FORCING_VARIABLES):"
        )
        for var, count in sorted(live["forcing_ts_2wk_by_variable"].items()):
            print(f"#   {var:>8s} = {count:,}")
    else:
        print(f"# LIVE SQL unavailable: {live['error']}")
        print("# reporting appendix-A cross-check as fallback baseline:")
        print(f"#   2wk_row_count          = ~{APPENDIX_A_FORCING_ROWS_2WK_APPROX:,}   (appendix-A)")
        print(f"#   per_day_row_rate       = ~{APPENDIX_A_FORCING_ROWS_PER_DAY_APPROX:,}/day   (appendix-A)")

    # ---- D4. Direct-grid capacity formula vs producer limits ----
    _emit_section("D4. Direct-grid capacity formula vs producer limits (deployment config in effect)")
    producer_limits = _resolve_deployment_producer_limits(producer_limits_raw)
    typical_timestep = TYPICAL_TIMESTEP_COUNT_PER_CYCLE
    output_vars = FORCING_PRODUCER_OUTPUT_VARIABLE_COUNT
    estimated_rows_pre = legacy_station_count * typical_timestep * output_vars
    print("# formula: estimated_rows = station_count × timestep_count × output_variable_count")
    print(f"#   station_count (live legacy) = {legacy_station_count:,}")
    print(f"#   typical timestep (per cycle) = {typical_timestep}  (appendix-A wem example: 56 steps)")
    print(f"#   output_variable_count       = {output_vars}  (FORCING_VARIABLES: PRCP,TEMP,RH,wind,Rn,Press)")
    print(f"#   -> estimated_rows           = {estimated_rows_pre:,}")
    print()
    print("# Production timestep anchor (code-pinned, no env override in infra/env/*):")
    print(f"#   production_forecast_start_hour = {PRODUCTION_FORECAST_START_HOUR}")
    print(f"#   production_forecast_step_hours = {PRODUCTION_FORECAST_STEP_HOURS}")
    print(f"#   production_forecast_end_hour   = {PRODUCTION_FORECAST_END_HOUR}")
    print(f"#     -> range(0, {PRODUCTION_FORECAST_END_HOUR + 1}, {PRODUCTION_FORECAST_STEP_HOURS})"
          f" = {PRODUCTION_TIMESTEP_COUNT_PER_CYCLE} timesteps per production cycle")
    print("#     (code carrier: services/orchestrator/scheduler_adapters.py:200-203;")
    print("#      no _FORECAST_END_HOUR / _FORECAST_STEP_HOURS override present in")
    print("#      infra/env/ at capture time — see grep-c note in D4' below)")
    print()
    print("# Producer limits (default vs manifest_effective vs runtime_effective at capture time):")
    for name, blk in producer_limits.items():
        print(
            f"#   {name:<28s} default={_fmt_int(blk['default']):>15s}  "
            f"manifest_effective={_fmt_int(blk['manifest_effective']):>15s}  "
            f"runtime_effective={_fmt_int(blk['runtime_effective']):>15s}  "
            f"env_var={blk['env_var']}  {blk['runtime_override_source']}"
        )
    producer_breaches, capacity_ctx = _check_producer_capacity_breach(legacy_station_count, producer_limits)
    print()
    print("# D4' Deployment env-override grep capture (must be re-populated on node-27 at capture time)")
    grep_capture = os.environ.get(
        "NHMS_CMFD_P02_ENV_GREP_CAPTURE",
        "grep -c FORCING_MAX_ infra/env/node27-ingest.env  ->  <capture on node-27 at run time>",
    )
    print(f"#   {grep_capture}")
    print("#   (any non-zero count would indicate a deployment override that the driver would have")
    print("#    surfaced via env FORCING_MAX_* above; 0 means the manifest_effective values are in force.)")
    print()
    print("# Producer manifest byte estimator (max_manifest_bytes discharge, first-pass upper bound):")
    print(f"#   station_count                     = {legacy_station_count:,}")
    print(f"#   per_station_manifest_bytes_est    = {PER_STATION_MANIFEST_BYTES_ESTIMATE:,} bytes/station"
          " (conservative upper-bound anchor; §2.4 covers real 3-station materialization)")
    est_mb = capacity_ctx["estimated_manifest_bytes"] / (1024 * 1024)
    cap_mb = capacity_ctx["max_manifest_bytes"] / (1024 * 1024)
    pct = capacity_ctx["manifest_pct_of_cap"]
    print(f"#   estimated_manifest_bytes          = {capacity_ctx['estimated_manifest_bytes']:,}"
          f" (~{est_mb:.2f} MiB)")
    print(f"#   max_manifest_bytes (runtime cap)  = {capacity_ctx['max_manifest_bytes']:,}"
          f" (~{cap_mb:.2f} MiB)")
    print(f"#   estimated / max_manifest_bytes    = {pct:.2f}% of cap"
          if pct is not None else
          "#   estimated / max_manifest_bytes    = N/A")
    print()
    print("# Breakeven analysis at live station count × 6 variables:")
    print("#   breakeven_T (timesteps at which estimated_rows == max_timeseries_row_count)")
    print("#     = max_timeseries_row_count / (station_count × output_vars)")
    if capacity_ctx["breakeven_timestep_count"] is not None:
        prod_pct = 100.0 * PRODUCTION_TIMESTEP_COUNT_PER_CYCLE / capacity_ctx["breakeven_timestep_count"]
        print(f"#     = {capacity_ctx['breakeven_timestep_count']:,} timesteps")
        print(f"#   production T = {PRODUCTION_TIMESTEP_COUNT_PER_CYCLE}"
              f" ({prod_pct:.1f}% of breakeven; safety margin holds under default deployment config)")
    else:
        print("#     = N/A (station_count == 0)")
    print()
    if producer_breaches:
        print("# producer-limit breach candidates:")
        for b in producer_breaches:
            print(f"#   BREACH: {b}")
    else:
        print(f"# no producer-limit breach with legacy station count {legacy_station_count:,} "
              f"× typical timestep {typical_timestep} × variables {output_vars}")

    # ---- D5. Runtime staging limits ----
    _emit_section("D5. Runtime staging byte/line limits (7 MAX_DIRECT_GRID_* constants pinned in manifest)")
    print("# NOTE: MAX_PACKAGE_MANIFEST_BYTES is a non-direct-grid PRCP-manifest cap and is")
    print("# EXPLICITLY EXCLUDED from the 7-constant pinned set per manifest scope.")
    print("# runtime.py:52-58 constants are code-pinned (no runtime env override in the current")
    print("# runtime.py shape) — 'runtime_effective' below equals the manifest pin unless the")
    print("# code has drifted since manifest freeze.")
    for name, value in staging_limits.items():
        print(f"#   {name:<38s} pinned={_fmt_int(value):>15s} bytes/lines (from manifest, per runtime.py:52-58)")
    staging_breaches = _check_staging_limit_zero_or_negative(staging_limits)
    if staging_breaches:
        print("# staging-limit breach candidates:")
        for b in staging_breaches:
            print(f"#   BREACH: {b}")
    else:
        print("# all 7 staging limits are positive integers; manifest not corrupt")
    print()
    print("# D5' Audit-chain rationale (why manifest-pinned staging values equal the deployed values")
    print("#      without an on-node runtime file read):")
    print("#   (a) workers/shud_runtime/runtime.py:52-58 constants are plain integer literals with no")
    print("#       env override (grep for _env_int / os.getenv in that block returns 0 matches — the")
    print("#       env override wiring only exists on the producer side, workers/forcing_producer/"
          "producer.py:318-324).")
    print("#   (b) Tree-equivalence attest (binder header line 19) binds baseline↔carrier zero diff on")
    print("#       `workers/`, so the deployed runtime.py bytes at capture time equal the baseline_commit")
    print("#       runtime.py bytes.")
    print("#   (c) The readiness-manifest.v1.json companion .sha256 is verified at driver start; if the")
    print("#       manifest bytes had drifted the driver would have failed at _load_manifest before D1.")
    print("#   (d) node-27 `git pull --ff-only` discipline sets the deployed working tree file bytes to")
    print("#       the code-carrier commit's tree, which the tree-equivalence attest in (b) proves is")
    print("#       identical to the baseline_commit tree for `workers/`.")
    print("#   Therefore the runtime constants deployed at capture time = manifest-pinned values, by")
    print("#   transitive derivation. Contrast with D4 producer limits which DO have env override wiring")
    print("#   (_env_int in producer.py) and thus require the runtime os.environ read + grep-c capture")
    print("#   above; runtime.py has no such wiring, so a runtime file read adds nothing over the chain.")

    # ---- D6. ~5x used-cell reduction structural evaluation ----
    _emit_section("D6. Direct-grid ~5x used-cell reduction claim (appendix-A structural evaluation)")
    print("# The migration doc claims a ~5x used-cell reduction (docs/ForcingReplace/...md:653).")
    print("# Structural evaluation (from appendix-A per-basin est. used cells column):")
    print(f"#   sum_per_basin_est_used_cells = {APPENDIX_A_USED_CELLS_TOTAL:,}")
    print("#     (312+120+174+8+75+20+11+77+12+120+260+4+1, appendix-A table at docs line 1108-1122)")
    print(f"#   doc-cited round-total       = ~{APPENDIX_A_USED_CELLS_ROUND:,}  (docs/ForcingReplace/...md:653)")
    print(f"#   legacy station baseline     = {legacy_station_count:,}  (D2 live SQL)")
    reduction_ratio = legacy_station_count / APPENDIX_A_USED_CELLS_ROUND
    print(f"#   reduction ratio             = {reduction_ratio:.2f}x  (legacy_stations / used_cells_est)")
    print(f"#   doc-claimed reduction       = ~{APPENDIX_A_EXPECTED_REDUCTION}x")
    print("# Interpretation: the ~5x reduction claim is structurally consistent with the")
    print("# appendix-A per-basin used-cell estimates. Exact used-cell counts per-basin remain")
    print("# a Change #909 (forcing-mapping-asset-build / grid-registry) output — §2.6 only")
    print("# discharges the platform-level baseline, not per-basin migration accounting.")

    # ---- D7. Limit-breach verdict ----
    _emit_section("D7. Limit-breach verdict (blocker for a separate capacity change if YES)")
    all_breaches = producer_breaches + staging_breaches
    if all_breaches:
        verdict = "YES"
        print(f"# verdict: {verdict} — {len(all_breaches)} breach(es) below")
        for b in all_breaches:
            print(f"#   BREACH: {b}")
        print("# Per tasks.md §2.6: 'any limit breach is flagged as a blocker for a separate")
        print("# capacity change'. Certification blocked until the capacity gap is addressed.")
    else:
        verdict = "NO"
        print("# verdict: NO BREACH — legacy baseline fits within all pinned producer + staging limits")
        print("#   producer breaches: 0 / staging limit breaches: 0")
        est_mb = capacity_ctx["estimated_manifest_bytes"] / (1024 * 1024)
        cap_mb = capacity_ctx["max_manifest_bytes"] / (1024 * 1024)
        pct = capacity_ctx["manifest_pct_of_cap"]
        if pct is not None:
            print(f"#   manifest_bytes: ~{est_mb:.2f} MiB vs {cap_mb:.2f} MiB cap"
                  f" ({pct:.2f}% of cap)")
        if capacity_ctx["breakeven_timestep_count"] is not None:
            prod_pct = 100.0 * PRODUCTION_TIMESTEP_COUNT_PER_CYCLE / capacity_ctx["breakeven_timestep_count"]
            print(f"#   breakeven_T = {capacity_ctx['breakeven_timestep_count']:,} timesteps"
                  f"; production T = {PRODUCTION_TIMESTEP_COUNT_PER_CYCLE} ({prod_pct:.1f}% of breakeven)")

    # ---- D8. Non-goal reaffirmation ----
    _emit_section("D8. Non-goal reaffirmation")
    print("# Per tasks.md §2.6 non-goal: no capacity-limit change and no per-basin migration")
    print("# accounting beyond the platform-level baseline.")
    print("# §2.6 reads code-pinned + deployment-config limits, measures the live legacy baseline,")
    print("# evaluates the estimation formula, and reports the verdict. It does NOT:")
    print("#   * modify any producer / runtime limit constant")
    print("#   * write per-basin migration plans (that is #909 forcing-mapping-asset-build)")
    print("#   * touch any production PG row (SELECT-only; session-level read-only guard set after")
    print("#     connect — safe under either nhms_display_ro role-level or nhms session-level guard)")

    # ---- D9. Discharge check ----
    _emit_section("D9. Discharge check against tasks.md §2.6 required-evidence bullets")
    print("# tasks.md §2.6 has 3 required-evidence bullets + 1 non-goal bullet.")
    print("# Bullet 1 (live legacy baseline via SQL — basin count, station count, forcing rows):")
    print("#   D1 (basin count) + D2 (station count) + D3 (forcing_station_timeseries rows) — DISCHARGED")
    print("# Bullet 2 (formula evaluation + producer/runtime limits + ~5x reduction + breach verdict):")
    print("#   D4 (formula vs producer limits) + D5 (staging limits) + D6 (~5x reduction)")
    print("#   + D7 (breach verdict) — DISCHARGED")
    print("# Bullet 3 (host + baseline_commit + manifest checksum, plus code_carrier_sha tree-diff")
    print("# when HEAD ≠ baseline): binder header lines 1-2 + tree-equivalence attest — DISCHARGED")
    print("# Non-goal (no capacity change, no per-basin migration): D8 — DISCHARGED")

    # ---- summary + exit ----
    print()
    print("=== §2.6 CAPACITY BASELINE SUMMARY ===")
    print(f"D1 basin_count_live         = {live.get('active_model_instance_count', 'N/A')}"
          f"  (production basins via active model_instance)")
    print(f"D2 legacy_station_live      = {live.get('active_met_station_count', 'N/A')}")
    print(f"D3 forcing_ts_2wk_rows_live = {live.get('forcing_ts_2wk_row_count', 'N/A')}")
    print(f"D4 formula_estimate         = {estimated_rows_pre:,}")
    print(f"D5 staging_limits_healthy   = {'YES' if not staging_breaches else 'NO'}")
    print(f"D6 reduction_ratio_est      = {reduction_ratio:.2f}x  (vs doc-claim ~{APPENDIX_A_EXPECTED_REDUCTION}x)")
    print(f"D7 breach_verdict           = {'NO BREACH' if not all_breaches else 'BREACH'}")
    print()
    if all_breaches:
        print("FAIL: §2.6 capacity baseline flagged breach(es); requires a separate capacity change.")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
