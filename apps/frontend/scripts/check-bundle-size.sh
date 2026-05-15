#!/usr/bin/env bash
set -euo pipefail

assets_dir="${1:-dist/assets}"
limit_kb="${BUNDLE_GZIP_LIMIT_KB:-500}"
limit_bytes=$((limit_kb * 1024))

if [[ ! -d "$assets_dir" ]]; then
  echo "Bundle size check failed: $assets_dir does not exist. Run pnpm build first." >&2
  exit 1
fi

found_count=0
total_bytes=0
included_count=0

is_excluded_vendor_chunk() {
  local name="$1"

  # These exact prefixes are produced by vite.config.ts manualChunks for
  # production-ready heavy visualization libraries. Do not skip arbitrary app
  # chunks that merely contain "map" or "chart" in their names.
  [[ "$name" =~ ^vendor-(map|charts)-[A-Za-z0-9_-]+\.js$ ]] && return 0

  # Rollup can still emit MapLibre package subchunks alongside vendor-map.
  [[ "$name" =~ ^maplibre-gl-[A-Za-z0-9_-]+\.(js|css)$ ]] && return 0

  return 1
}

while IFS= read -r file; do
  found_count=$((found_count + 1))
  name="$(basename "$file")"
  if is_excluded_vendor_chunk "$name"; then
    echo "skip ${name} (intentional heavy vendor chunk)"
    continue
  fi

  gzipped_bytes="$(gzip -c "$file" | wc -c | tr -d '[:space:]')"
  total_bytes=$((total_bytes + gzipped_bytes))
  included_count=$((included_count + 1))
  awk -v bytes="$gzipped_bytes" -v name="$name" 'BEGIN { printf "include %7.1f KB gzip %s\n", bytes / 1024, name }'
done < <(find "$assets_dir" -type f \( -name '*.js' -o -name '*.css' \) | sort)

if [[ "$found_count" -eq 0 ]]; then
  echo "Bundle size check failed: no JS or CSS assets found in $assets_dir." >&2
  exit 1
fi

if [[ "$included_count" -eq 0 ]]; then
  echo "Bundle size check failed: no budgeted JS or CSS assets were included." >&2
  exit 1
fi

total_kb="$(awk -v bytes="$total_bytes" 'BEGIN { printf "%.1f", bytes / 1024 }')"
if [[ "$total_bytes" -gt "$limit_bytes" ]]; then
  echo "Bundle size check failed: ${total_kb} KB gzip exceeds ${limit_kb} KB limit." >&2
  exit 1
fi

echo "Bundle size check passed: ${total_kb} KB gzip under ${limit_kb} KB limit."
