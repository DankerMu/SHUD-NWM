# Design

## Numbers

| | before | after | reason |
|---|---|---|---|
| wait bound | 5400 s | 14400 s | longest recorded pass interval 193.9 min (11,634 s); observed 126 min |
| unit `TimeoutStartSec` | 7200 s | 21600 s | wait bound + the 7200 s the refresh had before the wait existed |
| succession start timeout | 7500 s | 21900 s | unit timeout + 300 s, as before |
| probe stopped dwell | 6 h (3 x unit timeout) | 8 h (unit timeout + 2 h) | see below |

## Probe dwell

The stopped dwell tolerates an operator's manual-publisher window: stop the refresh timer, wait for a refresh
in flight to end, publish, start the timer. The wait can last the refresh oneshot's whole start timeout, now
6 h. Three options:

- 6 h (unchanged): a legal window that waits out a full start timeout exceeds the dwell and reports
  `timer_stopped` once; a documented false alarm.
- 18 h (the old factor of three): no false alarm, but a timer that was stopped and forgotten (2026-08-28, six
  days) is reported twelve hours later.
- 8 h (start timeout + 2 h for the publish): the legal window stays inside the dwell and detection moves by
  two hours, against manifest bounds of 120 h (warning) and 168 h (consumer). Chosen.

`MAX_STOPPED_DWELL_HOURS` (24) is untouched. The probe reads only the timer's state and the manifest age, so
a refresh service that is `activating` for hours does not change its verdict.

## Effect of a longer start job

While the refresh job is starting, `Before=` keeps the next scheduler pass queued (observed on 2026-10-08,
109 samples). A refresh that waits for a pass therefore delays the next pass only by its own run time (about
10 minutes), as before; the wait itself overlaps a pass that is running anyway. A hung `systemctl` inside
the wait is now ended after 6 h instead of 2 h, and the scheduler would be held that long. The stall probe's
defaults are 360 minutes of age (`DEFAULT_MAX_TRIGGER_AGE_MINUTES`, `DEFAULT_MAX_PASS_AGE_MINUTES`), so such a
hang is reported at about the time systemd ends it. Not changed here.

The same holds for a refresh that hangs after a short wait: the wrapper has no timeout of its own for the
Python runner, so its budget grows from 7200 s to as much as 21600 s, during which `Before=` holds the
scheduler; through the succession tool (scheduler timer stopped) the blocking start can last 21900 s instead
of 7500 s. The stall probe's pass-age default (360 min) equals the new timeout, so there is no lead time.
Known cost of one start timeout covering both the wait and the refresh; accepted here.
