# Extraction algorithm

> Reference documentation. If you are meeting this pipeline for the first time,
> read [TUTORIAL.md](TUTORIAL.md) instead — it teaches the underlying APIs
> hands-on and covers the traps. Come back here for stage-by-stage detail.

This document walks `tools/extract_closure.py` **in the order it executes**. The
`extract()` function is the spine of the extractor; every other function is
described here at the point `extract()` first calls it. For each stage you get
its **purpose**, a concrete **input** and **output**, and the **algorithm** in
between.

The goal of the whole pipeline: given one application target `T`, produce a flat,
standalone, buildable directory containing exactly `T`'s minimal build closure
(first-party sources + headers + generated code), with third-party dependencies
kept as FetchContent declarations rather than copied.

## The running example

Every stage below is illustrated with one real run of the sample project in this
repo:

```sh
python3 tools/extract_closure.py guess --with-tests --verify
```

`guess` links the first-party libraries `rng` and `input`, the generated
`build_info` header, and the third-party library `fmt`. Both flags are on, so
every optional stage runs too. Paths are abbreviated with `…` for the repo root
`/home/.../cmake_and_gcc-dependency-extraction`.

## Prerequisites (what the build must already have produced)

The extractor is a *consumer* of an already-configured, already-built tree.
Before it runs:

- The project is configured into a build dir (default `build/`).
- The build was compiled with `-MMD` so a per-translation-unit `.d` header
  dependency file sits next to every object file under
  `build/**/CMakeFiles/<target>.dir/`.

No source file is ever parsed by hand; every fact comes from CMake or the
compiler.

## Execution order at a glance

```
                        ┌─ Stage 1   load_codemodel ────── CMake File API reply
  build/       ────────▶│  Stage 2   load_targets ──────── by_id / name_to_id / dir_of
  src/         ────────▶│  Stage 3   gather_fetchcontent ─ fetch{}   (CMakeLists.txt + include()s)
  -MMD *.o.d   ────────▶│  Stage 4   external_regions ──── regions{}  (third-party dirs)
  CMakeLists.txt ──────▶│  Stage 5   transitive_closure ── closure of target ids
  ctest registry ─────▶ │  Stage 6   classify ──────────── first_party[] / externals[]
                        │  Stage 7   select_tests * ────── tests[]
                        │  Stage 8   (include roots) ───── src/gen include roots, cxx_std
                        │  Stage 9   collect_sources ───── all_sources
                        │  Stage 10  parse_depfile ─────── headers
                        │  Stage 11  collision-check + layout  extracted/<T>/{src,include,generated}
                        │  Stage 12  write_cmakelists ──── extracted/<T>/CMakeLists.txt
                        │  Stage 13  write_readme ──────── extracted/<T>/README.md
                        └─ Stage 14  verify_build * ────── extracted/<T>/build/  (built + green)
                                          * Stage 7 needs --with-tests, Stage 14 needs --verify
```

| Stage | Function | Turns … | … into |
|------:|----------|---------|--------|
| 1  | `load_codemodel()`        | the build dir | the File API codemodel JSON |
| 2  | `load_targets()`          | the codemodel | per-target JSON + lookup maps |
| 3  | `gather_fetchcontent()` + `parse_fetchcontent()` | `CMakeLists.txt` + its `include()`s | the `FetchContent_Declare` blocks |
| 4  | `external_regions()`      | the directory tree | third-party dirs → owning dependency |
| 5  | `transitive_closure()`    | the root target id | every target id it links |
| 6  | `classify()`              | that closure | first-party targets vs external names |
| 7  | `ctest_registry()` + `select_tests()` | the CTest registry | the covering tests to carry over |
| 8  | inline in `extract()`     | the compile groups | include roots + C++ standard |
| 9  | `collect_sources()`       | target `sources[]` | the `.cpp` files to copy |
| 10 | `parse_depfile()`         | the `*.o.d` depfiles | the exact header set to copy |
| 11 | inline (uses `longest_root()`) | sources + headers | the flat `extracted/<T>/` tree |
| 12 | `write_cmakelists()`      | the collected facts | a standalone `CMakeLists.txt` |
| 13 | `write_readme()`          | the target lists | a `README.md` |
| 14 | `verify_build()`          | the extracted tree | a proof it configures, builds, passes |

