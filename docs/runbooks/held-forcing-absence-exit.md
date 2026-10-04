# Held Forcing Master With No Job: The Operator-Verified Absence Exit (#2682)

Companion of [failed-basin-retry.md](failed-basin-retry.md) § "Disposition — held forcing master: the
forcing bind (#2675)". That section binds a held forcing master whose job **did** run. This one releases
a held forcing master whose job **does not exist**.

## When this applies

A forcing master is held when its journal row reads `status=reserved`, `slurm_job_id=null`,
`matched_slurm_job_id=null`, `submit_outcome=submit_result_ambiguous`, no `reconciliation_decision`, with
a complete forcing identity (`slurm_comment` is the attempt comment
`nhms_forcing_attempt:<idempotency_key>:a<submission_attempt>`, `cohort_members` covers tasks `0..n-1`,
and the owner is recorded when `slurm_ownership_required` is set). Since #2667 its members are skipped
`active_duplicate_pipeline` on every pass, which freezes the source's forward lane.

Use this exit only when all three hold:

- restart reconcile keeps reporting the row unresolved (typically `query_unavailable`, listed by
  `list-operator-actions` as `held_reservation_unresolved` once the anchor is 6h old);
- `sacct` and `squeue` show **no** pending or running master carrying this attempt's comment (the
  precondition below);
- there is therefore nothing for `bind-reserved-job` to bind, and the automatic
  `absence_retry_permitted` never arrives because reconcile cannot compute a credible absence.

`query_unavailable` is a reconcile answer, not a field of the row. The command cannot test it: your
verification replaces it, and is recorded.

## Precondition: prove the absence (read-only)

Take `submission_attempt`, `submission_attempt_started_at`, `slurm_comment`, `expected_slurm_user` and
`expected_slurm_account` from the journal replay (step 1 of the procedure), then query all users:

```bash
sacct -a --name nhms_forcing --starttime <submission_attempt_started_at> --endtime now --parsable2 \
  --format=JobID,JobName,State,User,Account,Submit,End,SubmitLine
squeue -a --name nhms_forcing -o "%i %T %u %a %V %k"
```

Search both outputs for the exact attempt comment (`--comment=<slurm_comment>` in `SubmitLine`, the
`%k` column of `squeue`). A comment ending in another `:a<m>` belongs to another attempt of the same key
and does not count.

**The precondition is: every master carrying this attempt's comment is terminal or cancelled, or there
is none.** In detail:

