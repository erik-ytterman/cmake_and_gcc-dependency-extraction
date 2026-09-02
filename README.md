# cmake + gcc dependency-extraction POC (proof of concept)

A small CMake/CTest project shaped to explore **minimal per-application code
closure extraction**. Four applications draw from a shared pool of libraries,
each touching a *different subset*, plus generated code.

| Doc | Role |
|---|---|
| **README.md** (you are here) | Quickstart — run the thing |
| [TUTORIAL.md](TUTORIAL.md) | Learn the concepts and APIs, hands-on, with the traps |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |

`python3 util/export_docs.py` renders all three to standalone HTML under
`/tmp/extract_closure/` for offline reading or printing (needs `markdown-it-py`).

## Structure

```
CMakeLists.txt            top-level: fmt (FetchContent) + generated build_info
cmake/build_info.hpp.in   template for generated code
libs/
  rng/                    random-number wrapper (stdlib only)
  input/                  user-input wrapper (depends on fmt)
apps/
  guess/                  number guessing game : input + rng + fmt + build_info
  roller/                 dice roller          :         rng + fmt + build_info
  greeter/                greeter              : input +       fmt + build_info
  tally/                  die-roll histogram   :         rng +       build_info
tools/extract_closure.py  the extractor
util/export_docs.py       render the Markdown docs to standalone HTML
```

## Dependency subsets

| target   | input | rng | fmt | build_info (generated) |
|----------|:-----:|:---:|:---:|:----------------------:|
| guess    |   x   |  x  |  x  |           x            |
| roller   |       |  x  |  x  |           x            |
| greeter  |   x   |     |  x  |           x            |
| tally    |       |  x  |     |           x            |

Because the closures differ per target, extracting the *minimal* closure of a
single app produces a genuinely smaller, flatter, standalone tree. `tally` is
deliberately the one app with **no third-party dependency at all** — it exists to
show what the extracted tree looks like when there is nothing to re-declare.

## Build & test

```sh
cmake -S . -B build
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Extracting a minimal standalone closure

`tools/extract_closure.py` extracts the minimal build closure of one application
target into a flat, standalone, buildable tree:

```sh
cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD"     # build emits .d header deps
cmake --build build -j
python3 tools/extract_closure.py guess --verify  # --verify configures+builds it
```

Output lands in `extracted/<target>/`:

```
extracted/guess/
  CMakeLists.txt      standalone; fmt re-declared via FetchContent
  src/<origin>/..     first-party sources folded in (app + linked libs)
  include/..          first-party headers at their original include path
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
[TUTORIAL.md §11](TUTORIAL.md).

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
  src/rng/rng.cpp
  src/tally/main.cpp
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
python3 tools/extract_closure.py guess --with-tests --verify   # --verify also runs ctest
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
