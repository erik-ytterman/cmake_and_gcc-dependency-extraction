# The complex_deep sample

The larger of the two samples under `samples/`, shaped to exercise
`tools/extract_closure.py` against the situations
[TUTORIAL.md §10–11](../../TUTORIAL.md) warns about — and, since it depends on
Boost, to make the cost of *not* shipping a large dependency measurable.

Each `samples/*` is its own CMake project with its own build directory. From the
repo root:

```sh
cmake -S samples/complex_deep -B samples/complex_deep/build -DCMAKE_CXX_FLAGS="-MMD"
cmake --build samples/complex_deep/build -j
ctest --test-dir samples/complex_deep/build
python3 tools/extract_closure.py report \
    --src samples/complex_deep --build samples/complex_deep/build \
    --out /tmp/cd --with-tests --verify
```

`samples/complex_deep/extract_all.sh` runs that and checks the result.

> **Disk and time.** Boost unpacks to ~670 MB and builds from source. Expect
> ~1 GB per build tree and several minutes for a cold build — and note that the
> extracted tree fetches and builds Boost *again*, because a re-declared
> dependency is paid for by whoever builds it.

## One application, over a library tree it only partly uses

```
libs/
  core/              core (STATIC)         -> cd_version         [reached]
      src/util.cpp AND src/detail/util.cpp   (same basename)
      src/internal.hpp                       (private header)
    codec/           corecodec (OBJECT)    -> core               [reached]
  jsonio/            jsonio (STATIC)       -> nlohmann_json      [reached]
  textutil/          textutil (STATIC)     -> Boost::algorithm   [reached]
  heavy/
    geom/            geom (STATIC)         -> Boost::geometry    [NOT reached]
    netsvc/          netsvc (STATIC)       -> Boost::asio, Threads  [NOT reached]
    parsing/         parsing (STATIC)      -> Boost::spirit      [NOT reached]
apps/
  report/            report -> core, corecodec, jsonio, textutil, cd_version
```

`report` is the only application. It reaches four of the seven first-party
targets; the three under `heavy/` belong to the rest of the monorepo and each
pulls in one of Boost's expensive header trees. Extraction leaves all three
behind, along with their tests.

## What each feature is here to exercise

| Feature | Where | Exercises |
|---|---|---|
| Deep `add_subdirectory()` nesting | `libs/heavy/geom` is three levels down | directory-tree walking (Stage 4) |
| `OBJECT` library | `core/codec` | a target with no archive artifact |
| Same-basename sources | `core/src/util.cpp`, `core/src/detail/util.cpp` | structure preservation (Stage 11) |
| Private header | `core/src/internal.hpp` | placement beside its sources, not in `include/` |
| Declaration in an `include()`d file | Boost, in `cmake/deps.cmake` | the trace beats a text scan (Stage 3) |
| Declaration inside a function | `nlohmann_json`, in `declare_json()` | the same, harder |
| `find_package` dependency | `Threads`, used by `netsvc` | re-emission — and it is *not* reached, so it must not appear |
| Namespaced imported target | `Boost::algorithm` | the link token is `Boost::…`, the declaration is `boost` |
| Generated header | `cd_version` INTERFACE lib | an INTERFACE target the codemodel cannot see |
| A large third-party dependency | Boost | what stays behind, and what does not |

## What the extraction produces

```
sources      : 7
headers      : 6
FetchContent : boost, nlohmann_json
find_package : (none)
tests        : core_test, jsonio_test, textutil_test
```

`geom_test`, `netsvc_test` and `parsing_test` are reported as skipped: each
needs a library outside `report`'s closure, so carrying it over would enlarge
the tree. `find_package(Threads)` does not appear either — only `netsvc` used
it, and `netsvc` is not reached.

The deliverable is **18 files, 176 KB**. Boost's 673 MB never travels: it comes
back as a four-line `FetchContent_Declare` with its original pinned URL and
hash.
