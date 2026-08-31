# Extraction algorithm

This document describes `tools/extract_closure.py` stage by stage: for each step,
its **purpose**, its **input**, its **output**, and the **actual algorithm** it
runs. The goal of the whole pipeline is: given one application target `T`,
produce a flat, standalone, buildable directory containing exactly `T`'s minimal
code closure (first-party sources + headers + generated code), with third-party
dependencies kept as FetchContent rather than copied.

## Prerequisites (what the build must have produced)

The extractor is a *consumer* of an already-configured/built tree. Before it runs:

- The project is configured into a build dir (default `build/`).
- The build was compiled with `-MMD` so per-translation-unit `.d` header
  dependency files exist under `build/**/CMakeFiles/<target>.dir/`.

No source files are parsed by hand; every fact comes from CMake or the compiler.

```
                ┌──────────────────────────────────────────────┐
   build/  ────▶│  1. codemodel   2. targets     3. closure     │
   src/    ────▶│  4. partition  4b. tests*      5. roots       │──▶ extracted/<T>/
   -MMD .d ────▶│  6. sources     7. headers     8. layout      │      (standalone)
   ctest   ────▶│  9. CMakeLists 10. README     11. verify*     │
                └──────────────────────────────────────────────┘
                                        * optional (--with-tests / --verify)
```

---

## Stage 1 — Load the CMake File API codemodel

**Function:** `load_codemodel()`

**Purpose:** Obtain a machine-readable, authoritative description of every target
and the link edges between them. This is the ground truth for "what does `T`
depend on", replacing fragile parsing of `CMakeLists.txt` or Graphviz text.

**Input:** the build directory.

**Output:** `(reply_dir, codemodel_json)` — the path to the File API reply
directory and the parsed top-level codemodel object (which contains
`paths.source`, `paths.build`, and the list of targets for each configuration).

**Algorithm:**
1. Create the query stub `build/.cmake/api/v1/query/codemodel-v2` (an empty file
   whose *name* requests the codemodel object, version 2).
2. Run `cmake <build>` — a no-op reconfigure using the existing cache. Because
   the query now exists, CMake writes a reply under
   `build/.cmake/api/v1/reply/`. (This does not re-fetch `fmt`; it is already
   populated.)
3. Read the newest `index-*.json`, find the object whose `kind == "codemodel"`,
   and load the JSON file it points to.

---

## Stage 2 — Load per-target JSON and build lookup maps

**Function:** `load_targets()`

**Purpose:** Make each target's full description (type, sources, include dirs,
dependencies) addressable by id and by name.

**Input:** the reply dir and the codemodel object.

**Output:** three dicts — `by_id: id → target_json`, `name_to_id: name → id`, and
`dir_of: id → directory index`.

**Algorithm:** For the first configuration, iterate `configurations[0].targets`;
each entry references a `jsonFile`. Load each file and index it under both its
stable `id` (used by dependency edges) and its human `name` (used by the CLI).
Record each target's `directoryIndex` too — Stage 4 needs it to tie a target to
the CMake directory that defines it.

---

## Stage 3 — Compute the transitive target closure

**Function:** `transitive_closure()`

**Purpose:** Find every target that `T` links, directly or indirectly — the set
of libraries whose code could end up in `T`.

**Input:** the root target id (`name_to_id[T]`) and `by_id`.

**Output:** a set of target ids reachable from `T`.

**Algorithm:** Iterative depth-first graph traversal over the `dependencies[]`
edges in each target's codemodel JSON. Start with `T`; repeatedly pop a target,
mark it seen, and push the ids of all its dependencies. Terminates when the
worklist empties. For `guess` this yields `{guess, input, rng, fmt}`.

Two things this traversal deliberately does *not* reach:

- **`build_info`.** It is an `INTERFACE` library, and CMake omits such targets
  from `dependencies[]` because they impose no build ordering. Its generated
  header still reaches the output — via Stage 7, because it appears in the apps'
  `.d` files. The `.d` files, not the target graph, are what guarantee generated
  headers are not missed.
- **Test targets.** The edges point `input_test → input`, never the reverse, so
  no test is reachable from an application root. Stage 4b picks them up
  separately, and only under `--with-tests`.

