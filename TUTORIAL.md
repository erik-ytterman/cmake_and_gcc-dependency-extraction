# Tutorial: extracting one app's build closure

**Who this is for:** developers who need to pull a single application out of a
larger CMake project — to ship it, open-source it, hand it to a partner, or
shrink a build — and want to understand *how* to do that reliably rather than by
hand-copying files until it compiles.

**What you'll learn:** which tools can tell you the truth about a build, how to
query them, and the five traps that make a naive implementation quietly wrong.

**The three docs in this repo:**

| Doc | Role |
|---|---|
| [README.md](README.md) | Quickstart — run the thing |
| **TUTORIAL.md** (you are here) | Learn the concepts and the APIs, hands-on |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |

Work through this with a terminal open. Every command below is real, and every
output shown is actual output from this project. If you would rather watch the
whole tutorial run start to finish before reading it, skip to
[section 11](#11-running-every-lab-at-once) — `tools/test_tutorial.sh` executes
the core labs (§2–§9) in order. Section 12 is an optional add-on.

---

## Vocabulary

Terms used throughout. Skim now, refer back as needed.

| Term | Meaning |
|---|---|
| **Translation unit (TU)** | One source file *plus every header it includes*, as the compiler sees it after preprocessing. One `.cpp` → one TU → one object file → one depfile. It is the unit the compiler actually reasons about, which is why per-TU facts are so precise. |
| **Closure** | Everything transitively reachable from a starting point. *Target closure*: every library an app links, directly or through another library. *Header closure*: every header a TU includes, directly or through another header. Both get a fuller treatment in section 3. |
| **Target graph** | The directed graph of CMake targets and the "links against" edges between them. The codemodel is its only trustworthy source. See section 3. |
| **Dependency boundary** | The line between code the extractor copies (first-party) and code it re-declares so the tree re-fetches it (third-party). Drawn from the project's own dependency declarations. See section 3. |
| **Depfile** (`.d` file) | A small file in Makefile syntax that the compiler writes next to each object file, listing that TU's header closure. Produced by `-MMD`. |
| **Codemodel** | The CMake File API object describing a configured build: its targets, their sources, include directories and link edges. |
| **First-party / third-party** | Code the project owns versus code it pulls in from outside. The boundary is drawn from the project's own dependency declarations — never from a path pattern. |
| **Ground truth** | A fact recorded by the tool that actually did the work (CMake, the compiler), as opposed to one re-derived by inspecting files afterwards. The whole design rests on preferring the former. |
| **Generator expression** | CMake's `$<...>` syntax, evaluated when the build files are generated rather than when the `CMakeLists.txt` is read. One of several reasons that file cannot simply be parsed. |
| **INTERFACE library** | A CMake target that compiles nothing and exists only to pass usage requirements — include directories, defines, flags — to whatever links it. `build_info` is one. See Trap 2. |
| **Imported target** | A target standing in for something built outside this project, e.g. produced by `find_package()`. |
| **Multi-config generator** | A generator whose single build tree holds several configurations at once (Ninja Multi-Config, Visual Studio), as opposed to one configuration per build dir. |
| **In-source / out-of-source build** | Whether the build directory sits inside the source tree (`./build/`) or outside it. This project's default is in-source — see Trap 3. |
| **POC** | Proof of concept. This repo is one: it demonstrates the approach rather than being production-hardened. |

---

## 1. The problem

You have a monorepo. Four apps share a pool of libraries, each touching a
different subset, plus some generated code and a third-party dependency:

```
guess    -> input + rng + fmt + build_info
roller   ->         rng + fmt + build_info
greeter  -> input +       fmt + build_info
tally    ->         rng +       build_info
```

You want `greeter` alone in a small standalone tree: its sources, the library
sources it actually links, the headers it actually includes, and nothing else.

The hard part is *nothing else*. Anyone can copy the whole repo.

### The repo at a glance

Three different things live side by side here, and it helps to keep them
straight before you start — especially `build/`, which is an *input* to the
extractor, not one of its products.

| Directory | Purpose |
|---|---|
| **The sample project** — what you extract *from* | |
| `CMakeLists.txt` | Top level. Declares `fmt` via FetchContent, generates `build_info.hpp`, adds the subdirectories below. |
| `apps/` | The four applications, one directory each. Deliberately shaped so their dependency subsets differ — that is what makes minimal extraction observable. |
| `libs/` | The shared first-party libraries (`rng`, `input`). Each has `include/`, `src/`, and a `test/` registered with CTest. |
| `cmake/` | CMake inputs that are not targets — here `build_info.hpp.in`, the template `configure_file()` turns into a generated header. This is the project's stand-in for "generated code". |
| **The tool** — what this tutorial teaches | |
| `tools/` | `extract_closure.py`, the extractor. Pure Python, no part of the C++ build. |
| **Generated output** — both are gitignored, delete freely | |
| `build/` | The configured build tree of the *parent* project, and the extractor's entire input: File API replies under `.cmake/api/`, `.d` depfiles beside each object file in `<target>/CMakeFiles/<target>.dir/`, generated headers in `generated/`, and fetched third-party sources in `_deps/`. |
| `extracted/` | The extractor's output — one self-contained tree per app, each with its *own* nested `build/` when you pass `--verify`. |

So `build/` is where the facts come from, and `extracted/` is where the answers
go. If a path in this tutorial confuses you, check which of those two it is
under.

---

## 2. Why the obvious approaches fail

Try these first, so you understand what the real approach is buying you.

**"Parse the CMakeLists.txt files."** You would be reimplementing CMake.
`target_link_libraries` takes generator expressions, variables, and function
calls; targets are defined across subdirectory scopes; conditionals mean the file
does not describe one build, it describes a family of them. The text is the
program, not the answer.

**"Use `cmake --graphviz=deps.dot`."** Closer — it emits a real target graph:

```sh
cmake --graphviz=build/deps.dot -S . -B build
```

But it is a *visualization* format, tuned for humans and lossy on purpose. It
tells you `greeter` links `input`. It does not tell you which source files
`input` compiles, which include directories it exports, or which of its headers
`greeter` actually reached.

**"Grep the sources for `#include`."** To resolve `#include "input/input.hpp"`
you need the include path. To handle `#ifdef` you need a preprocessor. To handle
macro-constructed include names you need the whole compiler. You will get it
subtly wrong, and the failure mode is a header that is missing only in some
configuration.

**"Just copy everything."** Correct, and useless. The goal is minimality.

The insight behind all of this: **do not re-derive facts the build system and
the compiler already computed.** Ask them.

---

## 3. The three sources of truth

| Source | Question it answers | Authority for |
|---|---|---|
| CMake File API codemodel | What targets exist, what links what, what compiles what | The **target graph** |
| Compiler depfiles (`-MMD`) | Which headers did this translation unit *actually* include | The **header closure** |
| The project's own `FetchContent_Declare` | What is third-party, and pinned to what | The **dependency boundary** |

Plus one more for tests: `ctest --show-only=json-v1` — the authority on what is
actually a registered test.

Each is a *fact produced by the build*, not a guess about it. That is the whole
design principle. The rest of this tutorial is learning to query them.

The right-hand column names the three things you have to get right, so it is
worth being precise about what each one is.

### The target graph

The directed graph whose nodes are CMake targets — executables, libraries — and
whose edges are "links against". Walking it outward from one app gives that app's
**target closure**: every library whose compiled code can end up in the binary.

This is what decides *which source files* the extraction copies. Get it wrong in
one direction and the extracted tree fails to link; get it wrong in the other and
you have copied libraries the app never used, which defeats the point.

Only the codemodel knows this graph (Lab 1), because only CMake resolved the
variables, generator expressions and subdirectory scopes that produced the edges.
One caveat that costs people real time: `dependencies[]` encodes *build
ordering*, so targets with nothing to build are absent from it — see Trap 2.

### The header closure

For one translation unit, the set of headers the preprocessor actually reached,
following includes transitively.

Note the four things it is *not*: not every header in the repo, not every header
under an include directory, not every header named in an `#include` line (some
are behind `#if`), and not every header a *different* app needed. It is the real,
post-preprocessor set for this specific TU.

This is what decides *which header files* the extraction copies, and it is where
minimality actually comes from — a header that exists but was never included
never enters the tree. Only the compiler knows it, because only the compiler ran
the preprocessor. `-MMD` is how you ask (Lab 2).

### The dependency boundary

The line between code you **copy** and code you **re-declare**.

First-party code is copied into the extracted tree. Third-party code is not —
instead the project's own dependency declaration is reproduced, so the standalone
tree re-fetches the same pinned version the parent built against. That is what
keeps the result both self-contained and small: you get `fmt` 10.2.1 without
carrying a copy of `fmt` around.

Drawing this line wrong is costly in both directions. Put the boundary too far
out and you vendor a stale, partial snapshot of somebody else's library — that is
exactly Trap 1, and it built fine while being wrong. Put it too far in and the
extracted tree does not build at all, because code you assumed was external is
declared nowhere.

The authority has to be the project's *own* declarations — here
`FetchContent_Declare` — for two reasons: they are the only statement of what
this project considers external, and they carry the version pin that has to
travel with the extraction. Lab 3 shows how to turn those declarations into a
test you can apply to any file.

---

## 4. Lab 1 — The CMake File API

The File API is CMake's machine-readable description of a configured build
(CMake ≥ 3.14). It replaces every form of CMakeLists.txt scraping.

It works as a **file-based request/response protocol**:

1. You drop an empty file into a query directory, named for what you want.
2. You re-run CMake.
3. CMake writes JSON into a reply directory.

### Try it

```sh
mkdir -p build/.cmake/api/v1/query
touch build/.cmake/api/v1/query/codemodel-v2
cmake build                    # no-op reconfigure using the existing cache
ls build/.cmake/api/v1/reply/
```

The filename *is* the request: `codemodel-v2` asks for the codemodel object,
version 2. Nothing is passed on the command line. Start at the index:

```sh
python3 -c "
import json,glob
i=json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/index-*.json'))[-1]))
print(i['cmake']['version']['string'])
for o in i['objects']: print(o['kind'], o['jsonFile'])
"
```

```
4.2.3
codemodel codemodel-v2-a535780dfc73e26aabb2.json
```

(Your CMake version and the hash in the filename will differ — the hash is
content-derived.) Filenames are content-hashed, so **always resolve them through
the index** — never glob for `target-greeter-*.json` in production code.

### What the codemodel gives you

The top-level codemodel lists the build's **configurations** — just one here,
since Makefiles is a single-config generator. Each configuration carries a
`targets[]` array and a `directories[]` array, and every target additionally has
its own detail file (named by `jsonFile`). Here is the real `greeter` detail,
trimmed to the fields that drive an extraction:

```json
{
  "name": "greeter",
  "id": "greeter::@31045165d3807a4b136c",
  "type": "EXECUTABLE",
  "paths": { "source": "apps/greeter", "build": "apps/greeter" },
  "nameOnDisk": "greeter",
  "artifacts": [ { "path": "apps/greeter/greeter" } ],

  "dependencies": [
    { "id": "fmt::@976f4f0bee90b99ecdb6" },
    { "id": "input::@6d945ddea8f4ec024c33" }
  ],
  "linkLibraries": [
    { "id": "input::@6d945ddea8f4ec024c33" },
    { "id": "fmt::@976f4f0bee90b99ecdb6" },
    { "id": "build_info::@6890427a1f51a3e7e1df" }
  ],
  "link": {
    "language": "CXX",
    "commandFragments": [
      { "fragment": "-MMD", "role": "flags" },
      { "fragment": "../../libs/input/libinput.a",     "role": "libraries" },
      { "fragment": "../../_deps/fmt-build/libfmt.a",  "role": "libraries" }
    ]
  },

  "sources": [
    { "path": "apps/greeter/src/main.cpp",
      "compileGroupIndex": 0, "sourceGroupIndex": 0 }
  ],
  "compileGroups": [
    {
      "language": "CXX",
      "sourceIndexes": [ 0 ],
      "compileCommandFragments": [ { "fragment": "-MMD -std=gnu++17" } ],
      "languageStandard": { "standard": "17" },
      "includes": [
        { "path": ".../libs/input/include" },
        { "path": ".../build/_deps/fmt-src/include" },
        { "path": ".../build/generated" }
      ]
    }
  ]
}
```

Read it against the six things the File API is the authority for:

**Targets.** `name` is the human handle; `id` (`greeter::@<hash>`) is the stable
key every edge refers to — join on `id`, never on `name`, because a `name` may be
an alias (`fmt::fmt`) and the edges never use names anyway. `type` is one of
`EXECUTABLE`, `STATIC_LIBRARY`, `SHARED_LIBRARY`, `MODULE_LIBRARY`,
`OBJECT_LIBRARY`, `INTERFACE_LIBRARY`, or `UTILITY`; the extractor copies sources
only for the buildable kinds. `paths.source` is the directory that *defined* the
target — the hook Lab 3 uses to separate first-party from third-party — and
`artifacts[].path` / `nameOnDisk` name what it produces, which is how a CTest
command is matched back to its target (§6). Under a recent CMake the
`INTERFACE` and imported targets (`build_info`, and `fmt`'s header-only variant)
are listed in a sibling `abstractTargets[]` array rather than in `targets[]`;
either way they are missing from `dependencies[]` — the next point.

**Link edges.** Three arrays describe "what does this link", and they differ:

- `dependencies[]` — the **build-ordering** graph, ids only. This is what
  `transitive_closure()` walks: from `greeter`, follow every `id`, and you have
  the target closure in about ten lines. It lists only targets that *build
  something*, so `INTERFACE` libraries like `build_info` are absent — Trap 2.
- `linkLibraries[]` — the resolved link interface, `INTERFACE` libraries
  included (`build_info` appears here where `dependencies[]` omits it). Newer
  codemodels only; older ones expose just `dependencies[]` and `link`.
- `link.commandFragments[]` — the literal linker arguments, each tagged with a
  `role` (`flags`, `libraries`, `libraryPath`, `frameworkPath`). The `libraries`
  fragments are real artifact paths (`../../_deps/fmt-build/libfmt.a`) — the
  ground truth §12's optional lab cross-checks against.

The extractor deliberately walks the narrow `dependencies[]` and lets the
depfiles recover what it misses; "two sources covering each other's blind spots"
is the whole Trap 2 lesson.

**Sources.** `sources[]` is every file attached to the target. `path` is relative
to the top-level source dir (or absolute — see the note below).
`compileGroupIndex` points into `compileGroups[]` and is **`null`** for a file
that is listed but not compiled — a header dropped into `add_executable()`, a
data file in a `source_group()`. `collect_sources()` filters on exactly that
(plus a real C/C++ extension) so it never hands a `.hpp` to the compiler.
`sourceGroupIndex` is just the IDE folder ("Source Files") and is irrelevant
here.

**Include dirs.** `compileGroups[].includes[].path` is the fully-resolved `-I`
list for that group — **absolute paths, in search order**, after CMake expanded
every `target_include_directories`, generator expression and transitive usage
requirement. The extractor needs it to re-file each copied header at the same
include-relative path, so `#include "input/input.hpp"` keeps resolving with no
edit to any source. An entry may also carry `"isSystem": true` (a `-isystem`
path); those are the headers a compiler omits from a `-MMD` depfile, which is
precisely why "is it in the depfile?" cannot be the third-party test (§5).

**Language standard.** `compileGroups[].languageStandard.standard` is `"17"` here
— digits only, matching CMake's `CXX_STANDARD`. `language` is `"CXX"`, and
`compileCommandFragments[]` shows what actually reached the compiler
(`-std=gnu++17` — `gnu++`, not `c++`, because `CXX_EXTENSIONS` defaults on). The
extractor copies the number straight into the generated
`set(CMAKE_CXX_STANDARD 17)`; if targets in the closure disagree, the last one
processed wins (a production tool would take the max).

**Directory tree.** `configurations[0].directories[]` — the next subsection.

### The directory tree

`configurations[0].directories[]` is the part most people miss, and Lab 3 is
built on it. It is the `add_subdirectory()` tree — one entry per directory, with
`source`, `build`, `parentIndex`, `childIndexes`, `targetIndexes` and
`projectIndex`:

```sh
python3 -c "
import json, glob
cm = json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/codemodel-v2-*.json'))[-1]))
for i, d in enumerate(cm['configurations'][0]['directories']):
    tix = d.get('targetIndexes', [])
    tix = f'{len(tix)} targets' if len(tix) > 4 else tix
    print(f\"{i}  {d['source']:<22} -> {d['build']:<18} parent={d.get('parentIndex')}  {tix}\")
"
```

```
0  .                      -> .                  parent=None  28 targets
1  build/_deps/fmt-src    -> _deps/fmt-build    parent=0  [28]
2  libs/rng               -> libs/rng           parent=0  [33, 34]
3  libs/input             -> libs/input         parent=0  [31, 32]
4  apps/guess             -> apps/guess         parent=0  [30]
5  apps/roller            -> apps/roller        parent=0  [35]
6  apps/greeter           -> apps/greeter       parent=0  [29]
7  apps/tally             -> apps/tally         parent=0  [36]
```

Each entry has:

- **`source`, `build`** — the directory on each side, relative to the top (or
  absolute). These two *diverge* for FetchContent: directory 1's source is
  `build/_deps/fmt-src` — under the **build** tree — while its build side is
  `_deps/fmt-build`. Hold that: it is Trap 1.
- **`parentIndex`, `childIndexes`** — the tree links. A missing `parentIndex`
  marks a top-level directory; `external_regions()` uses that so a name collision
  at the root can never mark the whole project third-party.
- **`targetIndexes`** — which entries of `configurations[0].targets[]` this
  directory defined (index 28 → `fmt`, 33/34 → `rng`/`rng_test`, …).
  `external_regions()` inverts the relation: once a directory is known
  third-party, every target it defined is third-party too.
- **`projectIndex`, `minimumCMakeVersion`** — which `project()` owns the
  directory (here `guessing_poc` vs the nested `FMT`) and the
  `cmake_minimum_required` in force. Useful for diagnostics; unused by the
  extractor.

The `_deps/fmt-src` line is the entire reason Lab 3 exists: FetchContent's source
directory lands *inside the build tree*, so no "is it under `build/`?" test can
tell a dependency's headers from genuinely generated ones. The directory tree,
which records *who declared what*, can.

> **Note on paths.** File API paths are relative to the top-level source or build
> dir when they sit underneath it, and absolute otherwise. `Path(top) / p`
> handles both, because Python returns the right-hand operand when it is
> absolute.

---

## 5. Lab 2 — Compiler depfiles

The codemodel knows targets. It does *not* know which headers a translation unit
(TU — one `.cpp` plus everything it includes) actually pulled in. Only the
compiler knows that, because only the compiler ran the preprocessor.

Ask for it with `-MMD`, which makes GCC/Clang write a `.d` file next to each
object file:

```sh
cmake -S . -B build -DCMAKE_CXX_FLAGS="-MMD"
cmake --build build -j
cat build/apps/greeter/CMakeFiles/greeter.dir/src/main.cpp.o.d
```

Real output:

```make
apps/greeter/CMakeFiles/greeter.dir/src/main.cpp.o: \
 .../apps/greeter/src/main.cpp \
 .../build/_deps/fmt-src/include/fmt/color.h \
 .../build/_deps/fmt-src/include/fmt/format.h \
 .../build/_deps/fmt-src/include/fmt/core.h \
 .../build/_deps/fmt-src/include/fmt/core.h \
 .../build/generated/build_info.hpp \
 .../libs/input/include/input/input.hpp
```

That is ground truth: the exact header closure of one TU, after the preprocessor
resolved every conditional and every include path. This is what makes the
extraction *minimal* — a header that exists but was never included never gets
copied.

The format is a Make rule, so parsing is three lines: join `\`-newline
continuations, drop everything up to the first `:`, split on whitespace. Note
`fmt/core.h` appearing twice — depfiles list a header once per inclusion path, so
collect into a set.

**`-MMD` vs `-MD`:** `-MD` lists system headers too; `-MMD` omits them. You want
`-MMD` — you are not going to copy `/usr/include/stdio.h`.

> **Note (CMake ≥ 4.0): not every `.d` file is a compiler depfile.** CMake 4.0
> also writes a *link-step* depfile named `link.d` into the same
> `CMakeFiles/<target>.dir/`, which lists object files and libraries, not
> headers. Collect the per-TU depfiles by matching `*.o.d`, not `*.d` — see
> Trap 5 in [section 8](#8-the-five-traps).

### The subtlety worth internalizing

Look at that depfile again. `fmt/core.h` and `build_info.hpp` are both under
`build/`, and both are listed. Why is a third-party header not treated as a
"system" header and omitted?

```sh
grep CXX_INCLUDES build/apps/greeter/CMakeFiles/greeter.dir/flags.make
```

```
-I.../libs/input/include -I.../build/_deps/fmt-src/include -I.../build/generated
```

Plain `-I`, not `-isystem`. FetchContent brings a dependency in via
`add_subdirectory()`, so its targets are ordinary targets in your build and their
include dirs are ordinary include dirs. (Imported targets from `find_package()`
often *are* marked system, and would then be omitted from `.d`.)

**Lesson:** you cannot use "is it in the depfile?" to decide what is
third-party. Whether a dependency's headers appear depends on how its include
directory happened to be passed. You need a separate authority — Lab 3.

---

## 6. Lab 3 — The dependency boundary

To keep the extracted tree *standalone but not bloated*, third-party code is not
copied at all. Instead the project's own `FetchContent_Declare` block is lifted
verbatim into the generated `CMakeLists.txt`:

```cmake
FetchContent_Declare(
  fmt
  GIT_REPOSITORY https://github.com/fmtlib/fmt.git
  GIT_TAG        10.2.1
  GIT_SHALLOW    TRUE
)
```

The pin travels with the extraction, so the standalone tree builds against the
same version the parent did — without vendoring a snapshot that will rot.

That leaves one question: **which files are third-party?** The answer must not be
a path heuristic, because `_deps/` is a default that projects override, and it
must not be a name check, because a dependency's target names need not match its
declared name (`googletest` → `gtest`, `gmock`).

The extractor uses the directory tree from Lab 1 (`external_regions()`), marking
a directory third-party on either signal:

1. it defines a target named after a `FetchContent_Declare`, or
2. it is the population directory FetchContent creates at
   `<build>/_deps/<name>-src`.

Child directories inherit the marking, so a dependency that calls
`add_subdirectory()` internally is covered. Each marked directory contributes
**both** its source and its build path. Anything inside those regions is never
copied, never used as an include root, and never treated as a first-party target.

### And for tests

`ctest --show-only=json-v1` is a second machine-readable API, and the authority
on what is a test:

```sh
cd build && ctest --show-only=json-v1 |
  python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['tests'][0], indent=2))"
