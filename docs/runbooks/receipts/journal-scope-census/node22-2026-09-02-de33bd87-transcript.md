# node-22 live census transcript — PR #1951 head de33bd87

## run 1 (default budgets) — fail-loud, exit 1

```text
pre HEAD=3acea778 porcelain=?? .nhms-work/ 
worktree HEAD=de33bd8722914e912ae58432b4eff040ba2be29a
Python 3.12.7
journal module: /scratch/frd_muziyao/nhms-census-de33bd87/services/orchestrator/file_orchestration_journal.py
census module: /scratch/frd_muziyao/nhms-census-de33bd87/services/orchestrator/journal_scope_census.py
root_source_pid=2672417 root=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal realpath=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal
timer=active service=activating start_utc=2026-09-02T09:40:26Z
exit=1 end_utc=2026-09-02T09:43:01Z service_after=activating
--- stderr
file_journal_record_limit_exceeded: pipeline_job_records
--- receipt
(no receipt file)
--- post
/scratch/frd_muziyao/NWM                                  3acea778 [master]
/scratch/frd_muziyao/NWM-issue-1684                       de63a5ad (detached HEAD)
/scratch/frd_muziyao/NWM/.codex/verify-issue-253-cff8d6e  cff8d6e8 (detached HEAD)
post HEAD=3acea778 porcelain=?? .nhms-work/ 
```

## run 2 (--max-records 5000000) — exit 0

```text
pre HEAD=3acea778 porcelain=?? .nhms-work/ 
worktree HEAD=de33bd8722914e912ae58432b4eff040ba2be29a
Python 3.12.7
journal module: /scratch/frd_muziyao/nhms-census-de33bd87/services/orchestrator/file_orchestration_journal.py
census module: /scratch/frd_muziyao/nhms-census-de33bd87/services/orchestrator/journal_scope_census.py
root_source_pid=2682444 root=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal realpath=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal
timer=active service=activating start_utc=2026-09-02T09:43:44Z
exit=0 end_utc=2026-09-02T09:46:17Z service_after=activating
--- stderr
--- receipt
{"divergent_rows": [], "divergent_total": 0, "exit_code": 0, "generated_at": "2026-09-02T09:46:17Z", "journal_root": "/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal", "journal_root_verified": "/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal", "limits": {"max_files": 100000, "max_records": 5000000}, "reconcile_abort_triggers": 0, "schema_version": "nhms.scheduler.job_id_scope_census.v1", "surfaces": {"active_reconcile": {"divergent": 0, "files": 0, "present": false, "rows": 0}, "by_cycle_direct": {"divergent": 0, "files": 9362, "present": true, "rows": 9362}, "flat_direct": {"divergent": 0, "files": 5125, "present": true, "rows": 5125}, "journal_replay": {"divergent": 0, "files": 6273, "latest_files": 5998, "latest_present": true, "present": true, "rows": 14922, "segment_files": 275, "segment_present": true}, "reconcile_inventory": {"divergent": 0, "files": 3, "present": true, "residue": 0, "rows": 3}}}
--- post
/scratch/frd_muziyao/NWM                                  3acea778 [master]
/scratch/frd_muziyao/NWM-issue-1684                       de63a5ad (detached HEAD)
/scratch/frd_muziyao/NWM/.codex/verify-issue-253-cff8d6e  cff8d6e8 (detached HEAD)
post HEAD=3acea778 porcelain=?? .nhms-work/ 
```