---

## Stage 1 — Load the CMake File API codemodel

**Function:** `load_codemodel()`

**Purpose:** Obtain a machine-readable, authoritative description of every target
and every link edge between them. This is the ground truth for "what does `T`
depend on", replacing fragile parsing of `CMakeLists.txt` or Graphviz output.

**Input:** the build directory.

```
build/
```

**Output:** `(reply_dir, codemodel)`.

```
reply_dir = …/build/.cmake/api/v1/reply/

codemodel = {
  "paths": { "source": "…", "build": "…/build" },
  "configurations": [ {
      "targets":     [ { "name", "id", "jsonFile", "directoryIndex" }, … ],
      "directories": [ { "source", "build", "parentIndex", "childIndexes" }, … ]
  } ]
}
```

**Algorithm:**
1. Create the query stub `build/.cmake/api/v1/query/codemodel-v2` — an empty file
   whose *name* requests the codemodel object, version 2.
2. Run `cmake <build>` — a no-op reconfigure against the existing cache. Because
   the query now exists, CMake writes a reply under
   `build/.cmake/api/v1/reply/`. (This does not re-fetch `fmt`; it is already
   populated.)
3. Read the newest `index-*.json`, find the object whose `kind == "codemodel"`,
   and load the JSON file it points to.

**Note:** reply filenames are content-hashed and change on every configure, so
the index is the only stable entry point — never glob for `codemodel-*.json`
directly.

---

## Stage 2 — Index every target

**Function:** `load_targets()`

**Purpose:** Make each target's full description (type, sources, include dirs,
dependencies, artifacts) addressable three ways: by stable id (how link edges
refer to targets), by name (how the CLI and FetchContent refer to them), and by
directory index (how Stage 4 ties a target to the CMake directory that defines
it).

**Input:** the reply dir and the codemodel object.

**Output:** three dicts.

```
by_id      = { "guess::@bba4818aa150d7f5ff20": {<full target JSON>},
               "fmt::@976f4f0bee90b99ecdb6":   {…}, "rng::@…": {…}, "input::@…": {…}, … }
name_to_id = { "guess": "guess::@bba4818aa150d7f5ff20",
               "fmt": "fmt::@976f4f0bee90b99ecdb6", "rng": "rng::@…", … }
dir_of     = { "guess::@…": 4, "fmt::@…": 1, "rng::@…": 2, "input::@…": 3, … }
```

**Algorithm:** For `configurations[0]`, iterate `targets`; each entry references a
`jsonFile`. Load each file and index it under both its `id` and its `name`, and
record its `directoryIndex`.

**Note:** because the top `CMakeLists.txt` calls `include(CTest)`, the codemodel
also lists CTest's bookkeeping targets (`Nightly*`, `Experimental*`,
`Continuous*`). They are harmless here — nothing links them, so they never enter
a closure.

---

## Stage 3 — Parse the FetchContent declarations

**Function:** `gather_fetchcontent()`, then `parse_fetchcontent()`

**Purpose:** Capture, verbatim, every third-party dependency block so the emitted
`CMakeLists.txt` can reproduce it exactly — same repo, same pinned tag — instead
of copying the dependency's code.

**Input:** the top-level `CMakeLists.txt`, every file it transitively
`include()`s, and any `--deps-file` globs.

```cmake
FetchContent_Declare(
  fmt
  GIT_REPOSITORY https://github.com/fmtlib/fmt.git
  GIT_TAG        10.2.1
  GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(fmt)
```

**Output:** `fetch` — declared name → block text + default link alias.

```python
{
  "fmt": {
    "block": "FetchContent_Declare(\n  fmt\n  GIT_REPOSITORY https://github.com/fmtlib/fmt.git\n"
             "  GIT_TAG        10.2.1\n  GIT_SHALLOW    TRUE\n)",
    "link":  "fmt::fmt",
  }
}
```

**Algorithm:** `gather_fetchcontent()` reads `CMakeLists.txt`, follows each
`include(<arg>)` it can resolve to a file (a path relative to the including file
or the source root, or a `cmake/<Module>` name; a still-variable path is
skipped, not guessed), and also reads any `--deps-file` globs — then concatenates
the lot. `parse_fetchcontent()` regex-scans that text for `FetchContent_Declare(
<name> … )` blocks (`FetchContent_Declare\s*\(\s*(\w+)(.*?)\n\)`, dotall) and
stores each block unmodified plus a conventional alias `<name>::<name>`.
Over-collecting is harmless: a declaration whose name never lands in the closure
is never emitted (Stage 12).