```

```json
{
  "backtrace": 1,
  "command": [
    ".../build/libs/rng/rng_test"
  ],
  "name": "rng_test",
  "properties": [
    {
      "name": "WORKING_DIRECTORY",
      "value": ".../build/libs/rng"
    }
  ]
}
```

(The top level also carries a `backtraceGraph` mapping each test back to the
`add_test()` call that created it, which is why piping the whole document
through `head` shows you bookkeeping rather than tests.)

Note what this buys you: matching `command[0]` against each target's
`artifacts[].path` recovers the owning target **without assuming any naming
convention**. A target called `*_test` is not a test until `add_test()` registers
it, and a test target can be named anything at all.

---

## 7. Putting it together

```
   codemodel ──▶ which targets, which sources, which include roots
   depfiles  ──▶ which headers, exactly
   directory tree + FetchContent ──▶ where the third-party boundary is
   ctest json ──▶ which tests cover the extracted code
                              │
                              ▼
                      extracted/<app>/
                        CMakeLists.txt   generated, standalone
                        src/<origin>/..  sources, flattened + namespaced
                        include/..       headers at their original rel. path
                        generated/..     generated headers, frozen
```

Run it and read the result:

```sh
python3 tools/extract_closure.py greeter --verify
cat extracted/greeter/CMakeLists.txt
```

For the clearest possible view, extract `tally` — the one app with no
third-party dependency. Its entire generated build file is:

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

Four files, no FetchContent, no `target_link_libraries`, and a build directory
with no `_deps/` — it compiles with no network access at all. The generated
`build_info.hpp` still made it in, which is the split in a nutshell: generated
code is frozen into the tree, third-party code is the only thing left to fetch.

Stage-by-stage detail is in [ALGORITHM.md](ALGORITHM.md).

---

## 8. The five traps

These are the ones this project actually hit. Each is a case where the naive
implementation *looks* right and produces a tree that builds.

### Trap 1 — `_deps/` lives under the build directory

The natural filter for "is this a generated header?" is
`path.is_relative_to(top_build)`. But FetchContent populates into
`<build>/_deps/`, so **a dependency's headers pass that test exactly like
generated ones.** In the depfile from Lab 2, `fmt/core.h` and `build_info.hpp`
are siblings under `build/`.

The result was a partial snapshot of fmt copied into `generated/` — and because
the emitted `target_include_directories(... PRIVATE generated)` is searched
*before* the include dir `fmt::fmt` contributes through its link interface, the
extracted app compiled against the stale copy instead of the pinned 10.2.1 it
fetched. It built fine. It was still wrong, and would diverge the moment the tag
moved.

**Fix:** no containment test can separate the two. Use the directory tree
(Lab 3).

### Trap 2 — INTERFACE libraries are missing from `dependencies[]`

Walk `greeter`'s edges and you get `fmt` and `input`. You do **not** get
`build_info`, even though `greeter` links it:

```cmake
target_link_libraries(greeter PRIVATE input fmt::fmt build_info)
```

`build_info` is an `INTERFACE` library — header-only, nothing to build — so CMake
omits it from `dependencies[]`, which encodes *build ordering*. A closure
algorithm that trusts the target graph alone will silently drop every
header-only dependency in your project.

**Fix:** the depfiles catch it anyway — `build_info.hpp` is in the `.d` file
because the compiler really did include it. This is the deeper lesson: the two
sources of truth cover each other's blind spots. Use both.

### Trap 3 — an in-source build dir makes the roots overlap

With `build/` inside the source tree, *every* build-tree path is also under the
source root. So `is_under(p, top_source)` and `is_under(p, top_build)` are both
true for generated headers, and which branch wins is an accident of ordering.

The symptom was `build_info.hpp` landing in `include/` for some apps and
`generated/` for others.

**Fix:** check the build-tree root first, and pick the *longest* matching include
root rather than the first. Then test with an out-of-source build too — the two
layouts exercise different paths.

### Trap 4 — test edges point the wrong way

`input_test` depends on `input`; `input` does not depend on `input_test`. Walking
outward from an app therefore reaches **no tests, ever**. That is the correct
default — a test executable is not part of what your app links — but it means
tests need a separate discovery pass (`--with-tests`).

When you add one, keep minimality: carry a test over only if every first-party
target it links is *already* in the app's closure. Otherwise a test drags in a
library the app never used, and the "minimal closure" claim quietly stops being
true. And since the libraries have been flattened away, a carried-over test must
compile the library sources it used to link — there is no `input` target left for
it to link against.

### Trap 5 — not every `.d` file in a target dir is a compiler depfile

Stage 10 finds the header closure by globbing the `.d` files under each target's
`CMakeFiles/<target>.dir/`. Every `.d` there is a per-translation-unit depfile —
until CMake 4.0, which also writes a **link-step** depfile named `link.d` into
the same directory, listing the object files, static archives and system
libraries the link consumed:

```make
greeter: \
  CMakeFiles/greeter.dir/src/main.cpp.o \
  ../../libs/input/libinput.a \
  ../../_deps/fmt-build/libfmt.a \
  ...
