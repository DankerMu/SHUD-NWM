#!/usr/bin/env bash
# #2433 evidence — node-27 autopipe publish ticks (published>0) 与当时活动树身份。只读，不写任何服务状态。
set -uo pipefail
LOG=/home/nwm/autopipe-logs/autopipe.log
echo "# captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) host=node-27 log=$LOG"
echo
echo "## 1. published>0 的 tick（2026-09-15 起）"
echo "##    起止时间戳按行内 'autopipe: start|done' 标记提取，不受同行 JSON 前缀干扰；"
echo "##    cycles 列是该 tick 内出现过的 \"cycle\" 值去重后的有序列表。"
awk '
function ts(line,  m) {
  if (match(line, /\[2[0-9-]+T[0-9:]+Z\] autopipe: (start|done)/)) {
    m = substr(line, RSTART, RLENGTH); sub(/\] autopipe:.*/, "]", m); return m
  }
  return "[?]"
}
/autopipe: start/ { st=ts($0); pub=0; proc=0; delete seen; cyc="" }
/"cycle": "/ {
  line=$0
  while (match(line, /"cycle": "[^"]+"/)) {
    c=substr(line, RSTART+10, RLENGTH-11)
    if (!(c in seen)) { seen[c]=1; cyc = (cyc=="" ? c : cyc "," c) }
    line=substr(line, RSTART+RLENGTH)
  }
}
/"processed": / { p=$2; gsub(/,/,"",p); proc=p+0 }
/"published": / { v=$2; gsub(/,/,"",v); if (v+0>0) pub=v+0 }
/autopipe: done/ {
  if (pub>0) {
    d=ts($0); rc=$0; sub(/.*autopipe: done /,"",rc)
    if (d >= "[2026-09-15") printf "start=%s done=%s processed=%-3d published=%-3d %-22s cycles=%s\n", st, d, proc, pub, rc, (cyc==""?"-":cyc)
  }
}
' "$LOG"
echo
echo "## 2. 最近 6 趟 tick（含 no-op，证明 lane 仍在跑）"
grep "autopipe: done" "$LOG" | tail -6
echo
echo "## 3. 活动树身份（分支 + master reflog；HEAD reflog 见 §4）"
cd /home/nwm/NWM || exit 1
echo "HEAD=$(git rev-parse HEAD) branch=$(git branch --show-current)"
git reflog show master --date=iso | sed -n '1,20p'
echo
echo "## 4. HEAD reflog（master 恢复点 2026-09-16 15:13 CST = 07:13Z）"
git reflog --date=iso | sed -n '1,24p'