---

## Stage 4 — Partition first-party vs. third-party

**Function:** `extract()` (uses `parse_fetchcontent()`, `external_regions()`,
`region_owner()`)

**Purpose:** Decide which closure members get their code copied (first-party) and
which get re-declared as external dependencies (third-party) — and, just as
importantly, mark off the *regions of the filesystem* that third-party content
occupies, so later stages can never copy from them.

**Input:** the closure targets; the codemodel's directory graph; the set of
`FetchContent_Declare` names parsed from the top-level `CMakeLists.txt`.

**Output:** `first_party` (target JSONs), `externals` (declaration names), and
`regions` — a map of absolute directory → owning declaration.

**Algorithm:**
- `parse_fetchcontent()` regex-scans the top `CMakeLists.txt` for
  `FetchContent_Declare(<name> … )` blocks, recording for each the *verbatim
  block text* and a default link alias `<name>::<name>`.
- `external_regions()` walks `configurations[0].directories[]` and marks a
  directory third-party when either signal fires:
  - it defines a target named after a declaration (`fmt` → target `fmt`), or
  - it is the population dir FetchContent creates at `<build>/_deps/<name>-src`
    — which also catches deps whose target names differ from the declared name.

  Child directories inherit their parent's owner, so a dependency that calls
  `add_subdirectory()` internally is covered. A top-level directory is never
  marked, so a name collision there cannot blank out the whole tree. Each marked
  directory contributes **both** its source and its build path to `regions`.
- A closure target is **external** iff its own directory lies in a region (or,
  as a fallback for a declared-but-unpopulated dep, its `name` is a declaration);
  otherwise it is **first-party**.
- Note: `build_info` is an `INTERFACE_LIBRARY` with no sources; it is first-party
  but contributes only a generated header (picked up in Stage 7), not `.cpp`s.

**Why regions and not just root containment:** FetchContent populates *under the
build directory*, so `build/_deps/fmt-src/include/fmt/core.h` is "under
`top_build`" in exactly the same way a genuinely generated header is. Containment
tests alone therefore cannot separate the two, and treating `_deps/` as generated
code would copy a partial, frozen snapshot of the dependency into `generated/` —
where it would then *shadow* the pinned version the emitted `FetchContent` block
fetches. The directory graph is the authority that avoids this.

---

## Stage 4b — Select tests (`--with-tests` only)

**Function:** `ctest_registry()`, `select_tests()`

**Purpose:** Carry over the tests that actually exercise the extracted code, so
the standalone tree can be *validated* and not merely compiled. Skipped entirely
without the flag, leaving the default output application-only.

**Input:** the configured build dir; `by_id`; the app's `first_party` list.

**Output:** `tests` — one record per carried-over test (registered name, target
name, its own first-party closure, its own externals) — and a list of skipped
tests with the reason.

**Algorithm:**
1. `ctest_registry()` runs `ctest --show-only=json-v1` in the build dir. CTest is
   the authority on what *is* a test; a target merely named `*_test` is not one
   until `add_test()` registers it.
2. Recover each test's target by matching `command[0]` against the targets'
   `artifacts[].path` (resolved against `top_build`). This assumes no naming
   convention, so a test target called anything at all is still found. Tests
   whose command is a script or an external program match nothing and are
   ignored.
3. For each registered test, compute its own closure (Stage 3) and partition it
   (Stage 4). Carry it over **iff** every first-party target it links, minus
   itself, is already in the app's `first_party` set.

That subset rule is what keeps the guarantee from Stage 3 intact: a test can
never introduce a library the application itself does not use. `greeter` gets
`input_test` but not `rng_test`; `roller` the reverse; `guess` both. A test that
fails the rule is reported on stderr rather than dropped silently. A test with no
first-party dependencies exercises none of the closure's code and is skipped.

---

## Stage 5 — Gather include roots and the C++ standard

**Function:** `extract()`

**Purpose:** Learn the `-I` roots each header lives under, so a copied header can
be placed at the *same include-relative path* and existing `#include "a/b.hpp"`
lines keep resolving without editing sources. Also capture the language standard
for the generated CMakeLists.