```

A glob of `**/CMakeFiles/<target>.dir/**/*.d` sweeps `link.d` up with the real
`*.o.d` files, and the Lab 2 parser — "drop everything up to the first `:`, split
on whitespace" — turns its body into prerequisites like
`CMakeFiles/greeter.dir/src/main.cpp.o`. Resolved against the source root (where
it does not exist) that raised a `FileNotFoundError` on copy; a `.a` under
`_deps/` would instead have been classified as a first-party header. Under CMake
3.x there was no `link.d`, so the loose glob happened to be correct.

**Fix:** match `*.o.d` specifically — that is the per-TU naming, and `link.d`
does not fit it. More broadly: a filename pattern that "is always a compiler
depfile" is an assumption the build tool can invalidate in a point release, so
pin it to the shape you actually mean.

---

## 9. Check your understanding

1. Why resolve reply filenames through `index-*.json` instead of globbing
   `target-greeter-*.json`?
2. `input_test` and `greeter` both link `fmt`. Why does `extracted/tally`
   contain no `FetchContent` block even with `--with-tests`?
3. You add a header to a library but no source `#include`s it. Does it appear in
   the extracted tree? Which stage decides?
4. Your project uses `find_package(fmt)` instead of FetchContent. Which parts of
   this pipeline break, and what would you replace them with?
