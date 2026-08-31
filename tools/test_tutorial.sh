#!/usr/bin/env bash
# test_tutorial.sh — run every command block in TUTORIAL.md end to end.
#
# Reproduces §2, Lab 1 (§4), Lab 2 (§5), Lab 3 (§6), the §7 extractor runs,
# and the §9 Q2 check. Run from the repo root:
#
#     bash tools/test_tutorial.sh
#
# Exits non-zero on the first command that fails.

set -euo pipefail

# Always operate from the repo root, regardless of where we're invoked from.
cd "$(dirname "$0")/.."

section() { printf '\n========== %s ==========\n' "$1"; }

section "prerequisites"
cmake --version | head -1
gcc --version | head -1
python3 --version

# --- §2: the graphviz approach (shown only to be dismissed) --------------
section "§2  cmake --graphviz"
cmake --graphviz=build/deps.dot -S . -B build >/dev/null
ls build/deps.dot

# --- Lab 1 (§4): the CMake File API --------------------------------------
section "Lab 1  File API query"
mkdir -p build/.cmake/api/v1/query
touch build/.cmake/api/v1/query/codemodel-v2
cmake build            # no-op reconfigure using the existing cache
ls build/.cmake/api/v1/reply/

section "Lab 1  index reader"
python3 -c "
import json,glob
i=json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/index-*.json'))[-1]))
print(i['cmake']['version']['string'])
for o in i['objects']: print(o['kind'], o['jsonFile'])
"

section "Lab 1  greeter target"
python3 -c "
import json,glob
cm=json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/codemodel-v2-*.json'))[-1]))
t=[o for o in cm['configurations'][0]['targets'] if o['name']=='greeter'][0]
tf=json.load(open('build/.cmake/api/v1/reply/'+t['jsonFile']))
print('id        ', tf['id'])
print('type      ', tf['type'])
print('paths     ', tf['paths'])
print('deps      ', [d['id'] for d in tf.get('dependencies',[])])
print('sources   ', [(s['path'], s.get('compileGroupIndex')) for s in tf['sources']])
print('includes  ', [i['path'] for i in tf['compileGroups'][0]['includes']])
"

section "Lab 1  directory graph"
python3 -c "
import json,glob
cm=json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/codemodel-v2-*.json'))[-1]))
for i,d in enumerate(cm['configurations'][0]['directories']):
    print(i, d['source'], '->', d['build'], 'parent=', d.get('parentIndex'))
"

# --- Lab 2 (§5): compiler depfiles ---------------------------------------
section "Lab 2  build with -MMD"
cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD" >/dev/null
cmake --build build -j >/dev/null

section "Lab 2  greeter depfile"
cat build/apps/greeter/CMakeFiles/greeter.dir/src/main.cpp.o.d

section "Lab 2  include flags (plain -I, not -isystem)"
grep CXX_INCLUDES build/apps/greeter/CMakeFiles/greeter.dir/flags.make

# --- Lab 3 (§6): the dependency boundary ---------------------------------
section "Lab 3  FetchContent_Declare"
grep -n -A5 "FetchContent_Declare" CMakeLists.txt

section "Lab 3  ctest introspection"
( cd build && ctest --show-only=json-v1 |
  python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['tests'][0], indent=2))" )

# --- §7: putting it together ---------------------------------------------
section "§7  extract greeter --verify"
python3 tools/extract_closure.py greeter --verify
echo "--- extracted/greeter/CMakeLists.txt ---"
cat extracted/greeter/CMakeLists.txt

section "§7  extract tally --verify  (no third-party dependency)"
python3 tools/extract_closure.py tally --verify
echo "--- extracted/tally/CMakeLists.txt ---"
cat extracted/tally/CMakeLists.txt
echo "--- tally has no _deps/ in its build tree: ---"
if [ -d extracted/tally/build/_deps ]; then
  echo "UNEXPECTED: _deps/ present"; exit 1
else
  echo "confirmed: no extracted/tally/build/_deps"
fi

# --- §9 Q2: tally --with-tests still emits no FetchContent ----------------
section "§9 Q2  tally --with-tests emits no FetchContent"
python3 tools/extract_closure.py tally --with-tests >/dev/null
if grep -q FetchContent extracted/tally/CMakeLists.txt; then
  echo "UNEXPECTED: FetchContent present"; exit 1
else
  echo "confirmed: no FetchContent in extracted/tally"
fi

section "ALL LABS PASSED"
