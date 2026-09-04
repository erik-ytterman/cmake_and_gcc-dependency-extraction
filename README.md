# cmake + gcc dependency-extraction POC (proof of concept)

Explores **minimal per-application code closure extraction**: given one
executable in a CMake project, produce a standalone, buildable tree with only
that executable's sources, headers, generated code, and re-declared third-party
dependencies — nothing else.

| Doc | Role |
|---|---|
| **README.md** (you are here) | Quickstart — run the thing |
| [TUTORIAL.md](TUTORIAL.md) | Learn the concepts and APIs, hands-on, with the traps |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |
| [GLOSSARY.md](GLOSSARY.md) | Canonical vocabulary, shared by the code and every document |

`python3 util/export_docs.py` renders all four to standalone HTML under
`/tmp/extract_closure/` for offline reading or printing (needs `markdown-it-py`).

## Structure

```
README.md             quickstart (this file)
TUTORIAL.md           hands-on guide to the concepts and APIs
ALGORITHM.md          stage-by-stage reference
GLOSSARY.md           canonical vocabulary for the code and the docs
tools/
  extract_closure.py    the extractor
  test_tutorial.sh      runs every TUTORIAL lab against samples/basic
  run_full_cycle.sh     clean -> build -> test -> extract every app, both samples
util/
  export_docs.py        render the Markdown docs to standalone HTML
samples/
  basic/                the teaching sample — 4 apps, 2 libs, one dependency
  complex_deep/         the larger sample — deep tree, several dependencies,
                        an OBJECT library, a same-basename collision
```

Each `samples/*` is its own CMake project with its own `project()` and build
directory. The extractor is pointed at one with `--src` / `--build`.

## The basic sample

```
samples/basic/
  CMakeLists.txt            top-level: fmt (FetchContent) + generated build_info
  cmake/build_info.hpp.in   template for generated code
  libs/rng/                 random-number wrapper (stdlib only)
  libs/input/               user-input wrapper (depends on fmt)
  apps/guess/               number guessing game : input + rng + fmt + build_info
  apps/roller/              dice roller          :         rng + fmt + build_info
  apps/greeter/             greeter              : input +       fmt + build_info
  apps/tally/               die-roll histogram   :         rng +       build_info
```

| target   | input | rng | fmt | build_info (generated) |
|----------|:-----:|:---:|:---:|:----------------------:|
| guess    |   x   |  x  |  x  |           x            |
| roller   |       |  x  |  x  |           x            |
| greeter  |   x   |     |  x  |           x            |
| tally    |       |  x  |     |           x            |

Because the closures differ per target, extracting the *minimal* closure of a
single app produces a genuinely smaller, standalone tree. `tally` is deliberately
the one app with **no third-party dependency at all** — it shows what the
extracted tree looks like when there is nothing to re-declare.

## Build & test

Build and run the sample the ordinary way. `-MMD` tells the compiler to drop a
`.d` depfile — the list of headers that object depends on — next to every
object file; it is harmless for a
normal build and is exactly what the extractor reads later, so it is on from the
start here.

```sh
cd samples/basic
cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD"
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Extracting a minimal standalone closure

`tools/extract_closure.py` extracts the minimal build closure of one application
target into a standalone, buildable tree.

### What the extractor needs first

**It consumes an already-configured, already-built tree — it never configures or
builds the source project for you.** Two requirements on that build:

- **A per-translation-unit `.d` depfile next to every object file.** The
  extractor reads these for the exact header set. Passing `-MMD` in
  `CMAKE_CXX_FLAGS` guarantees them for any generator or CMake version; without
  it, whether the `.d` files exist depends on the generator (the Unix Makefiles
  generator on a recent CMake emits them anyway; Ninja and older setups may not).
  If a translation unit has no depfile, its headers are simply not copied — so a standalone
  build that then fails on a missing `#include` is the symptom of a build done
  without depfiles.
- **CMake ≥ 3.21**, because the extractor re-runs configure under
  `--trace-redirect` to capture the command trace.

