from __future__ import annotations

import json
import os
from pathlib import Path

from workers.model_registry.qhh_production_bootstrap import (
    DEFAULT_QHH_MODEL_ID,
    DEFAULT_QHH_PROJECT_NAME,
    QhhProductionBootstrapError,
    seed_qhh_forcing_stations,
)

ROOT = Path(__file__).resolve().parents[1]
BASINS_ROOT = Path(os.getenv("NHMS_BASINS_ROOT", ROOT / "data" / "Basins"))
MODEL_ID = os.getenv("QHH_MODEL_ID", DEFAULT_QHH_MODEL_ID)
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", DEFAULT_QHH_PROJECT_NAME)
SOURCE_FILE = Path(
    os.getenv(
        "QHH_TSD_FORC_PATH",
        BASINS_ROOT / "qhh" / "input" / PROJECT_NAME / f"{PROJECT_NAME}.tsd.forc",
    )
)


def main() -> int:
    try:
        result = seed_qhh_forcing_stations(
            database_url=os.environ["DATABASE_URL"],
            model_id=MODEL_ID,
            project_name=PROJECT_NAME,
            tsd_forc_path=SOURCE_FILE,
            containment_root=SOURCE_FILE.parent,
        )
    except KeyError as error:
        raise RuntimeError("DATABASE_URL is required for QHH forcing station seeding.") from error
    except QhhProductionBootstrapError as error:
        print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
