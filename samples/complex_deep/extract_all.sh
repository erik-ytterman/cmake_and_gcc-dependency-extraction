#!/usr/bin/env bash
# extract_all.sh -- configure the complex_deep sample, extract its application,
# and check what the emitted CMakeLists re-declares. Run from anywhere.
#
#   bash samples/complex_deep/extract_all.sh [--verify]
#
# --verify also builds and tests the extracted tree. That re-fetches Boost and
# nlohmann_json and builds Boost from source, so it takes several minutes and
# needs ~1 GB of free disk.

set -euo pipefail
cd "$(dirname "$0")/../.."       # repo root

SRC=samples/complex_deep
BUILD=$SRC/build
OUT=${TMPDIR:-/tmp}/cd-extracted
VERIFY=${1:-}

echo "== configure + build the sample (fetches and builds Boost) =="
cmake -S "$SRC" -B "$BUILD" -DCMAKE_CXX_FLAGS="-MMD" >/dev/null
cmake --build "$BUILD" -j >/dev/null 2>&1
ctest --test-dir "$BUILD" --output-on-failure >/dev/null
echo "   ok"

rm -rf "$OUT"

# app|expected FetchContent (comma-sep, - for none)|expected find_package
CASES="
report|boost,nlohmann_json|-
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

# The application must link the real imported target, not a <name>::<name> guess.
grep -q 'Boost::algorithm' "$OUT/report/CMakeLists.txt" \
  || { echo "FAIL: report does not link Boost::algorithm"; fail=1; }

# structure-preservation: both same-basename core sources survive, private header too
test -f "$OUT/report/src/core/src/util.cpp"        || { echo "FAIL: missing src/core/src/util.cpp"; fail=1; }
test -f "$OUT/report/src/core/src/detail/util.cpp" || { echo "FAIL: missing src/core/src/detail/util.cpp"; fail=1; }
test -f "$OUT/report/src/core/src/internal.hpp"    || { echo "FAIL: private header not beside its sources"; fail=1; }

# the three heavy libraries must NOT have come along
for heavy in geom netsvc parsing; do
  [ -d "$OUT/report/src/$heavy" ] && { echo "FAIL: $heavy leaked into the closure"; fail=1; }
done
# ...and neither must Boost's headers
[ -d "$OUT/report/include/boost" ] && { echo "FAIL: Boost headers were copied"; fail=1; }

rm -f "$SRC"/-.d
echo
[ "$fail" = 0 ] && echo "EXTRACTION AS EXPECTED" || { echo "SOME CHECKS FAILED"; exit 1; }
