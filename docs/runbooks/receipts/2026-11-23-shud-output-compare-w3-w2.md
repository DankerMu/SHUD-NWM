# SHUD output comparison: w3 and w2 against node-22, 2026-11-23

Provenance: the comparison directories were under `/scratch/frd_muziyao/nhms-prod/tmp/shud-verify/`
on w3 and no longer exist. The figures below are recovered from the migration session log
(tool output at 2026-11-23T07:18Z-07:22Z), not re-measured.

Method: take a forecast run directory produced on node-22 (`fcst_gfs_2026112112_*`), keep its
`output/` as the reference, rerun the same input through Slurm on the new host
(`-p CPU -A friends -c 4 --mem=8G`, `OMP_NUM_THREADS=4`, `SHUD/shud -n 4 <basin>`), then
`cmp -s` every reference file against the new output.

| basin | host | Slurm job | state | wall | files compared | byte-identical |
|---|---|---|---|---|---|---|
| tlh | w3 | 1000018 | COMPLETED 0:0 | 0.92 s | 27 | 22 |
| qhh | w3 | 1000020 | COMPLETED 0:0 | 8.81 s | 24 | 19 |
| qhh | w2 | (canary job) | COMPLETED | not recovered | 21 (walk excluded `state_checkpoints/`) | 19 |

Files that were not byte-identical:

| file | reference vs new | what was observed |
|---|---|---|
| `<basin>.SHUD` | 804 vs 805 bytes (tlh, qhh on w3); differs on w2 | content not examined |
| `<basin>.time.csv` | 7155 vs 7151 bytes (tlh), 7321 vs 7317 bytes (qhh) on w3; differs on w2 | content not examined |
| `state_checkpoints/state_checkpoints.json`, `state_checkpoints/<basin>.f012.cfg.ic.update`, `state_checkpoints/.<basin>.f012.cfg.ic.update.normalized` | present in the reference, missing in the w3 rerun | the rerun executed only the `shud` binary; whether a later pipeline stage writes these files was not checked at the time |

Every other output file (the `*.csv` series, `DY.dat`, `*.cfg.ic.update`, the `Debug_Table_*`
files, `cvode_stats.txt`, `nfcall.txt`, the `.bak` copies) compared byte-identical on both hosts.

What this receipt does not show: bit-identity of the two files whose content was not examined,
and any basin other than tlh and qhh.
