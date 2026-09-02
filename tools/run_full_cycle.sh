#!/usr/bin/env bash
# run_full_cycle.sh -- prove the whole repo works from a clean checkout.
#
# For every sample project, in order:
#   1. configure with -MMD, build, run its own CTest suite
#   2. extract every app target (--with-tests)
#   3. with --verify, also configure / build / test each extracted tree
#
# Run from anywhere:
#
#     bash tools/run_full_cycle.sh [--verify] [--hard]
#
#   --verify   also build + test each extracted tree. Slower: every extracted
#              tree re-fetches its third-party dependencies from scratch
#              (minutes for samples/complex_deep).
#   --hard     `git clean -ffdx` the entire repo first -- removes ALL untracked
#              files. The default clean removes only the git-ignored build
#              output (build*/, extracted/, __pycache__/, the stray -.d).
#
# Runs the whole matrix even if something fails; exits non-zero if any
# extraction failed.

set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

VERIFY=""
HARD=0
for arg in "$@"; do
  case "$arg" in
    --verify) VERIFY="--verify" ;;
    --hard)   HARD=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

SAMPLES="samples/basic samples/complex_deep"
OUT="${TMPDIR:-/tmp}/full-cycle-extracted"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# --- 1. clean ---------------------------------------------------------------
step "clean the repository"
if [ "$HARD" = 1 ]; then
  git clean -ffdx
else
  # -X = ignored files only (build*/, extracted/, __pycache__/, -.d);
  # -ff so the nested FetchContent checkouts under build/_deps/ go too.
  git clean -ffdX
fi
rm -rf "$OUT"
echo "   working tree:"
git status --short | sed 's/^/   /' || true

# --- 2. per-sample cycle --------------------------------------------------
declare -a RESULTS=()

for SRC in $SAMPLES; do
  name=$(basename "$SRC")
  build="$SRC/build"

  step "$name : configure + build + test the sample"
  cmake -S "$SRC" -B "$build" -DCMAKE_CXX_FLAGS="-MMD" >/dev/null
  cmake --build "$build" -j >/dev/null
  ctest --test-dir "$build" --output-on-failure >/dev/null
  echo "   sample builds and its tests pass"

  for appdir in "$SRC"/apps/*/; do
    app=$(basename "$appdir")
    step "$name : extract '$app'${VERIFY:+  (+ verify)}"
    if python3 tools/extract_closure.py "$app" \
         --src "$SRC" --build "$build" --out "$OUT/$name" \
         --with-tests $VERIFY 2>&1 | sed 's/^/   /'; then
      RESULTS+=("ok    $name/$app")
    else
      RESULTS+=("FAIL  $name/$app")
    fi
  done
done

rm -f ./-.d "$REPO"/samples/*/-.d

# --- 3. summary ----------------------------------------------------------
step "summary"
printf '   %s\n' "${RESULTS[@]}"
echo
echo "   extracted trees: $OUT/<sample>/<app>/"

if printf '%s\n' "${RESULTS[@]}" | grep -q '^FAIL'; then
  echo
  echo "SOME EXTRACTIONS FAILED" >&2
  exit 1
fi

step "FULL CYCLE GREEN"
