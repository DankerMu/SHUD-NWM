#!/usr/bin/env python3
"""M24 §4.2 live proof: FileSchedulerLease heartbeat + reclaim on real NFS.

Two independent processes share a lock on /scratch (NFS). Proves:
  P1  A holds + heartbeats across 3x TTL; a contender B trying every 0.5s
      NEVER acquires (no reclaim of a live holder => no double-submit).
  P2  A holder C that dies (pid gone) without releasing is reclaimed by B
      after TTL (owner-liveness reconcile => no permanent deadlock).

Emits a JSON receipt and exits non-zero on any failed assertion.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from services.orchestrator.scheduler import FileSchedulerLease, _LeaseHeartbeat

TTL = 2  # seconds; pass duration (6s) deliberately exceeds TTL


def _fs_type(path: Path) -> str:
    try:
        out = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"unknown({exc})"


def _holder_heartbeats(lock_path: str, workspace_root: str, hold_seconds: float, result_path: str) -> None:
    """Process A: acquire, heartbeat across > TTL, then release."""
    lease = FileSchedulerLease(Path(lock_path), ttl_seconds=TTL, workspace_root=Path(workspace_root))
    res = lease.acquire(pass_id="holderA", started_at=datetime.now(UTC))
    hb = _LeaseHeartbeat(lease, "holderA", max(0.001, TTL / 3))
    hb.start()
    time.sleep(hold_seconds)
    hb.stop()
    lease.release(pass_id="holderA")
    Path(result_path).write_text(
        json.dumps({"acquired": bool(res.get("acquired")), "lost": hb.lost, "pid": os.getpid()}),
        encoding="utf-8",
    )


def _holder_dies(lock_path: str, workspace_root: str, result_path: str) -> None:
    """Process C: acquire then die immediately WITHOUT release or heartbeat."""
    lease = FileSchedulerLease(Path(lock_path), ttl_seconds=TTL, workspace_root=Path(workspace_root))
    res = lease.acquire(pass_id="holderC", started_at=datetime.now(UTC))
    Path(result_path).write_text(
        json.dumps({"acquired": bool(res.get("acquired")), "pid": os.getpid()}), encoding="utf-8"
    )
    os._exit(0)  # hard exit: no release, no cleanup -> stale lock left behind


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/scratch/frd_muziyao/nhms-lease-proof")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = str(root / "scheduler.lock")
    a_result = str(root / "a_result.json")
    c_result = str(root / "c_result.json")
    for p in (lock_path, f"{lock_path}.guard", a_result, c_result):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    ctx = mp.get_context("fork")
    receipt: dict = {
        "proof": "m24-lease-nfs",
        "fs_type": _fs_type(root),
        "lock_path": lock_path,
        "ttl_seconds": TTL,
        "node": os.uname().nodename,
    }

    # ---- Phase 1: heartbeat holds the lease across > TTL; contender never steals ----
    hold_seconds = 3 * TTL  # 6s, three TTLs
    a = ctx.Process(target=_holder_heartbeats, args=(lock_path, str(root), hold_seconds, a_result))
    a.start()
    time.sleep(0.6)  # let A acquire first
    b_attempts: list[dict] = []
    deadline = time.time() + (hold_seconds - 1.0)
    while time.time() < deadline:
        lease_b = FileSchedulerLease(Path(lock_path), ttl_seconds=TTL, workspace_root=root)
        r = lease_b.acquire(pass_id="contenderB", started_at=datetime.now(UTC))
        if r.get("acquired"):
            lease_b.release(pass_id="contenderB")  # should never happen
        b_attempts.append({"t": round(time.time() - (deadline - (hold_seconds - 1.0)), 2),
                           "acquired": bool(r.get("acquired"))})
        time.sleep(0.5)
    # read live heartbeat_seq while A may still hold (just before join)
    seq_during = None
    try:
        seq_during = json.loads(Path(lock_path).read_text(encoding="utf-8")).get("heartbeat_seq")
    except Exception:  # noqa: BLE001
        pass
    a.join(timeout=10)
    a_res = json.loads(Path(a_result).read_text(encoding="utf-8"))
    b_acquired_count = sum(1 for x in b_attempts if x["acquired"])
    receipt["phase1_heartbeat_holds"] = {
        "holder_acquired": a_res["acquired"],
        "holder_heartbeat_lost": a_res["lost"],
        "contender_attempts": len(b_attempts),
        "contender_acquired_count": b_acquired_count,
        "heartbeat_seq_observed": seq_during,
        "pass_seconds_vs_ttl": f"{hold_seconds}s > {TTL}s TTL",
    }

    # ---- Phase 2: dead holder is reclaimed (liveness reconcile, no deadlock) ----
    c = ctx.Process(target=_holder_dies, args=(lock_path, str(root), c_result))
    c.start()
    c.join(timeout=10)  # reap C so its pid is freed (probe -> dead)
    c_res = json.loads(Path(c_result).read_text(encoding="utf-8"))
    time.sleep(TTL + 1.5)  # let the dead holder's lock age past TTL
    lease_b2 = FileSchedulerLease(Path(lock_path), ttl_seconds=TTL, workspace_root=root)
    r2 = lease_b2.acquire(pass_id="contenderB2", started_at=datetime.now(UTC))
    reclaimed = bool(r2.get("acquired"))
    if reclaimed:
        lease_b2.release(pass_id="contenderB2")
    receipt["phase2_dead_holder_reclaim"] = {
        "dead_holder_acquired": c_res["acquired"],
        "dead_holder_pid": c_res["pid"],
        "waited_seconds": TTL + 1.5,
        "contender_reclaimed": reclaimed,
    }

    # ---- Verdict ----
    p1_ok = (a_res["acquired"] and b_acquired_count == 0 and (seq_during or 0) >= 1)
    p2_ok = (c_res["acquired"] and reclaimed)
    receipt["verdict"] = "PASS" if (p1_ok and p2_ok) else "FAIL"
    receipt["assertions"] = {
        "p1_no_reclaim_of_live_holder": p1_ok,
        "p2_dead_holder_reclaimed": p2_ok,
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