**Input:** the `compileGroups[]` of every first-party target.

**Output:**
- `src_inc_roots` — include roots under the project source tree.
- `gen_inc_roots` — include roots under the build tree (generated headers).
- `cxx_std` — e.g. `"17"`.

**Algorithm:** Union all `compileGroups[].includes[].path` across the
*contributing* targets — the first-party closure plus any carried-over test
targets — **skipping any root inside a third-party region** (Stage 4): a
first-party header must never be placed relative to a dependency's include root.
Partition what remains by `is_under(root, top_source)` vs
`is_under(root, top_build)`. Read `languageStandard.standard` when present.
(Because the build dir is nested in the source tree, a build-tree root is *also*
under the source root — Stage 8 resolves the ambiguity by checking the build
root first.)

---

## Stage 6 — Collect first-party sources

**Function:** `extract()` (uses `collect_sources()`)

**Purpose:** Enumerate the `.cpp` files that must be compiled into the standalone
target: `T`'s own sources plus those of every first-party library it links — and,
under `--with-tests`, the per-test source lists as well.

**Input:** the `sources[]` of each first-party target; `top_source`.

**Output:** `app_sources` (list of `(origin_target_name, absolute_path)` pairs),
a per-test source list, and `all_sources` — their union, which is what gets
copied.

**Algorithm:** `collect_sources()` takes each `sources[]` entry with a non-null
`compileGroupIndex` (i.e. actually compiled, not merely a header listed as a
source), resolves its `path` against `top_source`, and keeps it if its extension
is a C/C++ source (`.c/.cc/.cpp/.cxx`). The `origin` name is retained so sources
can be namespaced on copy and collisions avoided.

It is called once over the app's `first_party`, then once per carried-over test
over *that test's* first-party closure. A test's list therefore contains its own
`.cpp` plus the library sources it used to link — necessary because Stage 8
flattens the libraries away, so there is no `input` target left for a test to
link against. Those library sources are already in `all_sources` (Stage 4b
guarantees the subset), so nothing extra is copied; only the compile lists
differ.

---

## Stage 7 — Collect the precise header closure

**Function:** `extract()` (uses `parse_depfile()`)

**Purpose:** Copy exactly the headers that are *really* included by the closure's
translation units — no more. This is what makes the extraction **minimal**: a
header that exists but is never `#included` is never copied.

**Input:** the `.d` files under each first-party target's `*.dir/`; `top_source`
and `top_build` for filtering.

**Output:** `headers` — a set of absolute header paths.

**Algorithm:**
1. For each *contributing* target (first-party closure plus carried-over tests),
   glob `build/**/CMakeFiles/<name>.dir/**/*.d`.
