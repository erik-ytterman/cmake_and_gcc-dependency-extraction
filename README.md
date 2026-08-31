# cmake + gcc dependency-extraction POC

A small CMake/CTest project shaped to explore **minimal per-application code
closure extraction**. Three applications draw from a shared pool of libraries,
each touching a *different subset*, plus generated code.

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
```

## Dependency subsets

| target   | input | rng | fmt | build_info (generated) |
|----------|:-----:|:---:|:---:|:----------------------:|
| guess    |   x   |  x  |  x  |           x            |
| roller   |       |  x  |  x  |           x            |
| greeter  |   x   |     |  x  |           x            |

Because the closures differ per target, extracting the *minimal* closure of a
single app produces a genuinely smaller, flatter, standalone tree.

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

1. **CMake File API codemodel** — the authoritative target/link graph, walked
   transitively from the chosen target to find its first-party libraries.
2. **Per-TU `.d` files** (`g++ -MMD`) — the precise set of headers actually
   `#included`, so only headers on the real closure are copied.
3. **Top-level `CMakeLists.txt`** — the `FetchContent_Declare(fmt ...)` block is
   copied verbatim into the extracted `CMakeLists.txt`, so third-party deps stay
   as FetchContent (not vendored) and the tree remains standalone yet minimal.

Because the apps touch different library subsets, the extracted trees differ:
`roller` contains no `input`, `greeter` contains no `rng`.

You can also emit the raw target graph directly for inspection:

```sh
cmake --graphviz=build/deps.dot -S . -B build
```
