#!/usr/bin/env bash
# node-22 generic unit-failure alert handler (#2913, OpenSpec
# node22-config-hygiene design D4). Mirrors scripts/node27_unit_failure_alert_once.sh.
#
# Invoked by infra/systemd/nhms-node22-unit-failure-alert@.service as the
# `OnFailure=` handler of a node-22 user unit:
#
#   OnFailure=nhms-node22-unit-failure-alert@%n.service
#
# `%n` expands to the FULL unit name, so $1 already carries the `.service`
# suffix. Deliberately DUMB: no database, no state file, no deduplication, no
# Python. Differences from the node-27 handler: the journal read is fixed at
# 30 lines of the user journal (no line-count or scope knob), and the
# transport has no default here -- `/usr/sbin/sendmail` is only the example
# value in infra/env/node22-unit-failure-alert.example.
#
# Mail channel: `$NHMS_FRONTIER_SENDMAIL -t -i` with NHMS_ALERT_EMAIL_TO /
# NHMS_ALERT_EMAIL_FROM, message on stdin, exit 0 == accepted.
#
# Exit discipline: a MISCONFIGURED alerter must not add a second failed unit
# on top of the failure it is reporting, so every unconfigured / unusable
# configuration path logs `SKIPPED reason=...` and exits 0. A transport that
# is present and REJECTS the message exits with the transport's rc, so a
# silently broken alert lane is itself visible.

set -euo pipefail

HOST=$(hostname 2>/dev/null || echo node-22)
SENDMAIL="${NHMS_FRONTIER_SENDMAIL:-}"
RECIPIENT="${NHMS_ALERT_EMAIL_TO:-}"
SENDER="${NHMS_ALERT_EMAIL_FROM:-}"

note() { echo "node22-unit-failure-alert: $*" >&2; }

# Fail-closed header hygiene: a CR/LF in an operator-edited env value opens a
# new header (an injected `Bcc:` would reach the envelope). Checked on the RAW
# value and refused, never stripped.
has_header_break() {
  case "$1" in
  *$'\r'* | *$'\n'*) return 0 ;;
  *) return 1 ;;
  esac
}

FAILED_UNIT="${1:-}"
if [ -z "$FAILED_UNIT" ]; then
  # Broken OnFailure= wiring (no %n). Report and stay green.
  note "SKIPPED reason=NO_UNIT_ARGUMENT"
  exit 0
fi
# The value comes from systemd's instance name, but a CR/LF must never be able
# to open a new header.
SAFE_UNIT=$(printf '%s' "$FAILED_UNIT" | tr -d '\r\n')

if [ -z "$RECIPIENT" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_TO_UNSET"
  exit 0
fi
if [ -z "$SENDER" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_FROM_UNSET"
  exit 0
fi
# The tainted value is NEVER echoed back into the log line; name the variable.
if has_header_break "$RECIPIENT"; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_TO_UNSAFE"
  exit 0
fi
if has_header_break "$SENDER"; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_FROM_UNSAFE"
  exit 0
fi
if [ -z "$SENDMAIL" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=SENDMAIL_UNCONFIGURED"
  exit 0
fi
if [ ! -x "$SENDMAIL" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=SENDMAIL_UNAVAILABLE path=$SENDMAIL"
  exit 0
fi

# Journal context is best effort: the alert must still go out naming the
# failed unit when the journal has nothing for it.
JOURNAL=$(journalctl --user -u "$FAILED_UNIT" -n 30 --no-pager 2>&1 || true)
if [ -z "$JOURNAL" ]; then
  JOURNAL="(no journal context available for $SAFE_UNIT)"
fi

# `|| rc=$?` (not `if ! ...`): inside a negated branch `$?` is the negation's
# status, which would swallow the failure. With `pipefail` this rc is the
# transport's own. Date / MIME-Version / Content-Type are written here because
# nothing downstream injects them; the charset matters for non-ASCII journal
# lines.
rc=0
printf '%s\n' \
  "To: $RECIPIENT" \
  "From: $SENDER" \
  "Subject: [NHMS node-22] unit failed: $SAFE_UNIT" \
  "Date: $(date -R)" \
  "MIME-Version: 1.0" \
  'Content-Type: text/plain; charset="utf-8"' \
  "" \
  "systemd unit $SAFE_UNIT entered the failed state on $HOST at $(date -u +'%Y-%m-%dT%H:%M:%SZ')." \
  "" \
  "Last 30 journal lines:" \
  "$JOURNAL" \
  | "$SENDMAIL" -t -i || rc=$?
if [ "$rc" -ne 0 ]; then
  note "SEND_FAILED unit=$SAFE_UNIT rc=$rc"
  exit "$rc"
fi

note "SENT unit=$SAFE_UNIT to=$RECIPIENT"
