#!/usr/bin/env bash
# #2436 provider授权矩阵 — 在 node-27 上对天地图 DataServer 做只读 GET。
# key 从活动树源码读取并全程 redact：任何输出都不含 tk 值。
set -uo pipefail
SRC=/home/nwm/NWM/apps/frontend/src/components/map/m11MapRuntime.tsx
TK=$(grep -oE "'[0-9a-f]{32}'" "$SRC" | head -1 | tr -d "'")
[ -n "$TK" ] || { echo "FATAL: key not found in $SRC" >&2; exit 1; }
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
TILE='x=3&y=1&l=3'
redact() { sed -e "s/$TK/<REDACTED-TK>/g" -e 's/tk=[0-9a-f]\{8,\}/tk=<REDACTED-TK>/g'; }

probe() { # $1=label $2=T $3=useBrowserUA $4=referer(- for none) $5=tkvalue
  local args=(-s -o /tmp/nwm2436.body -w '%{http_code} %{size_download} %{content_type}' --max-time 20)
  [ "$3" = "ua" ] && args+=(-A "$UA")
  [ "$4" != "-" ] && args+=(-H "Referer: $4")
  local code; code=$(curl "${args[@]}" "https://t0.tianditu.gov.cn/DataServer?T=$2&$TILE&tk=$5")
  local body=''
  case "$code" in *json*) body=$(head -c 200 /tmp/nwm2436.body | redact) ;; esac
  printf '%-46s ua=%-7s referer=%-26s -> %s %s\n' "$1" "$3" "$4" "$code" "$body"
}

echo "# captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ) host=node-27 source=$SRC (key redacted)"
echo "## A. 非浏览器 UA（curl 默认）——key 权限类型闸"
probe "A1 vec_w / no referer"      vec_w curl - "$TK"
probe "A2 vec_w / loopback"        vec_w curl "http://127.0.0.1:18023/" "$TK"
probe "A3 vec_w / prod origin"     vec_w curl "https://test.nwm.ac.cn/" "$TK"
echo "## B. 浏览器 UA——域名白名单闸"
probe "B1 vec_w / no referer"      vec_w ua - "$TK"
probe "B2 vec_w / loopback"        vec_w ua "http://127.0.0.1:18023/" "$TK"
probe "B3 vec_w / prod origin"     vec_w ua "https://test.nwm.ac.cn/" "$TK"
probe "B4 cva_w(注记) / loopback"  cva_w ua "http://127.0.0.1:18023/" "$TK"
probe "B5 cva_w(注记) / prod origin" cva_w ua "https://test.nwm.ac.cn/" "$TK"
echo "## C. 对照——非法 key 与缺 key"
probe "C1 vec_w / bogus tk"        vec_w ua "https://test.nwm.ac.cn/" "0000000000000000000000000000dead"
noktk() {
  local code; code=$(curl -s -o /tmp/nwm2436.body -w '%{http_code} %{size_download} %{content_type}' --max-time 20 \
    -A "$UA" -H "Referer: https://test.nwm.ac.cn/" "https://t0.tianditu.gov.cn/DataServer?T=vec_w&$TILE")
  printf '%-46s ua=%-7s referer=%-26s -> %s %s\n' "C2 vec_w / 无 tk 参数" "ua" "https://test.nwm.ac.cn/" "$code" \
    "$(head -c 120 /tmp/nwm2436.body | tr -d '\n' | redact)"
}
noktk
