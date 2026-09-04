# Extraction algorithm

> Reference documentation. If you are meeting this pipeline for the first time,
> read [TUTORIAL.md](TUTORIAL.md) instead — it teaches the underlying APIs
> hands-on and covers the traps. Come back here for stage-by-stage detail.
> Any term used below is defined in [GLOSSARY.md](GLOSSARY.md).

This document walks `tools/extract_closure.py` **in the order it executes**. The
`extract()` function is the spine of the extractor; every other function is
described here at the point `extract()` first calls it. For each stage you get
its **purpose**, a concrete **input** and **output**, and the **algorithm** in
between.

The goal of the whole pipeline: given one application target `T`, produce a
standalone, buildable directory containing exactly `T`'s minimal build closure
(first-party sources + headers + generated code, with the original directory
structure preserved), and third-party dependencies re-declared rather than
copied.

## The running example

Every stage below is illustrated with one real run against the `samples/basic/`
project, from inside that directory:

```sh
python3 ../../tools/extract_closure.py guess --with-tests --verify
```

`guess` links the first-party libraries `rng` and `input`, the generated
`build_info` header, and the third-party library `fmt`. Both flags are on, so
every optional stage runs too. Paths are abbreviated with `…` for the sample
source root `…/samples/basic` (and `…/samples/basic/build` for the build tree).

## Prerequisites (what the build must already have produced)

The extractor is a *consumer* of an already-configured, already-built tree.
Before it runs:

- The project is configured into a build dir (default `build/`).
- The build was compiled with `-MMD` so a per-translation-unit `.d` depfile
  sits next to every object file under
  `build/**/CMakeFiles/<target>.dir/`.
- CMake is 3.21 or newer (Stage 1 re-runs configure under `--trace-redirect`).

No source file is ever parsed by hand; every fact comes from CMake or the
compiler.