2. `parse_depfile()` reads each Make-syntax `.d` file: it joins `\`-newline
   continuations, drops the target before the first `:`, and splits the
   remaining prerequisites on whitespace.
3. Resolve each prerequisite to an absolute path and discard sources
   (`.c/.cc/.cpp/.cxx`).
4. Drop anything inside a third-party region (Stage 4): `fmt`'s headers come
   back through its `FetchContent` block, never as copies.
5. Keep what remains only if it lies under `top_source` (first-party) or
   `top_build` (generated). System headers (`/usr/...`) fall outside both roots
   and are dropped here.

Steps 4 and 5 are both required, and in that order: `build/_deps/fmt-src/...`
*is* under `top_build`, so step 5 alone would happily classify a dependency's
headers as generated code.

---

## Stage 8 — Lay out the flat output tree

**Function:** `extract()` (uses `longest_root()`)

**Purpose:** Materialize a clean, flat directory whose layout preserves every
include path, so the copied sources compile unmodified.

**Input:** `sources`, `headers`, `src_inc_roots`, `gen_inc_roots`, output root.

**Output:** files written under `extracted/<T>/`, plus:
- `cmake_sources` — source paths relative to the output dir (for `add_executable`).
- `used_include`, `used_generated` — whether each include dir is needed.

**Algorithm:**
- Remove any prior `extracted/<T>/` and create `src/`.
- **Sources:** copy each to `src/<origin>/<basename>`; record the relative path.
- **Headers:** for each header, choose its destination by finding the
  *longest matching include root* (`longest_root()`):
  - Try `gen_inc_roots` (build tree) **first** → copy to
    `generated/<path-relative-to-root>`, set `used_generated`. Checking the build
    root first is what keeps generated headers out of `include/` despite the
    in-source build dir.
  - Else try `src_inc_roots` → copy to `include/<path-relative-to-root>`, set
    `used_include`.
  - If no root matches, fall back to `include/<basename>` and warn.

The result is `src/` (flattened, namespaced), `include/` (headers at their
original relative path), and `generated/` (frozen generated headers).

---

## Stage 9 — Emit the standalone `CMakeLists.txt`

**Function:** `write_cmakelists()`

**Purpose:** Produce a self-contained build script that needs nothing from the
parent project.

**Input:** target name, `cxx_std`, `cmake_sources`, the include-dir flags, and
the external dependency blocks (`[fetch[e] for e in externals]`).

**Output:** `extracted/<T>/CMakeLists.txt`.

**Algorithm:** Assemble the file textually:
1. `cmake_minimum_required` + `project(<T>_standalone)` + the captured C++
   standard.
2. If there are externals: emit `include(FetchContent)`, then each captured
   `FetchContent_Declare(...)` block *verbatim*, then a single
   `FetchContent_MakeAvailable(<names>)`. The name set is the union of the app's
   externals and every carried-over test's, since a test may use a dependency
   the application does not.
3. `add_executable(<T> …)` listing every collected source, then
   `target_include_directories(<T> PRIVATE include generated)` — only the dirs
   that actually received files — then
   `target_link_libraries(<T> PRIVATE <alias …>)` linking each external via its
   `<name>::<name>` alias.
4. If tests were carried over: `enable_testing()`, then for each test the same
   executable triple (its own source list and its *own* externals — `rng_test`
   gets no `target_link_libraries` because it never used `fmt`), followed by
   `add_test(NAME <registered name> COMMAND <target>)`. The registered CTest
   name is preserved even where it differs from the target name.

---

## Stage 10 — Emit the README

**Function:** `write_readme()`

**Purpose:** Leave a short human description of what the extracted tree is.

**Input:** target name, the first-party target list, the external names.

**Output:** `extracted/<T>/README.md` listing the folded-in first-party targets,
the FetchContent third-party deps, and build instructions.

---

## Stage 11 — Verify (optional, `--verify`)

**Function:** `verify_build()`

**Purpose:** Prove the extracted tree really is standalone and buildable.

**Input:** the output directory.

**Output:** a configured+built `extracted/<T>/build/`, or a non-zero exit on
failure.

**Algorithm:** Run `cmake -S <out> -B <out>/build` then
`cmake --build <out>/build -j`. Any failure raises (via `check=True`), so a
successful run is a hard guarantee that the closure is complete and compiles.

When tests were carried over (Stage 4b), also run `ctest --output-on-failure` in
that build dir. This raises the guarantee from *the closure compiles standalone*
to *the closure passes its own tests standalone* — which is the stronger claim,
since compiling proves only that the headers resolved, not that the code that
came along still behaves.

---

## Why the output is both minimal and standalone

- **Minimal** — sources come only from the transitively-linked first-party
  targets (Stage 3/6), and headers come only from what the compiler recorded as
  actually included (Stage 7). Unused files never enter the tree. Different apps
  therefore yield different closures (`roller` has no `input`; `greeter` has no
  `rng`). `--with-tests` does not weaken this: a test is carried over only when
  the code it links is already present (Stage 4b), so the set of libraries in the
  tree is identical with or without the flag.
- **Standalone** — first-party code is copied with its include structure intact
  (Stage 8), generated code is frozen in place (Stage 7/8), and third-party code
  is reproduced through the project's own FetchContent declarations (Stage 4/9).
  Nothing points back at the parent repo. When a target has no third-party
  dependency at all (`tally`), Stage 9 emits neither the FetchContent section nor
  any `target_link_libraries`, and the extracted tree then configures and builds
  with no network access whatsoever.
