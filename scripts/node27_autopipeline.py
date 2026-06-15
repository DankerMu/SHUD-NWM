#!/usr/bin/env python3
"""Basin-agnostic autopipeline: discover object-store runs, seed missing basin
registries, then register -> mirror -> parse -> refresh-coverage every run.

Generalises the earlier qhh-hardcoded ingest into a basin-agnostic pipeline so
node-27 can self-serve any basin/run that appears under ``<OBJECT_STORE_ROOT>/runs/``:

  1. Scan ``runs/`` and parse ``fcst_{gfs,ifs}_<cycle10>_basins_<basin>_shud``
     into (basin, source, cycle). Non-matching dirs are ignored.
  2. For every distinct basin whose registry is not yet seeded
     (``core.basin`` has no ``basins_<basin>`` row), run the *generic* registry
     seed via the model-registry CLI -- discover-basins -> publish-basins ->
     import-basins-registry -> activate model_instance. Identity (model_id,
     package version) is read from that basin's first run manifest, never
     hard-coded.
  3. For every run, run the per-run pipeline (each step a subprocess so one
     run's failure never aborts the batch):
       register -> scripts/node27_ingest_run.py
       mirror   -> scripts/node27_mirror_forcing.py   (node-22 read-only)
       parse    -> workers.output_parser.cli parse
       refresh  -> scripts/node27_refresh_coverage.py  (Mission-4; skipped if absent)

Idempotent and failure-isolated. Re-running only does outstanding work:
already-seeded basins and already-parsed runs are detected and skipped.
Prints a JSON summary; exit 0 unless a run hard-failed.

Object-store / DB env (same contract as the per-run scripts)::

    OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
    OBJECT_STORE_PREFIX=s3://nhms
    DATABASE_URL=postgresql://nhms:nhms_dev@127.0.0.1:55432/nhms
    BASINS_ROOT=/home/ghdc/nwm/Basins        # geometry source for registry seed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
# fcst_<source>_<cycle10>_basins_<basin>_shud  (basin may contain underscores).
RUN_RE = re.compile(r"^fcst_(?P<source>gfs|ifs)_(?P<cycle>\d{10})_basins_(?P<basin>.+)_shud$")

DEFAULT_BASINS_ROOT = "/home/ghdc/nwm/Basins"
# Auth for import-basins-registry (models.switch_version => model_admin|sys_admin).
SEED_AUTH_ACTOR = os.environ.get("AUTOPIPE_AUTH_ACTOR", "node27-autopipe")
SEED_AUTH_ROLE = os.environ.get("AUTOPIPE_AUTH_ROLE", "model_admin")
# Scratch root for per-basin seed copies (the basin geometry subtree + the
# publish obj-store). Defaults to the system temp dir, but on a host whose / is
# small set AUTOPIPE_WORK_ROOT to a path on the big volume (node-27: / is 98G,
# /home is 1.7T) so a multi-GB basin copy never fills /. Scratch is removed
# after every seed regardless (see _seed_basin).
WORK_ROOT = os.environ.get("AUTOPIPE_WORK_ROOT") or tempfile.gettempdir()


# --------------------------------------------------------------------------- #
# run discovery
# --------------------------------------------------------------------------- #
def _discover_runs(object_store_root: Path, sources: tuple[str, ...]) -> list[dict[str, str]]:
    runs_dir = object_store_root / "runs"
    out: list[dict[str, str]] = []
    if not runs_dir.is_dir():
        return out
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        m = RUN_RE.match(entry.name)
        if not m or m.group("source") not in sources:
            continue
        out.append(
            {
                "run_id": entry.name,
                "source": m.group("source"),
                "cycle": m.group("cycle"),
                "basin": m.group("basin"),
            }
        )
    return out


def _read_manifest(object_store_root: Path, run_id: str) -> dict[str, Any]:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _basin_identity(object_store_root: Path, run_id: str) -> dict[str, str]:
    """Derive (model_id, basin_id, package_version) from a run manifest.

    package_version is the second-to-last path segment of model_package_uri,
    e.g. ``s3://nhms/models/basins_heihe_shud/vbasins-heihe-production/package/``
    -> ``vbasins-heihe-production`` (matches the qhh DB row exactly).
    """
    manifest = _read_manifest(object_store_root, run_id)
    identity = manifest.get("identity") or {}
    model = manifest.get("model") or {}
    model_id = identity.get("model_id") or model.get("model_id")
    basin_id = identity.get("basin_id") or model.get("basin_id")
    package_uri = identity.get("model_package_uri") or model.get("model_package_uri")
    if not (model_id and basin_id and package_uri):
        raise ValueError(f"manifest for {run_id} missing model_id/basin_id/model_package_uri")
    segments = [seg for seg in str(package_uri).rstrip("/").split("/") if seg]
    # .../models/<model_id>/<version>/package  -> version is segment before 'package'
    version = segments[-2] if segments[-1] == "package" else segments[-1]
    return {
        "model_id": str(model_id),
        "basin_id": str(basin_id),
        "package_version": version,
        "package_uri": str(package_uri),
    }


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _basin_seeded(database_url: str, basin_id: str) -> bool:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM core.basin WHERE basin_id = %s", (basin_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _already_ingested_runs(database_url: str, run_ids: list[str]) -> set[str]:
    """Return the subset of run_ids already fully ingested: hydro_run at a
    parser-advanced status AND carrying river_timeseries rows. Lets the cron
    re-scan cheaply -- finished runs are skipped instead of re-mirroring their
    (large) per-cycle forcing every tick."""
    if not run_ids:
        return set()
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.run_id
                FROM hydro.hydro_run h
                WHERE h.run_id = ANY(%s)
                  AND h.status IN ('parsed', 'frequency_done', 'published')
                  AND EXISTS (
                      SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_id = h.run_id
                  )
                """,
                (run_ids,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _activate_model(database_url: str, model_id: str) -> int:
    """Mark the model_instance active so the display station-coverage CTE
    (``met.interp_weight`` join requires ``model_instance.active_flag = true``)
    can see it. Generic import leaves the instance inactive."""
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE core.model_instance
                    SET active_flag = true, lifecycle_state = 'active'
                    WHERE model_id = %s
                    """,
                    (model_id,),
                )
                return cur.rowcount
    finally:
        conn.close()


def _backfill_output_geometry(database_url: str, river_network_version_id: str) -> int:
    """Stitch display geometry onto the NULL-geom ``.sp.riv`` output reaches the
    generic import seeds. The import deliberately leaves those reaches NULL
    (display geometry is a separate concern), so without this the national /
    per-run MVT JOINs the reach rows but renders nothing -- the basin's river
    segments are invisible and unclickable on the live map (the heihe
    regression). Reuses the single shared SQL; only_missing keeps it idempotent
    and record_geometry_source=False leaves properties_json untouched so a
    re-seed's import digest stays stable."""
    from workers.model_registry.basins_registry_import import (
        _backfill_output_segment_geometry,
    )

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                return _backfill_output_segment_geometry(
                    cur,
                    river_network_version_id,
                    only_missing=True,
                    record_geometry_source=False,
                )
    finally:
        conn.close()


def _publish_display_runs(database_url: str) -> int:
    """Advance fully-ingested display runs from 'parsed' to 'published'.

    ``/api/v1/layers`` (``latest_frequency_ready_run``) only surfaces hydro runs
    whose status is in ('frequency_done', 'published'); a display node never
    computes flood frequency, so without this the catalog stays empty and the
    q_down overlay never registers. 'published' (display products available) is
    the honest terminal state here -- flood/warning availability is still
    annotated separately from the actual ``flood.return_period_result``, so this
    does not fabricate return-period products. Idempotent (published runs and
    runs without timeseries are left untouched), matching the
    ``_already_ingested_runs`` completeness predicate."""
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hydro.hydro_run h
                    SET status = 'published', updated_at = now()
                    WHERE h.status = 'parsed'
                      AND EXISTS (
                          SELECT 1 FROM hydro.river_timeseries rt WHERE rt.run_id = h.run_id
                      )
                    """
                )
                return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# subprocess plumbing
# --------------------------------------------------------------------------- #
def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# generic registry seed
# --------------------------------------------------------------------------- #
def _isolate_basin_root(basins_root: Path, basin: str) -> Path:
    """Copy a single basin subtree into a private root and strip Synology
    ``@eaDir`` sidecars, so discover-basins stays under its 2048-entry budget
    (scanning the whole multi-basin Basins root blows the limit)."""
    only_root = Path(WORK_ROOT) / f"{basin}-only-root"
    dst = only_root / basin
    if dst.exists():
        shutil.rmtree(only_root, ignore_errors=True)
    only_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(basins_root / basin, dst, symlinks=False)
    for ea in dst.rglob("@eaDir"):
        shutil.rmtree(ea, ignore_errors=True)
    return only_root


def _seed_basin(
    *,
    basin: str,
    identity: dict[str, str],
    database_url: str,
    basins_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    model_id = identity["model_id"]
    work = Path(WORK_ROOT) / f"{basin}-seed"
    work.mkdir(parents=True, exist_ok=True)
    inv = work / "inventory.json"
    pkg = work / "package-manifest.json"
    obj_store = Path(WORK_ROOT) / f"{basin}-obj-store"  # writable; publish writes models/ here

    only_root = _isolate_basin_root(basins_root, basin)

    cli = [PY, "-m", "workers.model_registry.cli"]
    try:
        rc, out, err = _run(
            cli + ["discover-basins", "--basins-root", str(only_root), "--output", str(inv)], env
        )
        if rc != 0:
            return {"basin": basin, "outcome": "seed_failed", "stage": "discover", "error": (err or out)[-600:]}

        pub_env = dict(env)
        pub_env["OBJECT_STORE_ROOT"] = str(obj_store)
        rc, out, err = _run(
            cli
            + [
                "publish-basins",
                "--inventory", str(inv),
                "--model-id", model_id,
                "--version", identity["package_version"],
                "--output", str(pkg),
            ],
            pub_env,
        )
        if rc != 0:
            return {"basin": basin, "outcome": "seed_failed", "stage": "publish", "error": (err or out)[-600:]}

        rc, out, err = _run(
            cli
            + [
                "import-basins-registry",
                "--inventory", str(inv),
                "--package-manifest", str(pkg),
                "--database-url", database_url,
                "--auth-actor-id", SEED_AUTH_ACTOR,
                "--auth-role", SEED_AUTH_ROLE,
            ],
            env,
        )
        if rc != 0:
            return {"basin": basin, "outcome": "seed_failed", "stage": "import", "error": (err or out)[-600:]}
        import_report = _last_json(out) or {}

        rnv_id = import_report.get("river_network_version_id")
        geom_rows = _backfill_output_geometry(database_url, rnv_id) if rnv_id else 0
        activated = _activate_model(database_url, model_id)
        return {
            "basin": basin,
            "outcome": "seeded",
            "model_id": model_id,
            "package_version": identity["package_version"],
            "import_status": import_report.get("status"),
            "segment_count": import_report.get("segment_count"),
            "output_segment_count": import_report.get("output_segment_count"),
            "output_geometry_backfilled": geom_rows,
            "model_activated_rows": activated,
        }
    finally:
        # Seed scratch (multi-GB basin copy + publish obj-store) is only needed
        # during the CLI calls above; always remove it so it never accumulates.
        for scratch in (only_root, work, obj_store):
            shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# per-run pipeline
# --------------------------------------------------------------------------- #
def _refresh_coverage_script() -> Path | None:
    path = REPO_ROOT / "scripts" / "node27_refresh_coverage.py"
    return path if path.is_file() else None


def _process_run(run_id: str, env: dict[str, str]) -> dict[str, Any]:
    register = [PY, str(REPO_ROOT / "scripts" / "node27_ingest_run.py"), "--run-id", run_id]
    rc, out, err = _run(register, env)
    if rc != 0:
        return {"run_id": run_id, "outcome": "failed", "stage": "register", "rc": rc, "error": (err or out)[-500:]}

    mirror = [PY, str(REPO_ROOT / "scripts" / "node27_mirror_forcing.py"), "--run-id", run_id]
    rc, out, err = _run(mirror, env)
    if rc == 2:
        payload = _last_json(out) or {}
        return {
            "run_id": run_id,
            "outcome": "skipped",
            "stage": "mirror",
            "reason": payload.get("reason", "FORCING_NOT_ON_NODE22"),
        }
    if rc != 0:
        return {"run_id": run_id, "outcome": "failed", "stage": "mirror", "rc": rc, "error": (err or out)[-500:]}
    mirror_payload = _last_json(out) or {}

    parse = [PY, "-m", "workers.output_parser.cli", "parse", "--run-id", run_id]
    rc, out, err = _run(parse, env)
    if rc != 0:
        return {"run_id": run_id, "outcome": "failed", "stage": "parse", "rc": rc, "error": (err or out)[-500:]}
    parse_payload = _last_json(out) or {}

    refresh_status = "skipped_no_script"
    refresh_script = _refresh_coverage_script()
    if refresh_script is not None:
        rc, out, err = _run([PY, str(refresh_script), "--run-id", run_id], env)
        if rc != 0:
            # Coverage refresh is Mission-4 territory; a failure here does not
            # invalidate the ingest -- record it but keep the run as ingested.
            refresh_status = f"refresh_failed_rc{rc}"
        else:
            # rc=0 with refreshed=false means the run yielded no coverage row
            # (no displayable forcing/river data yet); latest-product still
            # resolves via the CTE fallback. Record honestly as "no_coverage_row".
            payload = _last_json(out) or {}
            refresh_status = "refreshed" if payload.get("refreshed") else "no_coverage_row"

    return {
        "run_id": run_id,
        "outcome": "ingested",
        "station_rows": (mirror_payload.get("station_timeseries") or {}).get("local_rows"),
        "river_rows": parse_payload.get("rows_written"),
        "parse_status": parse_payload.get("status"),
        "coverage_refresh": refresh_status,
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Basin-agnostic node-27 autopipeline.")
    parser.add_argument("--object-store-root", default=os.environ.get("OBJECT_STORE_ROOT"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--basins-root", default=os.environ.get("BASINS_ROOT", DEFAULT_BASINS_ROOT))
    parser.add_argument("--sources", default="gfs,ifs", help="Comma list of sources (default gfs,ifs).")
    parser.add_argument("--only-basin", default=None, help="Restrict to a single basin slug (e.g. heihe).")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N runs (smoke).")
    parser.add_argument("--seed-only", action="store_true", help="Only seed basin registries; skip run ingest.")
    parser.add_argument("--force", action="store_true", help="Re-ingest even already-parsed runs.")
    parser.add_argument("--progress", action="store_true", help="Per-step progress to stderr.")
    args = parser.parse_args(argv)

    if not args.object_store_root:
        parser.error("OBJECT_STORE_ROOT or --object-store-root is required.")
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required.")

    object_store_root = Path(args.object_store_root)
    basins_root = Path(args.basins_root)
    database_url = args.database_url
    sources = tuple(s.strip().lower() for s in args.sources.split(",") if s.strip())

    env = dict(os.environ)
    env.setdefault("OBJECT_STORE_ROOT", str(object_store_root))
    env["DATABASE_URL"] = database_url

    runs = _discover_runs(object_store_root, sources)
    if args.only_basin:
        runs = [r for r in runs if r["basin"] == args.only_basin]

    # ---- phase 1: seed any unseeded basin (identity from first run manifest) --
    seed_results: list[dict[str, Any]] = []
    basins = sorted({r["basin"] for r in runs})
    for basin in basins:
        first_run = next(r["run_id"] for r in runs if r["basin"] == basin)
        try:
            identity = _basin_identity(object_store_root, first_run)
        except Exception as exc:  # noqa: BLE001 - record + continue, isolate failure
            seed_results.append({"basin": basin, "outcome": "seed_failed", "stage": "identity", "error": str(exc)})
            continue
        if _basin_seeded(database_url, identity["basin_id"]):
            seed_results.append({"basin": basin, "outcome": "already_seeded", "basin_id": identity["basin_id"]})
            continue
        if args.progress:
            print(f"[seed] {basin} ({identity['model_id']} @ {identity['package_version']})",
                  file=sys.stderr, flush=True)
        result = _seed_basin(
            basin=basin, identity=identity, database_url=database_url, basins_root=basins_root, env=env
        )
        seed_results.append(result)
        if args.progress:
            print(f"[seed] {basin}: {result['outcome']}"
                  + (f" ({result.get('stage')})" if result["outcome"] == "seed_failed" else ""),
                  file=sys.stderr, flush=True)

    seed_failed = [s for s in seed_results if s["outcome"] == "seed_failed"]
    seeded_basins = {s["basin"] for s in seed_results if s["outcome"] in ("seeded", "already_seeded")}

    # ---- phase 2: per-run ingest (skip runs whose basin failed to seed) -------
    run_results: list[dict[str, Any]] = []
    already_count = 0
    if not args.seed_only:
        runnable = [r for r in runs if r["basin"] in seeded_basins]
        done = set() if args.force else _already_ingested_runs(database_url, [r["run_id"] for r in runnable])
        already_count = len([r for r in runnable if r["run_id"] in done])
        pending = [r for r in runnable if r["run_id"] not in done]
        if args.limit is not None:
            pending = pending[: args.limit]
        for idx, run in enumerate(pending, start=1):
            result = _process_run(run["run_id"], env)
            run_results.append(result)
            if args.progress:
                tail = f" ({result.get('stage')})" if result["outcome"] != "ingested" else ""
                print(f"[{idx}/{len(pending)}] {run['run_id']}: {result['outcome']}{tail}",
                      file=sys.stderr, flush=True)

    # ---- phase 3: advance fully-ingested runs to 'published' so the layer ----
    # catalog (discharge / q_down overlay) actually surfaces them. Idempotent;
    # also back-fills runs parsed by earlier ticks before this step existed.
    published_count = 0
    if not args.seed_only:
        published_count = _publish_display_runs(database_url)
        if args.progress:
            print(f"[publish] advanced {published_count} run(s) parsed -> published",
                  file=sys.stderr, flush=True)

    def by(outcome: str) -> list[dict[str, Any]]:
        return [r for r in run_results if r["outcome"] == outcome]

    summary = {
        "object_store_root": str(object_store_root),
        "basins_root": str(basins_root),
        "sources": list(sources),
        "discovered_runs": len(runs),
        "basins": basins,
        "seed": {
            "seeded": [s["basin"] for s in seed_results if s["outcome"] == "seeded"],
            "already_seeded": [s["basin"] for s in seed_results if s["outcome"] == "already_seeded"],
            "failed": [{"basin": s["basin"], "stage": s.get("stage"), "error": s.get("error")} for s in seed_failed],
            "details": seed_results,
        },
        "runs": {
            "already_ingested": already_count,
            "published": published_count,
            "ingested": len(by("ingested")),
            "skipped": len(by("skipped")),
            "failed": len(by("failed")),
            "ingested_by_source": {
                src: len([r for r in by("ingested") if r["run_id"].startswith(f"fcst_{src}_")]) for src in sources
            },
            "skipped_runs": [{"run_id": r["run_id"], "reason": r.get("reason")} for r in by("skipped")],
            "failed_runs": [
                {"run_id": r["run_id"], "stage": r.get("stage"), "error": r.get("error")} for r in by("failed")
            ],
        },
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if (not seed_failed and not by("failed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