5. A test's command is a shell script that runs the binary. What happens in
   `ctest_registry()`, and is that the right behavior?

<details>
<summary>Answers</summary>

1. Filenames are content-hashed and change between configures; the index is the
   only stable entry point.
2. Externals are collected per target from the *closure*, not project-wide.
   `tally` links `rng` + `build_info`, and `rng_test` links only `rng` — no path
   through either reaches `fmt`.
3. No. Stage 10 copies only what appears in a `.d` file, and an un-included header
   never reaches the preprocessor. This is exactly the minimality guarantee.
4. `parse_fetchcontent()` finds nothing, so the dependency looks first-party and
   its headers get copied. You would replace it with the `find_package()` calls
   as the dependency boundary and emit those into the generated CMakeLists —
   the *concept* (project's own declarations are the authority) is unchanged;
   only the parser differs.
5. `command[0]` matches no target artifact, so the test is skipped. Right
   behavior: the extractor can only carry over tests it can rebuild from
   codemodel data, and silently emitting a broken `add_test()` would be worse.

</details>

---

## 10. Porting this to your own project

A checklist, roughly in order of how often each one bites:

- [ ] **Turn on depfiles.** `-MMD` for GCC/Clang. MSVC has no direct equivalent;
      use `/showIncludes` and parse its output, or drive the closure from
      `compile_commands.json` plus a preprocessor pass.
- [ ] **Check your dependency boundary.** `find_package`, `vcpkg`, `conan`, and
      git submodules all need a different Stage 4 than FetchContent. The concept
      holds; the parser changes.
- [ ] **Handle multi-config generators.** This POC reads
      `configurations[0]`. Ninja Multi-Config and Visual Studio have several —
      pick deliberately, or extract per configuration.
- [ ] **Watch for basename collisions.** Sources are flattened to
      `src/<origin>/<basename>`. A target with `a/util.cpp` and `b/util.cpp`
      would have one silently overwrite the other. Add a collision check before
      you trust it on a big tree.
- [ ] **Decide about generated code.** This POC freezes generated headers as
      plain files. If yours embeds a version or a build stamp that must stay
      live, copy the `.in` template and the `configure_file()` call instead.
- [ ] **Verify by building, not by inspecting.** `--verify` configures and builds
      the extracted tree, and with `--with-tests` runs `ctest`. A closure that
      compiles proves the headers resolved; a closure that *passes its tests*
      proves the code still works. Make the stronger check the one your CI runs.

---

## 11. Running every lab at once

Everything in this tutorial through §9 is executable, and
[`tools/test_tutorial.sh`](tools/test_tutorial.sh) runs all of it end to end.
It is the tutorial as a script: the same commands, in the same order, against
this repo. Use it two ways — as a smoke test that the pipeline still works on
your machine, and as a way to see every lab's real output scroll past before you
read the prose behind it. (Section 12 is the exception — an optional lab that
needs CMake ≥ 4.0 and is deliberately left out of the script.)

```sh
bash tools/test_tutorial.sh
```

It runs from any directory (it `cd`s to the repo root itself) and reproduces, in
order:

| Section printed | What it does |
|---|---|
| `§2  cmake --graphviz` | Emits `build/deps.dot` — the lossy approach from §2 |
| `Lab 1  …` | File API query, index reader, the `greeter` target, the directory tree (§4) |
| `Lab 2  …` | Rebuilds with `-MMD`, prints the `greeter` depfile and its plain-`-I` include flags (§5) |
| `Lab 3  …` | The `FetchContent_Declare` block and the `ctest --show-only=json-v1` reply (§6) |
| `§7  extract …` | Extracts `greeter` and `tally` with `--verify`, printing each generated `CMakeLists.txt` |
| `§9 Q2  …` | Confirms `tally --with-tests` still emits no `FetchContent` |

It is strict by design: `set -euo pipefail` means it **exits non-zero on the
first command that fails**, and two labs assert their invariant explicitly (no
`_deps/` in `tally`'s build tree, no `FetchContent` in its `CMakeLists.txt`) and
abort if it does not hold. A clean run ends with `ALL LABS PASSED`, so the
script doubles as CI: if any source of truth stops telling the truth — the File
API layout shifts, a depfile goes missing, an extraction regresses — it fails
loudly instead of drifting out of sync with this document.

Everything it writes lands in the two gitignored directories from §1, `build/`
and `extracted/`, so re-running it never dirties the tree and you can delete both
freely afterward.

---

## 12. Optional lab — the CMake 4.0 link-step depfile

> **Optional, and not in `tools/test_tutorial.sh`.** This lab needs CMake ≥ 4.0
> and illustrates a *diagnostic idea*, not baseline behavior. The extractor does
> not use `link.d` today — Stage 10 only takes care to skip it (Trap 5). Read
> this if you are porting the pipeline to a project with dependencies that are
> not FetchContent.

### What `link.d` is

Trap 5 introduced it as a hazard: from CMake 4.0, the Makefile and Ninja
generators write a **link-step depfile** `link.d` next to each executable's
per-TU depfiles, and a `*.d` glob will scoop it into the header closure by
mistake. But turned around, `link.d` is a fourth build-produced fact — the
**exact list of inputs the linker consumed** to produce the binary. Nothing
derives it; the linker recorded it.

You do not ask for it — CMake 4.0+ emits it automatically. Rebuild and look:

```sh
cmake --version            # need >= 4.0 for this lab
cmake -S . -B build
cmake --build build -j
cat build/apps/greeter/CMakeFiles/greeter.dir/link.d
```

```make
greeter: \
  /usr/lib/gcc/x86_64-linux-gnu/15/../../../x86_64-linux-gnu/Scrt1.o \
  CMakeFiles/greeter.dir/src/main.cpp.o \
  ../../libs/input/libinput.a \
  ../../_deps/fmt-build/libfmt.a \
  /usr/lib/gcc/x86_64-linux-gnu/15/libstdc++.so \
  /usr/lib/x86_64-linux-gnu/libm.so.6 \
  ... (crt objects, libc, libgcc, ld.so)

CMakeFiles/greeter.dir/src/main.cpp.o:

../../libs/input/libinput.a:
...
```

Same Make shape as a compiler depfile: one rule whose prerequisites are the
link inputs, then (like `-MP`) an empty rule per prerequisite. Paths are
relative to the **target's** build dir — `build/apps/greeter` — not to the
`.dir/` the file sits in.

### The idea: a ground-truth check on everything that is not a header

The codemodel (Lab 1) tells you which targets *should* link. `link.d` tells you
what *did*. Comparing the two catches the blind spots that a pure target-graph
walk has — `OBJECT` libraries, generated sources, and above all **external
libraries that are not FetchContent**, which the extractor's Stage 4 boundary
does not see at all.

Filter the toolchain noise with the compiler's own search dirs, and classify
what remains:

```sh
python3 -c "
import pathlib, subprocess
out = subprocess.run(['gcc','-print-search-dirs'], capture_output=True, text=True).stdout
libline = next(l for l in out.splitlines() if l.startswith('libraries: '))
tool_dirs = [pathlib.Path(p).resolve() for p in libline.split('=',1)[1].split(':') if p]

d = pathlib.Path('build/apps/greeter/CMakeFiles/greeter.dir/link.d')
base = d.parent.parent.parent                       # build/apps/greeter
head = d.read_text().replace('\\\\\n',' ').split('\n\n',1)[0]
prereqs = [pathlib.Path(p) for p in head.split(':',1)[1].split()]

top = pathlib.Path.cwd()
for p in sorted({(base / q).resolve() for q in prereqs}):
    if any(t in p.parents for t in tool_dirs):
        continue                                    # provided by the toolchain
    kind = ('_deps -> must be a FetchContent dep' if '/_deps/' in str(p)
            else 'first-party (extractor copies / compiles it)')
    print(f'  {p.relative_to(top)}\n      {kind}')
"
```

```
  build/_deps/fmt-build/libfmt.a
      _deps -> must be a FetchContent dep
  build/apps/greeter/CMakeFiles/greeter.dir/src/main.cpp.o
      first-party (extractor copies / compiles it)
  build/libs/input/libinput.a
      first-party (extractor copies / compiles it)
```

Three inputs, all accounted for: one object from a first-party source, one
first-party archive, one archive under `_deps/` that the emitted
`FetchContent_Declare(fmt ...)` reproduces. **Nothing is left over.**

Now imagine `greeter` had called `find_package(SQLite3)` and linked
`SQLite::SQLite3`. It would appear here as `/usr/lib/.../libsqlite3.so` — outside
every toolchain dir, not under `_deps/`, not a first-party path. That leftover is
the signal: a dependency the FetchContent-only Stage 4 silently drops, so the
extracted `CMakeLists.txt` would omit the link and fail to build standalone. The
check turns a confusing downstream link error into a one-line warning at extract
time — which is exactly the "check your dependency boundary" item from §10, and
the "*nothing else*" promise from §1 applied in the other direction.

### Why it stays out of the baseline

Two reasons. It needs CMake ≥ 4.0, while the project floor is 3.20 (§4) and
`test_tutorial.sh` must pass on both. And it is a *diagnostic*, not a pipeline
stage: `extract_closure.py` would use `link.d` only to emit warnings, never to
decide what to copy — the three sources of truth in §3 already do that. Wiring it
in as an optional extra pass alongside Stage 4 is left as an exercise.

---

## Reference: what each API gives you

| Tool / API | Invocation | Gives you | Since |
|---|---|---|---|
| File API codemodel | `touch <build>/.cmake/api/v1/query/codemodel-v2` then reconfigure | Targets, link edges, sources, include dirs, language standard, directory tree | CMake 3.14 |
| Compiler depfiles | `-MMD` (GCC/Clang) | Exact per-TU header closure, post-preprocessor | — |
| CTest introspection | `ctest --show-only=json-v1` | Registered tests, their commands and properties | CMake 3.14 |
| Compile database | `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` | Per-TU command lines — useful cross-check | CMake 3.5 |
| Link-step depfile | `link.d` beside the object files (automatic) | Exact linker input list — objects, archives, libraries (§12) | CMake 4.0 |
| Target graph image | `cmake --graphviz=out.dot` | Human-readable graph; too lossy to build on | — |
