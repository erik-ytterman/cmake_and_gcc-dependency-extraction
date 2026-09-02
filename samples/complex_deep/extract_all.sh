#!/usr/bin/env bash
# extract_all.sh -- configure the complex_deep fixture once, then extract every
# app and check what each one re-declares. Run from anywhere.
#
#   bash samples/complex_deep/extract_all.sh [--verify]
#
# --verify also builds (and tests) each extracted tree; that re-fetches fmt and
# nlohmann_json per app, so it takes a few minutes.

set -euo pipefail
cd "$(dirname "$0")/../.."       # repo root

SRC=samples/complex_deep
BUILD=$SRC/build
OUT=${TMPDIR:-/tmp}/cd-extracted
VERIFY=${1:-}

echo "== configure + build the fixture =="
cmake -S "$SRC" -B "$BUILD" -DCMAKE_CXX_FLAGS="-MMD" >/dev/null
cmake --build "$BUILD" -j >/dev/null
ctest --test-dir "$BUILD" --output-on-failure >/dev/null
echo "   ok"

rm -rf "$OUT"

# app|expected FetchContent (comma-sep, - for none)|expected find_package
CASES="
calc|-|-
render|fmt|-
daemon|-|Threads
report|fmt,nlohmann_json|-
omni|fmt,nlohmann_json|Threads
"

fail=0
while IFS='|' read -r app want_fc want_fp; do
  [ -n "$app" ] || continue
  echo "== $app =="
  python3 tools/extract_closure.py "$app" \
      --src "$SRC" --build "$BUILD" --out "$OUT" --with-tests $VERIFY \
      2>&1 | sed 's/^/   /'

  cml=$OUT/$app/CMakeLists.txt
  got_fc=$(grep -oE 'FetchContent_MakeAvailable\(([^)]*)\)' "$cml" | sed -E 's/.*\(([^)]*)\).*/\1/;s/ /,/g' || true)
  [ -z "$got_fc" ] && got_fc="-"
  got_fp=$(grep -oE 'find_package\(([A-Za-z0-9_]+)' "$cml" | sed 's/find_package(//' | paste -sd, - || true)
  [ -z "$got_fp" ] && got_fp="-"

  if [ "$got_fc" != "$want_fc" ]; then echo "   FAIL FetchContent: want '$want_fc' got '$got_fc'"; fail=1; fi
  if [ "$got_fp" != "$want_fp" ]; then echo "   FAIL find_package: want '$want_fp' got '$got_fp'"; fail=1; fi
done <<< "$CASES"

# structure-preservation check: calc keeps both same-basename mathx sources
test -f "$OUT/calc/src/mathx/src/util.cpp"        || { echo "FAIL: missing src/mathx/src/util.cpp"; fail=1; }
test -f "$OUT/calc/src/mathx/src/detail/util.cpp" || { echo "FAIL: missing src/mathx/src/detail/util.cpp"; fail=1; }
test -f "$OUT/calc/src/mathx/src/internal.hpp"    || { echo "FAIL: private header not beside its sources"; fail=1; }

rm -f "$SRC"/-.d
echo
[ "$fail" = 0 ] && echo "ALL APPS EXTRACTED AS EXPECTED" || { echo "SOME CHECKS FAILED"; exit 1; }
