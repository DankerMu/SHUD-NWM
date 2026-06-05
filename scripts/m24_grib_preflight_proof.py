#!/usr/bin/env python3
"""M24 §4.5 live proof: GRIB-env preflight fails loud, passes on healthy root.

Exercises _slurm_grib_env_check on node-22 with these env states:
  A1 root unset + no assertion                  -> no blocker (no-fence default)
  A2 root unset + NHMS_GRIB_SYSTEM_ECCODES=false -> GRIB_ENV_UNAVAILABLE (opt-in loud)
  B  root = the real shared conda env (bin+lib) -> no blocker (healthy)
  C  root = a non-existent path                 -> GRIB_ENV_ROOT_INVALID (loud)
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

from services.orchestrator.scheduler import _slurm_grib_env_check


def _check(env: dict) -> dict:
    for key in ("NHMS_GRIB_ENV_ROOT", "NHMS_GRIB_SYSTEM_ECCODES"):
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    config = SimpleNamespace(slurm_env={})
    check, blockers = _slurm_grib_env_check(config)
    return {"check": check, "blocker_codes": [b.get("code") for b in blockers]}


def main() -> int:
    real_root = sys.argv[1] if len(sys.argv) > 1 else "/scratch/frd_muziyao/nhms-grib"
    receipt = {
        "proof": "m24-grib-preflight",
        "node": os.uname().nodename,
        "real_grib_root": real_root,
        "real_root_bin_exists": os.path.isdir(os.path.join(real_root, "bin")),
        "real_root_lib_exists": os.path.isdir(os.path.join(real_root, "lib")),
        "cases": {
            "A1_unset_no_assertion_passes": _check({}),
            "A2_unset_nodes_lack_eccodes_blocks": _check(
                {"NHMS_GRIB_SYSTEM_ECCODES": "false"}
            ),
            "B_healthy_real_root": _check({"NHMS_GRIB_ENV_ROOT": real_root}),
            "C_nonexistent_root": _check({"NHMS_GRIB_ENV_ROOT": "/nonexistent/grib-env"}),
        },
    }
    a1_pass = receipt["cases"]["A1_unset_no_assertion_passes"]["blocker_codes"] == []
    a2_loud = (
        "GRIB_ENV_UNAVAILABLE"
        in receipt["cases"]["A2_unset_nodes_lack_eccodes_blocks"]["blocker_codes"]
    )
    b_pass = receipt["cases"]["B_healthy_real_root"]["blocker_codes"] == []
    c_loud = "GRIB_ENV_ROOT_INVALID" in receipt["cases"]["C_nonexistent_root"]["blocker_codes"]
    receipt["assertions"] = {
        "A1_unset_no_fence_passes": a1_pass,
        "A2_nodes_lack_eccodes_fails_loud": a2_loud,
        "B_healthy_passes": b_pass,
        "C_invalid_fails_loud": c_loud,
    }
    receipt["verdict"] = (
        "PASS" if (a1_pass and a2_loud and b_pass and c_loud) else "FAIL"
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
