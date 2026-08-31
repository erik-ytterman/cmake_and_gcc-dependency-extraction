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
                 ┌─────────────────────────────────────────────┐
   build/  ─────▶│ 1. codemodel   2. targets   3. closure       │
   src/    ─────▶│ 4. partition   5. roots      6. sources       │──▶ extracted/<T>/
   -MMD .d ─────▶│ 7. headers     8. layout     9. CMakeLists    │      (standalone)
                 └─────────────────────────────────────────────┘
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

**Output:** two dicts — `by_id: id → target_json` and `name_to_id: name → id`.

**Algorithm:** For the first configuration, iterate `configurations[0].targets`;
each entry references a `jsonFile`. Load each file and index it under both its
stable `id` (used by dependency edges) and its human `name` (used by the CLI).

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
worklist empties. For `guess` this yields `{guess, input, rng, fmt, build_info}`.

---

## Stage 4 — Partition first-party vs. third-party

**Function:** `extract()` (uses `parse_fetchcontent()`)

**Purpose:** Decide which closure members get their code copied (first-party) and
which get re-declared as external dependencies (third-party).

**Input:** the closure targets; the set of `FetchContent_Declare` names parsed
from the top-level `CMakeLists.txt`.

**Output:** two lists — `first_party` (target JSONs) and `externals` (names).

**Algorithm:**
- `parse_fetchcontent()` regex-scans the top `CMakeLists.txt` for
  `FetchContent_Declare(<name> … )` blocks, recording for each the *verbatim
  block text* and a default link alias `<name>::<name>`.
- A closure target is **external** iff its `name` appears in that set (e.g.
  `fmt`); otherwise it is **first-party**. This uses the project's own
  declarations as the authority for what is "third-party", so nothing under the
  build tree's `_deps/` is ever copied.
- Note: `build_info` is an `INTERFACE_LIBRARY` with no sources; it is first-party
  but contributes only a generated header (picked up in Stage 7), not `.cpp`s.

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

**Algorithm:** Union all `compileGroups[].includes[].path` across first-party
targets. Partition each root by `is_under(root, top_source)` vs
`is_under(root, top_build)`. Read `languageStandard.standard` when present.
(Because the build dir is nested in the source tree, a build-tree root is *also*
under the source root — Stage 8 resolves the ambiguity by checking the build
root first.)

---

## Stage 6 — Collect first-party sources

**Function:** `extract()`

**Purpose:** Enumerate the `.cpp` files that must be compiled into the standalone
target: `T`'s own sources plus those of every first-party library it links.

**Input:** the `sources[]` of each first-party target; `top_source`.

**Output:** `sources` — a list of `(origin_target_name, absolute_path)` pairs.

**Algorithm:** For each first-party target, take each `sources[]` entry that has a
non-null `compileGroupIndex` (i.e. is actually compiled, not merely a header
listed as a source), resolve its `path` against `top_source`, and keep it if its
extension is a C/C++ source (`.c/.cc/.cpp/.cxx`). The `origin` name is retained
so sources can be namespaced on copy and collisions avoided.

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
1. For each first-party target, glob `build/**/CMakeFiles/<name>.dir/**/*.d`.
2. `parse_depfile()` reads each Make-syntax `.d` file: it joins `\`-newline
   continuations, drops the target before the first `:`, and splits the
   remaining prerequisites on whitespace.
3. Resolve each prerequisite to an absolute path. Discard sources
   (`.c/.cc/.cpp/.cxx`) and keep a header only if it lies under `top_source`
   (first-party) or `top_build` (generated). System headers (`/usr/...`) and
   third-party headers (`build/_deps/fmt-src/...`) fall outside both roots and
   are dropped — `fmt` returns as FetchContent instead.

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
   `FetchContent_MakeAvailable(<names>)` (names re-extracted from each block).
3. `add_executable(<T> …)` listing every collected source.
4. `target_include_directories(<T> PRIVATE include generated)` — only the dirs
   that actually received files.
5. `target_link_libraries(<T> PRIVATE <alias …>)` linking each external via its
   `<name>::<name>` alias.

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

---

## Why the output is both minimal and standalone

- **Minimal** — sources come only from the transitively-linked first-party
  targets (Stage 3/6), and headers come only from what the compiler recorded as
  actually included (Stage 7). Unused files never enter the tree. Different apps
  therefore yield different closures (`roller` has no `input`; `greeter` has no
  `rng`).
- **Standalone** — first-party code is copied with its include structure intact
  (Stage 8), generated code is frozen in place (Stage 7/8), and third-party code
  is reproduced through the project's own FetchContent declarations (Stage 4/9).
  Nothing points back at the parent repo.