Every command-line flag, its default and a set of worked invocations are in
[README.md § Command-line reference](README.md#command-line-reference); this
document notes each flag again at the stage it gates.

## Execution order at a glance

The extractor consumes four facts the configured build already produced: the
File API codemodel, the command trace, the `-MMD` `*.o.d` depfiles, and the CTest
registry.

```
  Stage 1   load_codemodel ────── File API reply + command trace
  Stage 2   load_targets ──────── by_id / name_to_id / dir_of
  Stage 3   load_trace ────────── fetch{} / find_pkgs{} / link_tokens{}
  Stage 4   external_regions ──── regions{}  (third-party directories)
  Stage 5   transitive_closure ── closure of target ids
  Stage 6   classify ──────────── first_party[] / externals[]
  Stage 7   select_tests * ────── tests[]
  Stage 8   (include roots) ───── source / generated include roots, cxx_std
  Stage 9   collect_sources ───── all_sources
  Stage 10  parse_depfile ─────── headers_by_origin
  Stage 11  place + collision-check + copy  extracted/<T>/{src,include,generated}
  Stage 12  write_cmakelists ──── extracted/<T>/CMakeLists.txt
  Stage 13  write_readme ──────── extracted/<T>/README.md
  Stage 14  verify_build * ────── extracted/<T>/build/  (built + green)

  * Stage 7 needs --with-tests, Stage 14 needs --verify
```

| Stage | Function | Turns … | … into |
|------:|----------|---------|--------|
| 1  | `load_codemodel()`        | the build dir | the File API codemodel + the command trace |
| 2  | `load_targets()`          | the codemodel | per-target JSON + lookup maps |
| 3  | `load_trace()` + `gather_fetchcontent()` / `traced_find_packages()` / `traced_link_tokens()` | the command trace | `FetchContent_Declare` blocks, `find_package()` calls, per-target link tokens |
| 4  | `external_regions()`      | the directory tree | third-party dirs → owning dependency |
| 5  | `transitive_closure()`    | the root target id | every target id it links |
| 6  | `classify()`              | that closure | first-party targets vs third-party names |
| 7  | `ctest_registry()` + `select_tests()` | the CTest registry | the covering tests to carry over |
| 8  | inline in `extract()`     | the compile groups | include roots + C++ standard |
| 9  | `collect_sources()`       | target `sources[]` | the `.cpp` files to copy |
| 10 | `parse_depfile()`         | the `*.o.d` depfiles | the exact header set to copy, per origin |
| 11 | inline (uses `longest_root()`) | sources + headers | the `extracted/<T>/` tree, structure preserved |
| 12 | `write_cmakelists()`      | the collected facts | a standalone `CMakeLists.txt` |
| 13 | `write_readme()`          | the target lists | a `README.md` |
| 14 | `verify_build()`          | the extracted tree | a proof it configures, builds, passes |

---

## Stage 1 — Load the codemodel and the command trace

**Function:** `load_codemodel()`

**Purpose:** Obtain two machine-readable, authoritative accounts of the configured
build: the **File API codemodel** (every target and every link edge — the ground
truth for "what does `T` depend on") and the **command trace** (every CMake
command that ran, with its arguments — the ground truth for the dependency
declarations). Both replace fragile parsing of `CMakeLists.txt` or Graphviz
output.

**Input:** the build directory.

```
build/
```

**Output:** `(reply_dir, codemodel, trace_file)`.

```
reply_dir  = …/build/.cmake/api/v1/reply/
trace_file = …/build/.cmake/extract-trace.json   (the command trace: JSON,
                                                  one trace record per line)

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
2. Run `cmake <build> --trace-expand --trace-format=json-v1
   --trace-redirect=<trace_file>` — a no-op reconfigure against the existing
   cache. Because the query now exists, CMake writes a reply under
   `build/.cmake/api/v1/reply/`; the trace flags additionally write one trace
   record per command invocation (arguments already variable-expanded) to
   `trace_file`. (This does not re-fetch `fmt`; it is already populated. A no-op
   reconfigure still re-runs every `CMakeLists.txt`, so the trace is complete
   each time.)
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

## Stage 3 — Recover the third-party dependencies from the command trace

**Function:** `load_trace()`, then `gather_fetchcontent()`,
`traced_find_packages()`, `traced_link_tokens()`

**Purpose:** Learn every third-party dependency the project actually pulls in, and
how each target links it, so the emitted `CMakeLists.txt` can reproduce the
dependency setup — same repo, same pinned tag, same `find_package()` call — without
copying the dependency's code.

**Input:** the command trace from Stage 1. Each trace record is one command CMake
ran, as `{"cmd", "args", "file", "line"}`:

```json
{"cmd":"FetchContent_Declare","args":["fmt","GIT_REPOSITORY","https://github.com/fmtlib/fmt.git","GIT_TAG","10.2.1","GIT_SHALLOW","TRUE"],"file":".../CMakeLists.txt","line":12}
{"cmd":"target_link_libraries","args":["guess","PRIVATE","input","rng","fmt::fmt","build_info"],"file":".../apps/guess/CMakeLists.txt","line":2}
{"cmd":"find_package","args":["Threads","REQUIRED"],"file":".../CMakeLists.txt","line":4}
```

`args` arrives already variable-expanded, with every `if()` / `foreach()` /
`function()` resolved, so `FetchContent_Declare(${dep} ...)`, a declaration inside
a wrapper function, and the `FetchContent_Declare` calls CPM and similar wrappers
synthesise internally are all visible — none of which a text scan of
`CMakeLists.txt` can follow.

**Output:** three structures.

```python
fetch = {                         # one entry per FetchContent dependency
  "fmt": {
    "block": "FetchContent_Declare(\n  fmt\n  GIT_REPOSITORY https://github.com/fmtlib/fmt.git\n"
             "  GIT_TAG 10.2.1\n  GIT_SHALLOW TRUE\n)",   # regenerated from args
    "link":  "fmt::fmt",                                  # imported-target name (fallback)
  }
}
find_pkgs   = { "Threads": "find_package(Threads REQUIRED)" }   # re-emitted verbatim
link_tokens = { "guess": ["input", "rng", "fmt::fmt", "build_info"], ... }
```

**Algorithm:**
- `load_trace()` reads the trace, skips the `{"version": ...}` header line, and
  keeps only `FetchContent_Declare` / `find_package` / `target_link_libraries`
  records (command names matched case-insensitively).
- A record is kept only when it comes from **the project's own CMake code** — its
  `file` is under the source root and outside every third-party region (Stage 4).
  Records from CMake's bundled modules (`find_package(Git)` inside
  `FetchContent.cmake`) or from a dependency's own `CMakeLists.txt` are dropped.
- `gather_fetchcontent()` **regenerates** a `FetchContent_Declare(...)` block from
  each kept declaration's argument list — one `KEYWORD value...` group per line.
  Formatting and comments from the original are lost; the arguments are exactly
  what CMake received. The conventional imported-target name `<name>::<name>` is
  stored as a link-line fallback.
- `traced_find_packages()` **re-emits** each kept `find_package()` call verbatim.
  The extracted tree cannot recreate these — the host toolchain must provide them
  — but the call is kept so the build stays honest about what it needs.
- `traced_link_tokens()` records, per target, its **link tokens**: the libraries
  it names in `target_link_libraries()` (keywords like `PRIVATE` removed),
  accumulated across every call. Calls on a name that is not a known project
  target are ignored, which drops `try_compile()`'s scratch targets.

**`--deps-file`:** a rarely-needed escape hatch. The globbed files are
text-scanned by `parse_fetchcontent()` (regex `FetchContent_Declare\s*\(\s*(\w+)
(.*?)\n\)`, dotall) and merged into `fetch`. Use it only for a declaration
guarded behind an `if()` the trace never enters.

**Note:** the FetchContent *names* are collected first (without the region
filter) because Stage 4 needs them before regions exist; the blocks, the
`find_package` calls and the link tokens are then filtered against the regions
Stage 4 produces.

---

## Stage 4 — Map the third-party filesystem regions

**Function:** `external_regions()` builds the map; `region_owner()` / `is_under()`
query it in later stages.

**Purpose:** Mark off the *regions of the filesystem* that third-party content
occupies, so no later stage can ever copy from them. This is what keeps `_deps/`
out of the extracted tree.

**Input:** the codemodel `directories[]`, plus `by_id`, `dir_of`, the set of
declared FetchContent names, `top_source`, `top_build`.

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
which get re-declared (third-party).

**Input:** the closure ids, `by_id`, `regions`, `fetch`, `top_source`.

**Output:** `first_party` (target JSONs) and `externals` (the third-party
dependency names — the list is named `externals` in the code).

```python
first_party = [ <guess JSON>, <input JSON>, <rng JSON> ]   # sorted by id
externals   = [ "fmt" ]
```

**Algorithm:** For each closure target, look at the directory its own source
lives in:

- `region_owner(<target source dir>)` is set → **third-party**, owned by that
  name (`fmt`'s source dir is `…/build/_deps/fmt-src`, a region root);
- else the target `name` is a declared FetchContent name → **third-party**
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

**Purpose:** Learn the `-I` roots the headers live under, so Stage 11 can place
each **public** header at the *same include-relative path* and existing
`#include <a/b.hpp>` lines keep resolving with no source edits. Also capture the
language standard for the generated `CMakeLists.txt`.

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
must never be placed relative to a dependency's include root. Then split what
remains: `src_inc_roots` is every root `is_under(root, top_source)`,
`gen_inc_roots` every root `is_under(root, top_build)`. Read
`languageStandard.standard` when present (default `"17"`).

**Note:** the two lists are *not* disjoint. Because `build/` is nested inside the
source tree, `…/build/generated` satisfies **both** tests and lands in both
lists. Stage 11 resolves the overlap by matching each header against
`gen_inc_roots` first, so a generated header is placed under `generated/`, not
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
`.c/.cc/.cpp/.cxx`. Retain the `origin` name so Stage 11 can place each source
relative to that target's directory, under `src/<origin>/`.

`collect_sources()` is called once over the app's `first_party`, then once per
carried-over test over *that test's* first-party closure. A test's list therefore
includes the library `.cpp` files it used to link — necessary because the
libraries are folded in, leaving no `input` target for a test to link against.
Those library sources are already in `all_sources`, so nothing extra is copied;
only the per-target compile lists differ.

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

**Output:** `headers_by_origin` — absolute header paths, keyed by the origin
target whose translation units pulled them (Stage 11 uses that key to place a
private header next to the right target's sources).

```python
{ "guess": { "…/build/generated/build_info.hpp",
             "…/libs/input/include/input/input.hpp",
             "…/libs/rng/include/rng/rng.hpp" },
  "input_test": { … }, "rng_test": { … } }
```

**Algorithm:**
1. For each contributing target, glob
   `build/**/CMakeFiles/<name>.dir/**/*.o.d`. The match is `*.o.d`, not `*.d`:
   CMake ≥ 4.0 also writes a link-step depfile `link.d` into the same `.dir/`,
   listing object files and libraries rather than headers (TUTORIAL.md Trap 6).
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

## Stage 11 — Place every file, collision-check, then copy

**Function:** inline in `extract()` (uses `longest_root()`)

**Purpose:** Give every source and header a path in the extracted tree that keeps
its `#include`s resolving — angle-bracket includes via `-I include` /
`-I generated`, file-relative quote includes because siblings stay siblings — so
the copied code compiles unmodified.

**Input:** `all_sources`, `headers_by_origin`, `src_inc_roots`, `gen_inc_roots`,
`origin_dir` (each target name → its own source directory), the output root.

**Output:** files on disk under `extracted/guess/`, plus the bookkeeping the next
stage needs.

```
extracted/guess/
├── src/
│   ├── guess/src/main.cpp
│   ├── input/src/input.cpp
│   ├── rng/src/rng.cpp
│   ├── input_test/test/input_test.cpp
│   └── rng_test/test/rng_test.cpp
├── include/
│   ├── input/input.hpp
│   └── rng/rng.hpp
└── generated/
    └── build_info.hpp

cmake_sources            = [ "src/guess/src/main.cpp", "src/input/src/input.cpp", "src/rng/src/rng.cpp" ]
input_test.cmake_sources = [ "src/input/src/input.cpp", "src/input_test/test/input_test.cpp" ]
rng_test.cmake_sources   = [ "src/rng/src/rng.cpp", "src/rng_test/test/rng_test.cpp" ]
used_include = True   used_generated = True
```

**Algorithm:** the structure is *preserved*, not flattened. Two placement
functions decide each file's path, keeping it relative to a meaningful root:

- **`place_source(path, origin)`** — a source keeps its path relative to its
  **origin** target's directory, under the one-level `src/<origin>/` namespace:
  `libs/rng/src/rng.cpp` (origin `rng`) → `src/rng/src/rng.cpp`. A generated
  source (under the build tree) goes to `generated/<relpath>`. A source listed
  from outside its target's directory falls back to `src/<origin>/<basename>`
  with a warning.
- **`place_header(path, origin)`** — a **public** header (one under an include
  root, `src_inc_roots`) keeps its include-relative path under `include/`:
  `libs/input/include/input/input.hpp` → `include/input/input.hpp`. A generated
  header → `generated/<relpath>` (checked first, so an in-source build dir does
  not misplace it into `include/`). A **private** header — under no include
  root — is placed beside the sources that include it, at
  `src/<origin>/<relpath>`, so a file-relative `#include "internal.hpp"` still
  resolves; one pulled by two targets is placed under each.

Then: **collision-check** — if two different files are placed at one path, print
each collision and `sys.exit` unless `--allow-collisions` (now rare, since
preserved paths seldom coincide — the usual case is two libraries exposing the
same `include/<path>.hpp`). Finally remove any prior `extracted/guess/`, copy
every file to its placed path, and record each source's path into
`cmake_sources` (and each test's own list). `used_include` / `used_generated`
are set from whether anything landed under those directories.

---

## Stage 12 — Emit the standalone `CMakeLists.txt`

**Function:** `write_cmakelists()`

**Purpose:** Produce a standalone build script that needs nothing from the
source project.

**Input:** the target name, `cxx_std`, `cmake_sources`, the `used_include` /
`used_generated` flags, `fetch`, the FetchContent names to declare, the
`find_package(...)` calls to re-emit, `link_lines` (the link line for each
executable), and the `tests` records.

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
  GIT_TAG 10.2.1
  GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(fmt)

add_executable(guess
  src/guess/src/main.cpp
  src/input/src/input.cpp
  src/rng/src/rng.cpp
)
target_include_directories(guess PRIVATE include generated)
target_link_libraries(guess PRIVATE fmt::fmt)

enable_testing()

add_executable(input_test
  src/input/src/input.cpp
  src/input_test/test/input_test.cpp
)
target_include_directories(input_test PRIVATE include generated)
target_link_libraries(input_test PRIVATE fmt::fmt)
add_test(NAME input_test COMMAND input_test)

add_executable(rng_test
  src/rng/src/rng.cpp
  src/rng_test/test/rng_test.cpp
)
target_include_directories(rng_test PRIVATE include generated)
add_test(NAME rng_test COMMAND rng_test)
```

**Algorithm:** Assemble the file textually:
1. `cmake_minimum_required` + `project(<T>_standalone)` + the captured C++
   standard.
2. If there are `find_package` dependencies: each `find_package(...)` call
   *verbatim*, under a comment noting the host toolchain must provide them.
3. If there are FetchContent dependencies: `include(FetchContent)`, then each
   regenerated `FetchContent_Declare(...)` block, then one
   `FetchContent_MakeAvailable(<names>)`. The name set is the union of every
   dependency reachable in the target graph from the app or a carried-over test,
   plus any named on a link line.
4. `add_executable(<T> …)` over `cmake_sources`, then
   `target_include_directories(<T> PRIVATE include generated)` — only the dirs
   that actually received files — then `target_link_libraries(<T> PRIVATE …)`
   from `link_lines[<T>]`.
5. If tests were carried over: `enable_testing()`, then for each test the same
   executable triple with *its own* source list and *its own* link line, followed
   by `add_test(NAME <registered name> COMMAND <target>)`.

**How a link line is built (inline in `extract()`):** the first-party libraries
are folded into the executable, so every link edge they carried to a third-party
dependency has to be re-homed onto the executable. For each executable, walk it
*and* every first-party library folded into it, and keep each link token whose
base name (before `::`) is a FetchContent or a `find_package` dependency. A
FetchContent dependency that is in the target graph but that no link token
happened to name (linked through a generator expression, say) still gets its
`<name>::<name>` imported-target name, as a safety net.

**Worked example:** `input_test` gets
`target_link_libraries(input_test PRIVATE fmt::fmt)` because `input`, folded into
it, links `fmt::fmt`; `rng_test` gets no `target_link_libraries` at all because
nothing folded into it links a third-party dependency. The registered CTest name
is preserved even where it differs from the target name.

---

## Stage 13 — Emit the README

**Function:** `write_readme()`

**Purpose:** Leave a short human description of what the extracted tree is and how
to build it.

**Input:** the target name, the `first_party` list, the FetchContent names, the
`find_package` names, the `tests` records.

**Output:** `extracted/guess/README.md`.

~~~markdown
# guess (extracted standalone closure)

Minimal build closure for `guess`, extracted from its source CMake project into a standalone tree.

- First-party targets folded in: guess, input, rng
- Third-party dependencies (via FetchContent): fmt
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

A `find_package` dependency, when there is one, adds a
`Provided by the host toolchain (find_package): …` line to the list.

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
  code is reproduced from the commands the project actually ran — a regenerated
  `FetchContent_Declare`, a re-emitted `find_package`, and link lines built from
  the traced `target_link_libraries` (Stages 3, 4, 12). Nothing points back at
  the source project. When a target has no third-party dependency at all
  (`tally`), Stage 12 emits neither the FetchContent section nor any
  `target_link_libraries`, and the extracted tree then configures and builds with
  no network access whatsoever.
