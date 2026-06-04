"""M24 §0 read-only baseline emitter.

Collects a point-in-time, read-only snapshot of the production environment and
writes it as a canonical M24 ``baseline`` receipt to
``artifacts/m24/<run_id>/baseline.json``.

The emitter only reads. It never mutates download/Slurm/SHUD/publish state. It
is the geological floor for every later M24 section (§1–§4).

Usage::

    uv run python scripts/m24_emit_baseline.py --run-id <id>

Collected stages:

- ``db_identity``     redacted DB host/db/user (never the password).
- ``active_models``   ``core.model_instance`` row count.
- ``hydro_run_gfs``   ``hydro.hydro_run`` status counts for GFS / 2026060400.
- ``hydro_run_ifs``   ``hydro.hydro_run`` status counts for IFS / 2026060400.
- ``state_snapshot``  ``hydro.state_snapshot`` row count (expected 0).
- ``gateway_health``  ``${SLURM_GATEWAY_URL}/api/v1/slurm/health`` result.
- ``provenance_claim`` records that live QHH ran via ``run_qhh_cycle.sh`` while
  the generic scheduler has never run live (m20 0/33).

DB or gateway unreachable marks that stage BLOCKED/error but the top-level
receipt stays PASS (baseline is a best-effort snapshot). Only a wholly missing
``DATABASE_URL`` flips the whole receipt to BLOCKED + ``dependency_blocker``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Allow running as a plain script (repo root on sys.path).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.m24_live.receipt import (  # noqa: E402
    CONTRACT_ID,
    SCHEMA_VERSION,
    validate_receipt,
    write_receipt,
)

CYCLE_TIME = "2026-06-04T00:00:00Z"
CYCLE_LABEL = "2026060400"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8081"
DB_CONNECT_TIMEOUT_SECONDS = 5
GATEWAY_TIMEOUT_SECONDS = 5

PROVENANCE_CLAIM = (
    "Live QHH ran via run_qhh_cycle.sh; the generic scheduler has never run "
    "live (m20 0/33)."
)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def _redact_dsn(database_url: str | None) -> dict[str, Any]:
    """Parse a DSN into host/db/user, never returning the password."""

    if not database_url:
        return {"host": None, "database": None, "user": None}
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return {"host": None, "database": None, "user": None}
    database = parsed.path.lstrip("/") or None
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "database": database,
        "user": parsed.username,
    }


def _connect(database_url: str):
    import psycopg2

    return psycopg2.connect(database_url, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)


def _collect_active_models(cursor) -> dict[str, Any]:
    cursor.execute("SELECT count(*) FROM core.model_instance")
    return {"status": "PASS", "counts": {"active_model_count": int(cursor.fetchone()[0])}}


def _collect_hydro_run_counts(cursor, source: str) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT status, count(*)
        FROM hydro.hydro_run
        WHERE LOWER(source_id) = LOWER(%s)
          AND cycle_time = %s::timestamptz
        GROUP BY status
        ORDER BY status
        """,
        (source, CYCLE_TIME),
    )
    by_status = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
    counts: dict[str, Any] = {"by_status": by_status, "total": sum(by_status.values())}
    return {"status": "PASS", "counts": counts}


def _collect_state_snapshot(cursor) -> dict[str, Any]:
    cursor.execute("SELECT count(*) FROM hydro.state_snapshot")
    return {"status": "PASS", "counts": {"state_snapshot_count": int(cursor.fetchone()[0])}}


def _collect_db_stages(database_url: str, *, connect=_connect) -> dict[str, dict[str, Any]]:
    """Run every read-only DB probe; per-stage BLOCKED on its own failure."""

    stages: dict[str, dict[str, Any]] = {}
    try:
        connection = connect(database_url)
    except Exception as error:  # noqa: BLE001 - record, do not crash baseline
        reason = _safe_error(error)
        for name in ("active_models", "hydro_run_gfs", "hydro_run_ifs", "state_snapshot"):
            stages[name] = {"status": "BLOCKED", "counts": {"error": reason}}
        return stages
    try:
        probes = {
            "active_models": lambda cur: _collect_active_models(cur),
            "hydro_run_gfs": lambda cur: _collect_hydro_run_counts(cur, "gfs"),
            "hydro_run_ifs": lambda cur: _collect_hydro_run_counts(cur, "ifs"),
            "state_snapshot": lambda cur: _collect_state_snapshot(cur),
        }
        for name, probe in probes.items():
            try:
                with connection.cursor() as cursor:
                    stages[name] = probe(cursor)
                connection.rollback()
            except Exception as error:  # noqa: BLE001
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001
                    pass
                stages[name] = {"status": "BLOCKED", "counts": {"error": _safe_error(error)}}
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
    return stages