What you do and don't re-run before an extraction:

| Command | Run it before extracting? |
|---|---|
| `cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD"` | **Once.** A no-op if the build dir is already configured that way. Re-run it only if you first configured *without* `-MMD` — it flips the flag, and the next build regenerates the `.d` files. |
| `cmake --build build -j` | **After every source edit**, so the objects and `.d` files are current. |
| `cmake <build>` reconfigure | **Never by hand.** The extractor runs it itself on every invocation (reconfigure + command trace). |

```sh
cd samples/basic
cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD"   # once; no-op if already done
cmake --build build -j                          # rebuilds only what changed
python3 ../../tools/extract_closure.py guess --verify   # --verify configures+builds the result
```

To run the whole thing end to end from a clean tree — clean, then configure /
build / test each sample and extract every one of its apps — use
`bash tools/run_full_cycle.sh` (add `--verify` to also build and test every
extracted tree).

Output lands in `extracted/<target>/`:

```
extracted/guess/
  CMakeLists.txt      standalone; fmt re-declared via FetchContent
  src/<origin>/..     first-party sources (app + linked libs) and private
                      headers, namespaced by origin, sub-directory structure kept
  include/..          first-party public headers at their original include path
  generated/..        generated headers, frozen as plain files
```

How it works — it combines three sources of truth:

1. **CMake File API codemodel** — the authoritative target graph, walked
   transitively from the chosen target to find its first-party libraries.
2. **Per-translation-unit `.d` files** (`g++ -MMD`) — for each `.cpp` and
   everything it includes, the precise set of headers actually `#included`, so
   only headers on the real closure are copied.
3. **The CMake command trace** — the extractor re-runs configure under
   `--trace-expand --trace-format=json-v1` and reads back every
   `FetchContent_Declare`, `find_package` and `target_link_libraries` the
   project's own CMake ran, with arguments already variable-expanded. Each
   `FetchContent_Declare` block is regenerated into the extracted `CMakeLists.txt`
   (so third-party code stays fetched, not vendored); each `find_package` call is
   re-emitted verbatim (the host toolchain provides it); each executable's link
   line is built from the traced `target_link_libraries`. `--deps-file <glob>`
   text-scans extra files for the rare declaration the trace never reaches. Needs
   CMake ≥ 3.21.

Because the apps touch different library subsets, the extracted trees differ:
`roller` contains no `input`, `greeter` contains no `rng`.

For a bigger repo — many top-level executables, a first-party library tree many
`add_subdirectory()` levels deep, several fetched dependencies — see
[TUTORIAL.md §11](TUTORIAL.md) and the `samples/complex_deep/` sample.

### A closure with no third-party dependency

`tally` links only `rng` and `build_info`, so its extracted `CMakeLists.txt`
drops the whole FetchContent section — and, having nothing to link, emits no
`target_link_libraries` either:

```cmake
cmake_minimum_required(VERSION 3.20)
project(tally_standalone LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(tally
  src/rng/src/rng.cpp
  src/tally/src/main.cpp
)
target_include_directories(tally PRIVATE include generated)
```

The tree is four files — two sources, one header, one generated header — and its
build directory contains no `_deps/`, so it configures and builds with **no
network access at all**. The generated `build_info.hpp` still arrives, which is
the point: generated code is frozen into the tree, while third-party code is the
only thing left to fetch. With `--with-tests` it also picks up `rng_test`, which
likewise needs nothing external, so the closure stays dependency-free.

### Carrying the tests over (`--with-tests`)

By default the extracted tree holds application code only — test targets are not
reachable by walking link edges outward from an app (`input_test` depends on
`input`, not the reverse). `--with-tests` adds them:

```sh
python3 ../../tools/extract_closure.py guess --with-tests --verify   # --verify also runs ctest
```

`ctest --show-only=json-v1` is the authority for what counts as a test, and each
test's command is matched against the targets' build artifacts, so nothing is
inferred from `*_test`-style naming. A test is carried over only when every
first-party target it links is already in the app's closure — so tests never
enlarge the tree — and one that is not is reported:

```
note: skipping test 'rng_test' -- it needs rng, not in greeter's closure
```

| target  | tests carried over     |
|---------|------------------------|
| guess   | `input_test`, `rng_test` |
| roller  | `rng_test`             |
| greeter | `input_test`           |
| tally   | `rng_test`             |

Since the libraries are folded in, each test compiles the library sources
it used to link; the emitted `CMakeLists.txt` gains `enable_testing()`, one
`add_executable()` per test, and an `add_test()` preserving the registered name.
This upgrades `--verify` from "compiles standalone" to "passes standalone".

You can also emit the raw target graph directly for inspection:

```sh
cmake --graphviz=build/deps.dot -S . -B build
```

## Command-line reference

```
python3 tools/extract_closure.py TARGET [--src DIR] [--build DIR] [--out DIR]
                                        [--with-tests] [--verify]
                                        [--deps-file GLOB ...] [--allow-collisions]
```

| Argument | Default | What it does |
|---|---|---|
| `TARGET` | *(required)* | The executable to extract, named by its **CMake target name** — not a path, not the output filename. An unknown name exits with the full list of available targets. |
| `--src DIR` | `.` | Source root of the project being read (the directory with its top-level `CMakeLists.txt`). Only actually consulted to resolve `--deps-file` globs; every other input is taken from the build dir, which records its own source path. |
| `--build DIR` | `build` | The project's already-configured build directory, resolved against the **current working directory** (not against `--src`). Must have been configured with CMake ≥ 3.21 and compiled with `-MMD` so the per-translation-unit `.d` files exist. |
| `--out DIR` | `extracted` | Output root. The standalone tree is written to `<out>/<TARGET>/`; an existing `<out>/<TARGET>/` is deleted and rewritten on each run. |
| `--with-tests` | off | Also carry over the registered CTest tests that cover the extracted code. A test is taken only when every first-party library it links is already in the closure, so tests never enlarge the tree; one that would is reported and skipped. Adds `enable_testing()` + an `add_executable()` / `add_test()` per test to the emitted `CMakeLists.txt`. |
| `--verify` | off | After extracting, configure and build `<out>/<TARGET>/` to prove it stands alone. With `--with-tests` it also runs `ctest`. Needs network access when the closure re-declares a FetchContent dependency (it re-fetches, e.g. `fmt`). |
| `--deps-file GLOB` | *(none)* | Extra CMake file(s) to text-scan for `FetchContent_Declare` blocks — a glob relative to `--src`, repeatable. Rarely needed: the command trace already captures every declaration that runs at configure time. Use it only for one guarded behind an `if()` the trace never enters. |
| `--allow-collisions` | off | Proceed (keeping the last file) when two different files map to the same path in the extracted tree, instead of aborting with both paths listed. Rare now that sub-directory structure is preserved — mostly two libraries exposing the same `include/<path>.hpp`. |

### Examples

All from inside `samples/basic` unless noted.

```sh
cd samples/basic

# Extract `guess` into ./extracted/guess/ — nothing else
python3 ../../tools/extract_closure.py guess

# ...and prove the emitted tree configures and builds on its own
python3 ../../tools/extract_closure.py guess --verify

# ...and carry its covering CTest tests over, running them under --verify
python3 ../../tools/extract_closure.py guess --with-tests --verify

# `tally` links no third-party dependency: the emitted CMakeLists has no FetchContent
python3 ../../tools/extract_closure.py tally --verify

# Run from the repo root instead, with an explicit output location
cd ../..
python3 tools/extract_closure.py guess \
    --src samples/basic --build samples/basic/build --out /tmp/closures

# Cover a FetchContent_Declare that sits behind an if() the trace never enters
python3 ../../tools/extract_closure.py guess --deps-file 'cmake/*.cmake'
```

For the larger sample and its per-app extraction script, see
`samples/complex_deep/` and [TUTORIAL.md §11](TUTORIAL.md).