**Note:** this runs *before* the closure walk because both Stage 4 (region
mapping) and Stage 6 (partition) need `fetch` to recognise a dependency.

---

## Stage 4 — Map the third-party filesystem regions

**Function:** `external_regions()` builds the map; `region_owner()` / `is_under()`
query it in later stages.

**Purpose:** Mark off the *regions of the filesystem* that third-party content
occupies, so no later stage can ever copy from them. This is what keeps `_deps/`
out of the extracted tree.

**Input:** the codemodel `directories[]`, plus `by_id`, `dir_of`, `fetch`,
`top_source`, `top_build`.

```
directories[]:
  0  source=.                     parent=None   children=[1..7]
  1  source=build/_deps/fmt-src    build=_deps/fmt-build   parent=0
  2  source=libs/rng               ...
  3  source=libs/input             ...
  4  source=apps/guess             ...
  ...
```

**Output:** `regions` — absolute directory → owning declaration name.

```python
{
  "…/build/_deps/fmt-src":   "fmt",   # source side
  "…/build/_deps/fmt-build": "fmt",   # build side
}
```

**Algorithm:**
- Mark a `directories[]` entry as owned by `<name>` when *either* signal fires:
  - its source dir is exactly `<build>/_deps/<name>-src`, the dir FetchContent
    populates — this also catches dependencies whose target name differs from
    the declared name; or
  - it defines a target whose `name` equals a declared `<name>` **and** it is not
    a top-level directory (a name collision at the top must never blank the whole
    tree).
- Propagate ownership to child directories, so a dependency that calls
  `add_subdirectory()` internally is covered too.
- For every owned directory, emit **both** its source path and its build path
  into `regions`.
- `region_owner(path)` returns the owner of the *longest* region root that
  contains `path`, else `None`. `is_under(path, root)` is the containment test it
  is built on.

**Why regions and not just root containment:** FetchContent populates *under the
build directory*, so `build/_deps/fmt-src/include/fmt/core.h` is "under
`top_build`" in exactly the same way a genuinely generated header is. A
containment test alone cannot separate the two, and treating `_deps/` as
generated code would freeze a partial snapshot of `fmt` into `generated/` — where
it would then *shadow* the pinned version the emitted `FetchContent` block
fetches. The directory tree is the authority that avoids this.

---

## Stage 5 — Walk the transitive target closure

**Function:** `transitive_closure()`

**Purpose:** Find every target that `T` links, directly or indirectly — the set
of libraries whose code could end up in `T`.

**Input:** the root id and `by_id`.

```
root = name_to_id["guess"] = "guess::@bba4818aa150d7f5ff20"

guess.dependencies = [ "fmt::@…", "rng::@…", "input::@…" ]
```

**Output:** a set of target ids reachable from the root.

```python
{ "guess::@bba4818aa150d7f5ff20", "fmt::@976f4f0bee90b99ecdb6",
  "rng::@56f3092fcbc80f32493b",   "input::@6d945ddea8f4ec024c33" }
```

**Algorithm:** Iterative depth-first traversal over the `dependencies[]` edges in
each target's codemodel JSON. Start with the root; repeatedly pop a target, mark
it seen, push the ids of its dependencies. Terminates when the worklist empties.

Two things this traversal deliberately does *not* reach:

- **`build_info`.** It is an `INTERFACE` library, and CMake omits such targets
  from `dependencies[]` because they impose no build ordering. Its generated
  header still reaches the output — via Stage 10, because it appears in the app's
  `.d` file. The `.d` files, not the target graph, are what guarantee generated
  headers are not missed.
- **Test targets.** The edges point `input_test → input`, never the reverse, so
  no test is reachable from an application root. Stage 7 picks them up
  separately, and only under `--with-tests`.

---

## Stage 6 — Partition the closure: first-party vs. third-party

**Function:** `classify()`

**Purpose:** Decide which closure members get their code copied (first-party) and
which get re-declared as external dependencies (third-party).

