# Tutorial: extracting one app's build closure

**Who this is for:** developers who need to pull a single application out of a
larger CMake project — to ship it, open-source it, hand it to a partner, or
shrink a build — and want to understand *how* to do that reliably rather than by
hand-copying files until it compiles.

**What you'll learn:** which tools can tell you the truth about a build, how to
query them, and the four traps that make a naive implementation quietly wrong.

**The three docs in this repo:**

| Doc | Role |
|---|---|
| [README.md](README.md) | Quickstart — run the thing |
| **TUTORIAL.md** (you are here) | Learn the concepts and the APIs, hands-on |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |

Work through this with a terminal open. Every command below is real, and every
output shown is actual output from this project.

---

## Vocabulary

Terms used throughout. Skim now, refer back as needed.

| Term | Meaning |
|---|---|
| **Translation unit (TU)** | One source file *plus every header it includes*, as the compiler sees it after preprocessing. One `.cpp` → one TU → one object file → one depfile. It is the unit the compiler actually reasons about, which is why per-TU facts are so precise. |
| **Closure** | Everything transitively reachable from a starting point. *Target closure*: every library an app links, directly or through another library. *Header closure*: every header a TU includes, directly or through another header. |
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
3.28.3
codemodel codemodel-v2-a535780dfc73e26aabb2.json
```

Filenames are content-hashed, so **always resolve them through the index** —
never glob for `target-greeter-*.json` in production code.

### What the codemodel gives you

The codemodel lists configurations; each has `targets[]` and `directories[]`,
with per-target detail in its own file. Here is the real `greeter` target:

```json
{
  "name": "greeter",
  "id": "greeter::@31045165d3807a4b136c",
  "type": "EXECUTABLE",
  "paths": { "build": "apps/greeter", "source": "apps/greeter" },
  "dependencies": [
    { "id": "fmt::@976f4f0bee90b99ecdb6" },
    { "id": "input::@6d945ddea8f4ec024c33" }
  ],
  "sources": [
    { "path": "apps/greeter/src/main.cpp", "compileGroupIndex": 0 }
  ]
}
```

Four things to notice, because each one matters later:

- **`dependencies[].id`** are opaque ids, not names. Walk the graph by id; use
  names only for display. Extracting a closure is a depth-first walk over these
  edges — about ten lines of code (`transitive_closure()`).
- **`sources[].compileGroupIndex`** is `null` for files that are listed but not
  compiled — a header added to `add_executable()`, say. Filter on it, or you will
  try to compile a `.hpp`.
- **`paths.source`** is the *directory* that defined the target. This turns out
  to be the key to identifying third-party code (Lab 3).
- **`compileGroups[].includes[]`** (not shown) gives the real `-I` list. You need
  it to place a copied header at the same include-relative path, so that
  `#include "input/input.hpp"` still resolves without editing any source.

### The directory graph

`configurations[0].directories[]` is the piece most people miss. It is the
subdirectory tree, with `source`, `build`, `parentIndex`, `childIndexes`, and
`targetIndexes`:

```sh
python3 -c "
import json,glob
cm=json.load(open(sorted(glob.glob('build/.cmake/api/v1/reply/codemodel-v2-*.json'))[-1]))
for i,d in enumerate(cm['configurations'][0]['directories']):
    print(i, d['source'], '->', d['build'], 'parent=', d.get('parentIndex'))
"
```

```
0 . -> . parent= None
1 build/_deps/fmt-src -> _deps/fmt-build parent= 0
2 libs/rng -> libs/rng parent= 0
...
```

Look at directory 1. **`fmt`'s source directory is inside the build
directory.** Hold that thought — it is Trap 1.

> **Note on paths.** File API paths are relative to the top-level source or build
> dir when they are underneath it, and absolute otherwise. `Path(top) / p` handles
> both, because Python returns the right-hand operand when it is absolute.

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

The extractor uses the directory graph from Lab 1 (`external_regions()`), marking
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
   directory graph + FetchContent ──▶ where the third-party boundary is
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

## 8. The four traps

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

**Fix:** no containment test can separate the two. Use the directory graph
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
3. No. Stage 7 copies only what appears in a `.d` file, and an un-included header
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

## Reference: what each API gives you

| Tool / API | Invocation | Gives you | Since |
|---|---|---|---|
| File API codemodel | `touch <build>/.cmake/api/v1/query/codemodel-v2` then reconfigure | Targets, link edges, sources, include dirs, language standard, directory tree | CMake 3.14 |
| Compiler depfiles | `-MMD` (GCC/Clang) | Exact per-TU header closure, post-preprocessor | — |
| CTest introspection | `ctest --show-only=json-v1` | Registered tests, their commands and properties | CMake 3.14 |
| Compile database | `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON` | Per-TU command lines — useful cross-check | CMake 3.5 |
| Target graph image | `cmake --graphviz=out.dot` | Human-readable graph; too lossy to build on | — |
