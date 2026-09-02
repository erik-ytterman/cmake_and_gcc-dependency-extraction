# complex_deep — a larger extraction fixture

The larger of the two sample projects under `samples/`, shaped to exercise
`tools/extract_closure.py` against the situations
[TUTORIAL.md §10–11](../../TUTORIAL.md) warns about. Each `samples/*` is its own
CMake project with its own build directory. From the repo root:

```sh
cmake -S samples/complex_deep -B samples/complex_deep/build -DCMAKE_CXX_FLAGS="-MMD"
cmake --build samples/complex_deep/build -j
ctest --test-dir samples/complex_deep/build
python3 tools/extract_closure.py <app> \
    --src samples/complex_deep --build samples/complex_deep/build \
    --out /tmp/cd --with-tests --verify
```

`samples/complex_deep/extract_all.sh` runs the extraction for every app.

## The library tree (several `add_subdirectory` levels deep)

```
libs/
  base/                base            -> cd_version (INTERFACE, generated header)
    mathx/             mathx           -> base
                         src/util.cpp AND src/detail/util.cpp   (same basename)
                         src/internal.hpp                       (private header)
  text/                text            -> fmt::fmt, base
  net/                 net             -> Threads::Threads, base, codec
    codec/             codec (OBJECT)
  data/                data            -> nlohmann_json::nlohmann_json, base
```

## The apps (each a different subset)

| app      | first-party closure            | FetchContent      | find_package |
|----------|--------------------------------|-------------------|--------------|
| `calc`   | mathx, base                    | —                 | —            |
| `render` | text, base                     | fmt               | —            |
| `daemon` | net, codec, base               | —                 | Threads      |
| `report` | data, text, base               | fmt, nlohmann_json | —           |
| `omni`   | everything                     | fmt, nlohmann_json | Threads     |

`fmt` reaches `render` only through `text`'s `PUBLIC` link; `Threads` reaches
`daemon` only through `net`'s — neither app names the dependency itself, so the
link line is reconstructed from the folded-in library's traced
`target_link_libraries`.

## What each feature checks

| Fixture feature | Exercises |
|---|---|
| `mathx` with two `util.cpp` + `src/internal.hpp` | structure-preserving Stage 11 — same-basename sources no longer collide; a private sibling header stays reachable |
| `libs/base/mathx`, `libs/net/codec` | a first-party tree 3 `add_subdirectory` levels deep |
| `fmt` in `cmake/deps.cmake` (via `include()`) | Stage 3 reads the trace, not this file |
| `nlohmann_json` inside `declare_json()` | a `FetchContent_Declare` wrapped in a function — invisible to a text scan |
| `nlohmann_json::nlohmann_json` | an imported-target name that differs from the declared name |
| `find_package(Threads REQUIRED)` | a non-FetchContent dependency, re-emitted verbatim |
| `codec` as `OBJECT` | its sources fold into the consuming target; no separate archive |
| `calc` | a closure with no third-party dependency at all |
| `--with-tests` across five libraries | `select_tests` picking only the covering tests, per app |