**Input:** the closure ids, `by_id`, `regions`, `fetch`, `top_source`.

**Output:** `first_party` (target JSONs) and `externals` (declaration names).

```python
first_party = [ <guess JSON>, <input JSON>, <rng JSON> ]   # sorted by id
externals   = [ "fmt" ]
```

**Algorithm:** For each closure target, look at the directory its own source
lives in:

- `region_owner(<target source dir>)` is set → **external**, owned by that name
  (`fmt`'s source dir is `…/build/_deps/fmt-src`, a region root);
- else the target `name` is a declared FetchContent name → **external**
  (fallback for a dependency declared but not yet populated);
- else → **first-party** (`guess`, `input`, `rng`).

**Note:** `build_info` is an `INTERFACE_LIBRARY` with no sources. Were it in the
closure it would land in `first_party`, but it contributes only a generated
header (picked up in Stage 10), never a `.cpp`.

---

## Stage 7 — Select the covering tests (`--with-tests` only)

**Function:** `ctest_registry()`, then `select_tests()`

**Purpose:** Carry over the tests that actually exercise the extracted code, so
the standalone tree can be *validated* and not merely compiled. Skipped entirely
without the flag.

**Input:** the build dir, `top_build`, `by_id`, and the app's `first_party` list.

```
ctest --show-only=json-v1  ->
  rng_test    command[0] = …/build/libs/rng/rng_test
  input_test  command[0] = …/build/libs/input/input_test
```

**Output:** `tests` (one record per carried-over test) and `skipped` (name +
reason).

```python
tests = [
  { "name": "input_test", "target": "input_test", "id": "input_test::@…",
    "first_party": [ <input JSON>, <input_test JSON> ], "externals": ["fmt"] },
  { "name": "rng_test",   "target": "rng_test",   "id": "rng_test::@…",
    "first_party": [ <rng JSON>, <rng_test JSON> ],   "externals": []      },
]
skipped = []
```

**Algorithm:**
1. `ctest_registry()` runs `ctest --show-only=json-v1` — CTest is the authority
   on what *is* a test; a target merely named `*_test` is not one until
   `add_test()` registers it.
2. Recover each test's target by matching `command[0]` against every target's
   `artifacts[].path` (resolved against `top_build`):
   `…/build/libs/rng/rng_test` → target `rng_test`. No naming convention is
   assumed. A test whose command is a shell script or an external program matches
   nothing and is ignored.
3. For each registered test, compute its own closure (Stage 5) and partition it
   (Stage 6). Let `needs` be its first-party targets minus itself. Carry it over
   **iff** `needs ⊆ {first-party names already in the app's closure}`.

**Why the subset rule:** it keeps the Stage 5 guarantee intact — a test can never
drag in a library the application itself does not use.

- `guess` (closure `{guess, rng, input, fmt}`) → keeps **both** `input_test`
  (needs `input`) and `rng_test` (needs `rng`).
- `greeter` (no `rng`) → keeps `input_test`, and prints
  `note: skipping test 'rng_test' -- it needs rng, not in greeter's closure`.
- `roller` (no `input`) → the mirror image: keeps `rng_test`, skips `input_test`.

A test whose `needs` is empty exercises none of the closure's code and is skipped
silently.

---

## Stage 8 — Gather include roots and the C++ standard

**Function:** inline in `extract()`

**Purpose:** Learn the `-I` roots each header lives under, so a copied header can
be placed at the *same include-relative path* and existing `#include "a/b.hpp"`
lines keep resolving with no source edits. Also capture the language standard for
the generated `CMakeLists.txt`.

**Input:** the `compileGroups[]` of every *contributing* target — the first-party
closure plus any carried-over test target.

```
guess.compileGroups[0].includes:
  …/libs/input/include
  …/build/_deps/fmt-src/include      <- dropped: inside the fmt region
  …/libs/rng/include
  …/build/generated
guess.compileGroups[0].languageStandard.standard = "17"
```

**Output:**

```python
# both lists are built from a set — order is irrelevant
src_inc_roots = [ "…/build/generated", "…/libs/input/include", "…/libs/rng/include" ]
gen_inc_roots = [ "…/build/generated" ]
cxx_std       = "17"
```

**Algorithm:** Union all `compileGroups[].includes[].path` across the contributing
targets, **dropping any root whose `region_owner` is set** — a first-party header
must never be filed relative to a dependency's include root. Then split what
remains: `src_inc_roots` is every root `is_under(root, top_source)`,
`gen_inc_roots` every root `is_under(root, top_build)`. Read
`languageStandard.standard` when present (default `"17"`).

**Note:** the two lists are *not* disjoint. Because `build/` is nested inside the
source tree, `…/build/generated` satisfies **both** tests and lands in both
lists. Stage 11 resolves the overlap by matching each header against
`gen_inc_roots` first, so a generated header is filed under `generated/`, not
`include/`.

---

## Stage 9 — Collect the first-party sources

**Function:** `collect_sources()`

**Purpose:** Enumerate the `.cpp` files that must be compiled into the standalone
target: `T`'s own sources plus those of every first-party library it links — and,
under `--with-tests`, each test's own source list.

**Input:** the `sources[]` of each first-party target, and `top_source`.

```
guess.sources  = [ { path: "apps/guess/src/main.cpp",  compileGroupIndex: 0 } ]
input.sources  = [ { path: "libs/input/src/input.cpp", compileGroupIndex: 0 } ]
rng.sources    = [ { path: "libs/rng/src/rng.cpp",     compileGroupIndex: 0 } ]
```

**Output:** `(origin_target_name, absolute_path)` pairs.

```python
app_sources = [ ("guess", "…/apps/guess/src/main.cpp"),
                ("input", "…/libs/input/src/input.cpp"),
                ("rng",   "…/libs/rng/src/rng.cpp") ]

input_test["sources"] = [ ("input",      "…/libs/input/src/input.cpp"),
                          ("input_test", "…/libs/input/test/input_test.cpp") ]
rng_test["sources"]   = [ ("rng",      "…/libs/rng/src/rng.cpp"),
                          ("rng_test", "…/libs/rng/test/rng_test.cpp") ]

all_sources = app_sources ∪ every test's sources
```

**Algorithm:** Keep each `sources[]` entry that has a non-null
`compileGroupIndex` (actually compiled, not just a header listed as a source),
resolve its `path` against `top_source`, and keep it if its extension is
`.c/.cc/.cpp/.cxx`. Retain the `origin` name so sources can be namespaced on copy
and collisions avoided.

`collect_sources()` is called once over the app's `first_party`, then once per
carried-over test over *that test's* first-party closure. A test's list therefore
includes the library `.cpp` files it used to link — necessary because Stage 11
flattens the libraries away, leaving no `input` target for a test to link
against. Those library sources are already in `all_sources`, so nothing extra is
copied; only the per-target compile lists differ.

---

## Stage 10 — Collect the precise header closure

**Function:** `parse_depfile()`

**Purpose:** Copy exactly the headers the closure's translation units *really*
included — no more. This is what makes the extraction **minimal**: a header that
exists but is never `#included` is never copied.

**Input:** the per-translation-unit `*.o.d` depfiles under each contributing
target's `.dir/`.

```
build/apps/guess/CMakeFiles/guess.dir/src/main.cpp.o.d:

  apps/guess/CMakeFiles/guess.dir/src/main.cpp.o: \
   …/apps/guess/src/main.cpp \
   …/build/_deps/fmt-src/include/fmt/color.h \
   …/build/_deps/fmt-src/include/fmt/format.h \
   …/build/_deps/fmt-src/include/fmt/core.h \
   …/build/_deps/fmt-src/include/fmt/core.h \
   …/build/generated/build_info.hpp \
   …/libs/input/include/input/input.hpp \
   …/libs/rng/include/rng/rng.hpp
```

(GCC really does list `core.h` twice; `parse_depfile()` returns a `set`, so
duplicates collapse.)

**Output:** `headers` — a set of absolute header paths.

```python
{ "…/build/generated/build_info.hpp",
  "…/libs/input/include/input/input.hpp",
  "…/libs/rng/include/rng/rng.hpp" }
```

**Algorithm:**
1. For each contributing target, glob
   `build/**/CMakeFiles/<name>.dir/**/*.o.d`. The match is `*.o.d`, not `*.d`:
   CMake ≥ 4.0 also writes a link-step depfile `link.d` into the same `.dir/`,
   listing object files and libraries rather than headers (TUTORIAL.md Trap 5).
2. `parse_depfile()` reads the Make-syntax file: join `\`-newline continuations,
   drop the target before the first `:`, split the rest on whitespace.
3. Resolve each prerequisite to an absolute path and drop sources
   (`.c/.cc/.cpp/.cxx`) → drops `main.cpp`.
4. Drop anything inside a third-party region (Stage 4) → drops the three
   `fmt/*.h`; `fmt`'s headers return through its `FetchContent` block, never as
   copies.
5. Keep what remains only if it lies under `top_source` (first-party) or
   `top_build` (generated) → drops any `/usr/...` system header.

Checks 4 and 5 are both required, **in that order**: `build/_deps/fmt-src/...`
*is* under `top_build`, so check 5 alone would misfile a dependency's headers as
generated code.

---

## Stage 11 — Lay out the flat extracted tree

**Function:** inline in `extract()` (uses `longest_root()`)

**Purpose:** Materialise a clean, flat directory whose layout preserves every
include path, so the copied sources compile unmodified.

**Input:** `all_sources`, `headers`, `src_inc_roots`, `gen_inc_roots`, the output
root.

**Output:** files on disk under `extracted/guess/`, plus the bookkeeping the next
stage needs.

```
extracted/guess/
├── src/
│   ├── guess/main.cpp
│   ├── input/input.cpp
│   ├── rng/rng.cpp
│   ├── input_test/input_test.cpp
│   └── rng_test/rng_test.cpp
├── include/
│   ├── input/input.hpp
│   └── rng/rng.hpp
└── generated/
    └── build_info.hpp

cmake_sources            = [ "src/guess/main.cpp", "src/input/input.cpp", "src/rng/rng.cpp" ]
input_test.cmake_sources = [ "src/input/input.cpp", "src/input_test/input_test.cpp" ]
rng_test.cmake_sources   = [ "src/rng/rng.cpp", "src/rng_test/rng_test.cpp" ]
used_include = True   used_generated = True
```

**Algorithm:**
- **Collision check (first, before any write):** group `all_sources` by their
  target destination `src/<origin>/<basename>`. If two different source paths
  map to one destination, print every clash and `sys.exit` — unless
  `--allow-collisions`, which downgrades it to a warning and lets the last write
  win. On the sample nothing collides.
- Remove any prior `extracted/guess/` and create `src/`.
- **Sources:** copy each to `src/<origin>/<basename>` and record its
  output-relative path. `cmake_sources` is that path for each of `app_sources`;
  each test record gets its own from its own source list.
- **Headers:** for each header, pick the destination by the *longest matching
  include root* (`longest_root()`):
  - try `gen_inc_roots` (build tree) **first** → `generated/<relpath>`, set
    `used_generated` (`…/build/generated` + `build_info.hpp` →
    `generated/build_info.hpp`). Checking the build root first is what keeps
    generated headers out of `include/` despite the in-source build dir.
  - else try `src_inc_roots` → `include/<relpath>`, set `used_include`
    (`…/libs/input/include` + `input/input.hpp` → `include/input/input.hpp`).
  - if no root matches, fall back to `include/<basename>` and warn.

The result is `src/` (flattened, namespaced by origin), `include/` (headers at
their original include-relative path), and `generated/` (frozen generated
headers).

---

## Stage 12 — Emit the standalone `CMakeLists.txt`

**Function:** `write_cmakelists()`

**Purpose:** Produce a self-contained build script that needs nothing from the
parent project.

**Input:** the target name, `cxx_std`, `cmake_sources`, the `used_include` /
`used_generated` flags, `fetch`, the union of external names (app + every carried
test), and the `tests` records.

**Output:** `extracted/guess/CMakeLists.txt`.

```cmake
cmake_minimum_required(VERSION 3.20)
project(guess_standalone LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(
  fmt
  GIT_REPOSITORY https://github.com/fmtlib/fmt.git
  GIT_TAG        10.2.1
  GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(fmt)

add_executable(guess
  src/guess/main.cpp
  src/input/input.cpp
  src/rng/rng.cpp
)
target_include_directories(guess PRIVATE include generated)
target_link_libraries(guess PRIVATE fmt::fmt)

enable_testing()

add_executable(input_test
  src/input/input.cpp
  src/input_test/input_test.cpp
)
target_include_directories(input_test PRIVATE include generated)
target_link_libraries(input_test PRIVATE fmt::fmt)
add_test(NAME input_test COMMAND input_test)

add_executable(rng_test
  src/rng/rng.cpp
  src/rng_test/rng_test.cpp
)
target_include_directories(rng_test PRIVATE include generated)
add_test(NAME rng_test COMMAND rng_test)
```

**Algorithm:** Assemble the file textually:
1. `cmake_minimum_required` + `project(<T>_standalone)` + the captured C++
   standard.
2. If there are externals: `include(FetchContent)`, then each captured
   `FetchContent_Declare(...)` block *verbatim*, then one
   `FetchContent_MakeAvailable(<names>)`. The name set is the union of the app's
   externals and every carried-over test's, since a test may use a dependency the
   app does not.
3. `add_executable(<T> …)` over `cmake_sources`, then
   `target_include_directories(<T> PRIVATE include generated)` — only the dirs
   that actually received files — then `target_link_libraries(<T> PRIVATE …)`
   linking each external via its `<name>::<name>` alias.
4. If tests were carried over: `enable_testing()`, then for each test the same
   executable triple with *its own* source list and *its own* externals, followed
   by `add_test(NAME <registered name> COMMAND <target>)`.

**Note on per-test externals:** `input_test` gets
`target_link_libraries(input_test PRIVATE fmt::fmt)` because `input` links `fmt`;
`rng_test` gets no `target_link_libraries` at all because nothing in its closure
reaches `fmt`. The registered CTest name is preserved even where it differs from
the target name.

---

## Stage 13 — Emit the README

**Function:** `write_readme()`

**Purpose:** Leave a short human description of what the extracted tree is and how
to build it.

**Input:** the target name, the `first_party` list, the external names, the
`tests` records.

**Output:** `extracted/guess/README.md`.

~~~markdown
# guess (extracted standalone closure)

Minimal build closure for `guess`, extracted from the parent CMake project into a flat, standalone tree.

- First-party targets folded in: guess, input, rng
- Third-party deps (via FetchContent): fmt
- Tests carried over: input_test, rng_test

## Build

```sh
cmake -S . -B build
cmake --build build -j
```

## Test

```sh
ctest --test-dir build
```
~~~

---

## Stage 14 — Verify by building and testing (`--verify` only)

**Function:** `verify_build()`

**Purpose:** Prove the extracted tree really is standalone and buildable — and,
when tests came along, that it still behaves.

**Input:** the output directory.

**Output:** a configured + built `extracted/guess/build/` with `ctest` green, or a
non-zero exit on the first failure.

```
--- verifying build of extracted 'guess' ---
--- OK: …/extracted/guess/build built and tested successfully ---
```

**Algorithm:** Run `cmake -S <out> -B <out>/build`, then
`cmake --build <out>/build -j`, then — when Stage 7 carried tests over —
`ctest --output-on-failure` in that build dir. Every step is `check=True`, so a
clean run is a hard guarantee.

Building alone proves the header closure resolved. Running the tests raises the
claim from *the closure compiles standalone* to *the closure passes its own tests
standalone* — the stronger statement, since compiling never exercises the code
that came along for the ride.

---

## Why the output is both minimal and standalone

- **Minimal** — sources come only from the transitively-linked first-party
  targets (Stages 5, 9), and headers come only from what the compiler recorded as
  actually included (Stage 10). Unused files never enter the tree, so different
  apps yield different closures (`roller` has no `input`; `greeter` has no
  `rng`). `--with-tests` does not weaken this: a test is carried over only when
  the code it links is already present (Stage 7), so the set of libraries in the
  tree is identical with or without the flag.
- **Standalone** — first-party code is copied with its include structure intact
  (Stage 11), generated code is frozen in place (Stages 10, 11), and third-party
  code is reproduced through the project's own FetchContent declarations
  (Stages 3, 4, 12). Nothing points back at the parent repo. When a target has no
  third-party dependency at all (`tally`), Stage 12 emits neither the FetchContent
  section nor any `target_link_libraries`, and the extracted tree then configures
  and builds with no network access whatsoever.