def _collect_gateway_health(gateway_url: str, *, http_get=None) -> dict[str, Any]:
    url = gateway_url.rstrip("/") + "/api/v1/slurm/health"
    getter = http_get if http_get is not None else _default_http_get
    try:
        status_code, body = getter(url)
    except Exception as error:  # noqa: BLE001
        return {"status": "BLOCKED", "counts": {"error": _safe_error(error), "url": url}}
    ok = isinstance(status_code, int) and 200 <= status_code < 300
    return {
        "status": "PASS" if ok else "BLOCKED",
        "counts": {"status_code": status_code, "body": body, "url": url},
    }


def _default_http_get(url: str) -> tuple[int, Any]:
    import httpx

    response = httpx.get(url, timeout=GATEWAY_TIMEOUT_SECONDS)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body


def _safe_error(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:500]


def build_baseline_receipt(
    run_id: str,
    *,
    database_url: str | None,
    gateway_url: str,
    connect=_connect,
    http_get=None,
    now=_utc_now_iso,
) -> dict[str, Any]:
    """Assemble (and validate) the baseline receipt without touching disk."""

    node = platform.node() or "unknown-node"
    command = "uv run python scripts/m24_emit_baseline.py --run-id " + run_id
    redaction_db = _redact_dsn(database_url)

    stages: list[dict[str, Any]] = []
    top_status = "PASS"
    dependency_blocker: str | None = None

    if not database_url:
        top_status = "BLOCKED"
        dependency_blocker = "DATABASE_URL is not set; baseline cannot read production DB."
        for name in ("active_models", "hydro_run_gfs", "hydro_run_ifs", "state_snapshot"):
            stages.append({"stage": name, "status": "BLOCKED", "counts": {"error": "DATABASE_URL missing"}})
    else:
        db_stages = _collect_db_stages(database_url, connect=connect)
        for name in ("active_models", "hydro_run_gfs", "hydro_run_ifs", "state_snapshot"):
            entry = db_stages[name]
            stages.append({"stage": name, "status": entry["status"], "counts": entry["counts"]})

    db_identity_stage = {
        "stage": "db_identity",
        "status": "PASS" if database_url else "BLOCKED",
        "counts": {"db_dsn_redacted": redaction_db},
    }
    stages.insert(0, db_identity_stage)

    gateway_stage = _collect_gateway_health(gateway_url, http_get=http_get)
    stages.append({"stage": "gateway_health", **gateway_stage})

    stages.append(
        {
            "stage": "provenance_claim",
            "status": "PASS",
            "counts": {
                "claim": PROVENANCE_CLAIM,
                "live_qhh_via": "run_qhh_cycle.sh",
                "generic_scheduler_live_runs": 0,
                "m20_live": "0/33",
            },
        }
    )

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "section": "baseline",
        "run_id": run_id,
        "node": node,
        "command": command,
        "timestamp": now(),
        "status": top_status,
        "execution_mode": "deterministic",
        "live_proof_accepted": False,
        "dependency_blocker": dependency_blocker,
        "redaction": {
            "db_dsn_redacted": True,
            "bounds": {"cycle_time": CYCLE_TIME, "cycle_label": CYCLE_LABEL},
        },
        "artifact_refs": [],
        "identity": {
            "source": "multi",
            "cycle_time": CYCLE_TIME,
            "model_id": None,
            "basin_id": None,
            "basin_version_id": None,
            "river_network_version_id": None,
        },
        "stages": stages,
        "slurm": {
            "job_id": None,
            "array_task_id": None,
            "original_task_id": None,
            "accounting": None,
            "log_uri": None,
        },
        "published_uri": None,
        "warm_start_quality": None,
        "notes": {
            "provenance_claim": PROVENANCE_CLAIM,
            "gateway_url": gateway_url.rstrip("/") + "/api/v1/slurm/health",
        },
    }

    validate_receipt(receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit the M24 read-only baseline receipt.")
    parser.add_argument("--run-id", required=True, help="Receipt run identifier.")
    parser.add_argument(
        "--root",
        default="artifacts/m24",
        help="Receipt root directory (default: artifacts/m24).",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("SLURM_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        help="Slurm gateway base URL (default: $SLURM_GATEWAY_URL or http://127.0.0.1:8081).",
    )
    args = parser.parse_args(argv)

    database_url = os.getenv("DATABASE_URL")
    receipt = build_baseline_receipt(
        args.run_id,
        database_url=database_url,
        gateway_url=args.gateway_url,
    )
    path = write_receipt(receipt, root=args.root)
    print(f"baseline receipt written: {path} (status={receipt['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
