"""#1987 task 5.2: registry counts (active / runnable / selected / excluded), node-27.

The four counters are the scheduler's own, produced by services/orchestrator/
scheduler_models.py:discover_models (evidence keys at :103-109). This script calls the
REAL discover_models against the REAL registry reader, so the numbers are the scheduler's
definition, not a hand-written SQL approximation.

READ ONLY: the registry reader only issues SELECTs (packages/common/model_registry.py
list_models / get_model_internal); no scheduler pass is started, nothing is submitted,
node-22 is not touched. `selected` is reported under the NO-operator-filter
configuration (model_ids=None, basin_ids=None), stated explicitly because `selected`
is the only one of the four that depends on operator configuration.
DSN via env only, never argv.
"""
import json
import os
import sys

sys.path.insert(0, "/home/nwm/NWM")

from packages.common.model_registry import PsycopgModelRegistryStore
from services.orchestrator import scheduler_models


class _Cfg:
    model_ids = None
    basin_ids = None


class _Sched:
    def __init__(self, registry):
        self.registry = registry
        self.config = _Cfg()


registry = PsycopgModelRegistryStore(
    database_url=os.environ["DATABASE_URL"],
    application_name="issue1987-registry-counts-readonly",
)
selected, evidence = scheduler_models.discover_models(_Sched(registry))

COUNTERS = (
    "active_model_count",
    "runnable_model_count",
    "selected_model_count",
    "excluded_model_count",
)
counts = {k: evidence[k] for k in COUNTERS}
reasons = {}
for ex in evidence.get("exclusions") or []:
    reasons[ex.get("reason", "?")] = reasons.get(ex.get("reason", "?"), 0) + 1
out = {"counts": counts, "exclusion_reasons": reasons,
       "operator_filters": evidence.get("operator_filters")}
print(json.dumps(out, indent=2, default=str))
json.dump(out, open("/home/nwm/tmp/1987/registry-counts-0918.json", "w"), indent=2, default=str)