| what `sacct` / `squeue` show for this attempt's comment | exit |
|---|---|
| no master at all | this runbook |
| one or more masters, all `FAILED` / `CANCELLED` / `TIMEOUT` / `NODE_FAIL`, none in `squeue` | this runbook; put each id and `State` in `--verification-note` |
| a master `PENDING` or `RUNNING` (in `sacct` or `squeue`) | **stop**: bind it with `bind-reserved-job` (#2675), or wait for it; never release |
| a master `COMPLETED` | bind it with `bind-reserved-job` (#2675): the members then resume at forecast without recomputing forcing |

If `sacct` itself fails or returns a truncated listing, the absence is not proven: stop and escalate.

## The double-write risk

This exit is the same class of operation as the forecast demotion: it makes the next pass submit forcing
again. Forcing artifacts are keyed by source, cycle and basin, **not by attempt**. If the job of this
attempt did run, or is still running, the retry is a second writer over the same forcing products, and
nothing in the code can detect that afterwards.

The command reduces the risk but cannot remove it:

- it refuses a `--checked-at` earlier than the attempt anchor plus the reconcile absence grace
  (`reconcile.RESERVATION_ABSENCE_GRACE`, 120 s: before it, an empty `sacct` answer may only be
  slurmdbd propagation lag) or later than now;
- it re-checks the full held tuple, the attempt and the anchor under the journal lock;
- it never runs `sbatch` and never runs `scancel`.

Everything else rests on the precondition above. You attest it with `--checked-by` and
`--verification-note`, and the journal keeps that attestation in an audit event.

## Procedure

1. **Preview the CAS inputs** from the journal replay, never from pass evidence or the flat
   `pipeline-jobs/<job_id>.json`:

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -c 'import json, sys
   from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
   row = FileOrchestrationJournalRepository(sys.argv[1]).get_pipeline_job(sys.argv[2])
   keys = ("status", "slurm_job_id", "matched_slurm_job_id", "submit_outcome", "reconciliation_decision",
           "submission_attempt", "submission_attempt_started_at", "slurm_comment", "expected_slurm_user",
           "expected_slurm_account", "slurm_ownership_required")
   print(json.dumps({key: row.get(key) for key in keys} | {"members": len(row["cohort_members"])}, sort_keys=True))' \
     <journal-root> <job_id>
   ```

2. **Run the precondition queries** above and keep their output.
3. **Release.** The command is the existing `demote-reserved-job`; the journal picks the forcing branch
   from the row it re-reads under the lock. On node-22 use the exact interpreter, not bare `uv`:

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli demote-reserved-job \
     --journal-root <journal-root> \
     --job-id job_cycle_<source>_<cycle>_..._forcing \
     --expected-attempt <submission_attempt> \
     --expected-attempt-started-at <submission_attempt_started_at> \
     --checked-by <operator> \
     --checked-at <when the queries ran, timezone-aware> \
     --verification-note "<the sacct and squeue commands and what they showed>" \
     --confirm
   ```

   Example:

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli demote-reserved-job \
     --journal-root <journal-root> \
     --job-id job_cycle_gfs_2026092500_convert_cohort_36e4f7b9bf80_forcing \
     --expected-attempt 1 \
     --expected-attempt-started-at 2026-09-25T03:12:44.118302Z \
     --checked-by frd_muziyao \
     --checked-at 2026-09-26T01:40:00Z \
     --verification-note "sacct -a --name nhms_forcing --starttime 2026-09-25T03:12:44 -o JobID,State,Submit,SubmitLine and squeue -a --name nhms_forcing: no master with nhms_forcing_attempt:cycle_gfs_2026092500_convert_cohort_36e4f7b9bf80:forcing:a1" \
     --confirm
   ```

   Exit 0 prints a sorted JSON receipt: `status=demoted` (or `demoted_with_warnings` when a derived
   projection failed after the durable append; the release is committed in both cases, do not retry it),
   `lane=forcing`, `status_to=reservation_lost`, `reconciliation_decision=absence_retry_permitted`, the
   attempt and anchor, the operator fields and `written_record_count=2`.

4. **After the release.** The row is exactly what the automatic absence permit writes
   (`reservation_lost` / `absence_retry_permitted`), plus one `operator_verified_absence` pipeline event
   (`details.lane=forcing`) holding `checked_by`, `checked_at`, `verification_note`, the expected attempt
   and anchor. Those fields are never written on the row, so a later attempt cannot inherit them. On the
   next pass the members are no longer held: their decision is `retry` with `restart_stage=forcing`, and
   a reserve of the same forcing run reclaims the master as attempt + 1 with a new attempt comment. The
   `held_reservation_unresolved` entry drops out of `list-operator-actions` once a newer reconciling pass
   no longer reports the row.

## Refusals

Missing `--confirm`, a blank `--checked-by` / `--verification-note`, or a timestamp without a timezone
exit 2 before the journal is opened. A forcing compare-and-swap refusal exits 2 with
`demote-reserved-job: refused: <name>; no journal bytes were written`:

| name | meaning |
|---|---|
| `identity_incomplete` | the forcing identity is incomplete (attempt comment, task map, or a required owner missing): escalate |
| `not_held` | not the held tuple: status is not `reserved`, `slurm_job_id` or `matched_slurm_job_id` is set, `submit_outcome` is not `submit_result_ambiguous`, or a `reconciliation_decision` is present; a repeated release lands here |
| `stale_attempt` | `--expected-attempt` or `--expected-attempt-started-at` differs from the durable row (a reconcile pass or another operator moved it): preview again |
| `verification_before_grace` | `--checked-at` is in the future, or earlier than the anchor plus the absence grace: run the queries again after the grace and pass the real time |
| `attestation_missing` | the verifier or the evidence is blank (returned by the journal method; the command line refuses it earlier, naming the option) |

An unknown `--job-id` exits 2 with `demote-reserved-job: pipeline job not found: <job_id>`. A forecast
row keeps its own contract and message, see [failed-basin-retry.md](failed-basin-retry.md) §
"Disposition — confirmed dead rows: the guarded operator demotion".

Verified by `tests/test_orchestrator_demote_reserved_job_forcing.py` (typed CAS, byte-identical refusals,
the automatic-permit row oracle, both CLI entrypoints, and the held → release → reclaim lane with zero
submissions and zero cancellations).
