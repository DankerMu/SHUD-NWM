# node-22 live record-budget measurement — #1953, head 7b38bcb8

Read-only. Nothing was written under the journal root: the script replays the
same record sources `_replay_all_pipeline_job_records` reads, with per-surface
counters instead of a budget, and then probes the default-budget lanes. No
`uv run`, no `uv sync` — the node-22 exception in CLAUDE.md pins the activity to
the exact active interpreter, because the shared `.venv` is 3.12.7 and the
repository pin is 3.11.

```text
interpreter: /scratch/frd_muziyao/NWM/.venv/bin/python
command:     /scratch/frd_muziyao/NWM/.venv/bin/python measure_1953.py \
               /scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal \
               result.json
node-22 head: 7b38bcb8bebd65ce051dae0771c896e1ca96c0fc
measured_at:  2026-09-14T23:35:46Z
script sha256: 3640c5cffc3dfa0bf7d4ff092ceec07036387383966ac40f48d881b6a79d6b18
receipt:      node22-2026-09-14-7b38bcb8-record-budget.json
```

`measure_1953.py` is a one-shot out-of-tree probe (it hardcodes the node-22
`sys.path`), deliberately not committed; the sha256 above is what pins the exact
version that produced this JSON.

## Why the receipt speaks for master

`services/orchestrator/file_orchestration_journal.py` is **byte-identical**
between node-22's head `7b38bcb8` and `origin/master`: both resolve to blob
`4ea00cbf8e125905d1ab8ea897a459d065dc90dc`
(`git rev-parse 7b38bcb8:services/orchestrator/file_orchestration_journal.py`
and the same for `origin/master`). The budget, the replay and the blocked-row
synthesis measured below are therefore master's, not a worktree's.

## Per surface (no budget: `max_records=10**9`)

| surface | files | raw consumes |
|---|---|---|
| `latest/**` | 7,898 | 54,258 |
| `journal/**.jsonl` | 325 | 94,123 |
| flat direct | — | 5,328 |

- raw consumes, `include_direct=False`: **148,381**
- raw consumes, `include_direct=True`: **153,709**
- unique jobs: **17,025** either way
- unbudgeted whole-tree replay: **132.73 s**
- default budget (`MAX_FILE_JOURNAL_RECORDS`): **100,000**

The budget bounds read WORK, not result size: 17,025 unique jobs cost 153,709
consumes, because one `latest/` view materialises many rows and every JSONL line
in every segment charges one unit whatever its record type.

## Default-budget probes

| probe | outcome | seconds |
|---|---|---|
| `_iter_pipeline_job_records()` (`include_direct=True`) | raised `file_journal_record_limit_exceeded: pipeline_job_records` | 91.97 |
| `_iter_pipeline_job_records(include_direct=False)` | raised `file_journal_record_limit_exceeded: pipeline_job_records` | 93.52 |
| `query_pipeline_job_by_slurm_id("measure-1953-nonexistent")` | returned a synthetic row, `status: "running"`, `error_code: file_journal_record_limit_exceeded`, `file_journal.evidence: {}` | 90.67 |
| `query_candidate_state("measure-1953-underivable-key")` | returned a synthetic row, `status: "running"`, `error_code: file_journal_record_limit_exceeded`, `file_journal.evidence: {}` | 91.12 |

Both returned rows carry an EMPTY `evidence` and the status of a job that is
running — which is precisely what this measurement was taken to show, and
what #1953 changes: the refusal now carries `evidence.lane`, and the synthetic
row's status names the blocked read. The reason token and the field are
unchanged, so
the census runbook's `file_journal_record_limit_exceeded: pipeline_job_records`
stderr line still reads exactly as recorded on 2026-09-02.
