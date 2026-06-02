from __future__ import annotations

import json
import os
from pathlib import Path

from workers.model_registry.qhh_production_bootstrap import (
    DEFAULT_QHH_MODEL_ID,
    DEFAULT_QHH_PROJECT_NAME,
    QhhProductionBootstrapError,
    seed_qhh_output_segments,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-smoke")).resolve()
MODEL_ID = os.getenv("QHH_MODEL_ID", DEFAULT_QHH_MODEL_ID)
PACKAGE_VERSION = os.getenv("QHH_PACKAGE_VERSION", "v0.0.1-qhh-smoke")
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", DEFAULT_QHH_PROJECT_NAME)


def main() -> int:
    riv_path = RUN_ROOT / "models" / MODEL_ID / PACKAGE_VERSION / "package" / f"{PROJECT_NAME}.sp.riv"
    try:
        result = seed_qhh_output_segments(
            database_url=os.environ["DATABASE_URL"],
            model_id=MODEL_ID,
            project_name=PROJECT_NAME,
            sp_riv_path=riv_path,
            containment_root=riv_path.parent,
        )
    except KeyError as error:
        raise RuntimeError("DATABASE_URL is required for QHH SHUD output segment seeding.") from error
    except QhhProductionBootstrapError as error:
        print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
