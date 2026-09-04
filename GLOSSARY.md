# Glossary

The canonical vocabulary for this repository — the code and every document draw
their terms from here.

| Doc | Role |
|---|---|
| [SUMMARY.md](SUMMARY.md) | Executive summary — value, and the process end to end |
| [README.md](README.md) | Quickstart — run the thing |
| [TUTORIAL.md](TUTORIAL.md) | Learn the concepts and the APIs, hands-on |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |
| **GLOSSARY.md** (you are here) | Canonical vocabulary, shared by the code and every document |
| [SAMPLES.md](SAMPLES.md) | The file formats extraction reads and writes, with real samples |

**The rule: one concept, one term, one spelling.** If a word appears in a
docstring, a comment, an identifier, or a document, it means what this file says
it means. When two words could serve, section 5 records which one wins.

**How to use it**

- **Reading the code or docs?** Look a term up here; the definitions assume no
  prior CMake or build-system knowledge.
- **Writing docs?** Link to this file instead of redefining a term inline.
- **Writing code?** Name things with these words. Section 4 maps every
  project-specific term to the identifiers that carry it, so a reader can get
  from a document to the function that implements it.

| Section | Covers |
|---|---|
| [1. Graphs and algorithms](#1-graphs-and-algorithms) | The graph vocabulary, and every algorithm the extractor runs |
| [2. C and C++](#2-c-and-c) | Preprocessor, translation units, object files, linking, headers |
| [3. CMake](#3-cmake) | Targets, linking, the run model, the machine-readable interfaces, dependencies |
| [4. This project](#4-this-project) | Terms this repo coined, each tied to the code that implements it |
| [5. Naming rules](#5-naming-rules) | Which spelling wins when several are possible |

---

## 1. Graphs and algorithms

Extraction is graph work. The standard terms are used precisely, so they are
worth pinning down even if you have met them before.

### 1.1 Graph vocabulary

| Term | Meaning |
|---|---|
| **Directed graph** | A set of **nodes** joined by **edges** that each point one way (A → B). Here the nodes are CMake targets and every "A links B" is an edge. |
| **Node** | One vertex of the graph. In the target graph, one CMake target. |
| **Edge** | A one-way connection between two nodes. In the target graph, "links against". |
| **Path** | A chain of edges leading from one node to another. |
| **Reachable** | B is reachable from A when some path leads A → … → B. |
| **Transitive** | Holding across a chain of edges: if A → B and B → C then C is *transitively* reachable from A, even with no direct A → C edge. |
| **Closure** | Everything transitively reachable from a starting node — the set you get by following every edge outward until nothing new appears. See **target closure** and **header closure** in section 4. |
| **Traversal** (or **walk**) | Visiting every reachable node exactly once. |
| **Depth-first** | A traversal order: follow one edge as far as it leads, then back up and try the next. The opposite is breadth-first (all neighbours, then their neighbours). |
| **Root** | The node a traversal starts from — for us, the application target being extracted. |
| **Leaf** | A node with no outgoing edges. `rng` links nothing, so it is a leaf. |
| **Cycle** | A path that returns to where it started. Target graphs should not contain one, but a traversal must not hang if they do. |
| **Tree** | A graph with a single root and exactly one path to every node. CMake's `directories[]` is a tree; the target graph is **not**, because two applications can link the same library. |

### 1.2 The algorithms used

Every algorithm in the extractor, what it does, and why that one. All of them are
small — the difficulty in this project is knowing *which facts to feed them*, not
the algorithms themselves.

| Algorithm | In the code | What it does |
|---|---|---|
| **Depth-first traversal** | `transitive_closure()` | Walks the target graph outward from one root, using an explicit `stack` (a list) and a `seen` set. Iterative rather than recursive, so a deep graph cannot exhaust the Python stack; the `seen` set both prevents rework and makes cycles safe. Cost is linear in nodes plus edges. |
| **Transitive closure** | `transitive_closure()` | The *result* of that walk: every target the root links, directly or through another library. This is the set whose code may end up in the binary. |
| **Longest-root match** | `longest_root()`, `region_owner()` | Given a path and several candidate roots that contain it, pick the **deepest** one (longest path string). "Most specific wins" — so a header under a nested include directory is placed relative to that directory, not to some shorter root it also happens to sit under. Both functions are the same idea applied to different data: one to include roots, one to third-party regions. |
| **Path containment** | `is_under()` | Is this path at, or beneath, that root? Implemented with `Path.relative_to()`, catching the exception as "no". The primitive under both longest-root matches. |
| **Partition** | `classify()` | Splits one closure into two lists — first-party targets whose code is copied, third-party names that are re-declared. A single pass with a predicate. |
| **Subset test** | `select_tests()` | `needs <= already_have` on Python sets: carry a test over only when every library it links is already in the closure. This is what stops tests from enlarging the extracted tree. |
| **Set difference** | `select_tests()` | `needs - already_have` names precisely which libraries were missing, so a skipped test is reported with a reason rather than dropped silently. |
| **Deduplicate and sort** | `sorted(set(...))`, used throughout | Collapses repeats — a depfile may name the same header several times — and fixes an order, so two runs on the same build produce byte-identical output. |

---

## 2. C and C++

| Term | Meaning |
|---|---|
| **Preprocessor** | The first pass the compiler runs. It executes `#include` (pasting the named file in as text), `#define`, and `#if`. Its output is the translation unit. |
| **Translation unit** (**TU**) | One source file *plus every header it includes*, as the compiler sees it after preprocessing. One `.cpp` → one TU → one object file → one depfile. Per-TU facts are precise because the compiler actually computed them. |
| **Object file** (`.o`) | The compiled form of one translation unit: machine code with unresolved references to things defined elsewhere. |
| **Compiler** | Turns one source file into one object file. `-MMD` is a compiler flag. |
| **Linker** | Combines object files and libraries into an executable, resolving the cross-references the compiler left open. |
| **Static library** / **archive** (`.a`) | A bundle of object files. Linking against it copies in only the objects actually referenced; nothing remains to resolve at run time. |
| **Include path** | The list of directories (`-I`) the preprocessor searches for angle-bracket includes. |
| **Include-relative path** | A header's path *below* the include root that exposes it — `input/input.hpp` for `libs/input/include/input/input.hpp`. Preserving it is what keeps `#include <input/input.hpp>` resolving after a copy. |
| **Public header** | A header reachable through an include path, included as `#include <pkg/x.hpp>`. Copied to `include/…` at its include-relative path. |
| **Private header** | A header reached only by a file-relative `#include "sibling.hpp"` from a source next to it. Copied to `src/<origin>/…` beside that source. |
| **System header** | A header from a compiler- or system-owned directory, or one reached through `-isystem`. `-MMD` deliberately leaves these out of depfiles. |

---

## 3. CMake

### 3.1 Targets and linking

| Term | Meaning |
|---|---|
| **Target** | The unit CMake builds or tracks — an executable, a library, or a bookkeeping "utility" target — created by `add_executable()` / `add_library()`. Every node of the target graph is a target, and everything the extractor copies belongs to one. |
| **Executable** | A target that links to a runnable program. |
| **Library** | A target carrying code for others to link: `STATIC` (an `.a`, the case here), `SHARED` (an `.so`), `OBJECT` (loose `.o` files), `MODULE` (a plugin), or `INTERFACE` (below). |
| **INTERFACE library** | A target that compiles nothing and exists only to carry usage requirements to whatever links it. `build_info` is one. Absent from `dependencies[]` — see the tutorial's Trap 2. |
| **Link** / **links against** | `target_link_libraries(A B)` makes A link B: B's compiled code and its usage requirements flow into A. These edges are what the target graph is made of. |
| **Usage requirements** | The include directories, defines, flags and onward links a target exports to everything that links it — the `PUBLIC` / `INTERFACE` arguments of the `target_*` commands. |
| **Imported target** | A target standing in for something built outside this project, e.g. from `find_package()`. Conventionally namespaced (`fmt::fmt`, `Threads::Threads`); that **imported-target name** is what you pass to `target_link_libraries()`. |
| **`target_include_directories()`** | Adds directories to a target's include path. The extractor emits one per executable (`PRIVATE include generated`) so the copied headers resolve. |
| **Compile group** | A set of a target's sources sharing compile settings — language, include directories, defines, standard (`compileGroups[]` in the codemodel). |

### 3.2 Running CMake

| Term | Meaning |
|---|---|
| **Configure** | The phase that runs the `CMakeLists.txt` files against the cache, deciding what the build contains. |
| **Generate** | The phase that writes the actual build files (Makefiles, Ninja files) afterwards. |
| **Reconfigure** | Running `cmake <build>` again with nothing changed: a no-op for the build, but it still refreshes the File API reply. This is how the extractor gets a reply without disturbing anything. |
| **Cache** (`CMakeCache.txt`) | The variables the first configure persists — compiler, options, resolved paths. Later configures reuse it. |
| **Generator** | The build system CMake emits for (Unix Makefiles, Ninja, Visual Studio). |
| **Generator expression** | CMake's `$<...>` syntax, evaluated at *generate* time rather than when the `CMakeLists.txt` is read. One reason that file cannot simply be parsed. |
| **In-source / out-of-source build** | Whether the build directory sits inside the source tree (`./build/`, this project's default) or outside it. See the tutorial's Trap 3. |

### 3.3 Machine-readable interfaces

These are the interfaces that make the whole approach possible: each reports what
the build *actually* did, rather than what a file suggests it might do.

| Term | Meaning |
|---|---|
| **File API** | CMake's machine-readable interface (CMake ≥ 3.14). A file-based request/response protocol: drop an empty, specially-named file in the **query** directory, reconfigure, and read the JSON CMake writes to the **reply** directory. |
| **Query** / **reply** | The two halves of that protocol — `<build>/.cmake/api/v1/query/` and `…/reply/`. Reply filenames are content-hashed, so always resolve them through the reply index rather than globbing. |
| **Codemodel** | The File API object describing a configured build: its targets, their sources, include directories and link edges. The authority for the target graph. |
| **Command trace** | The log CMake writes under `--trace-expand --trace-format=json-v1`: one **trace record** per command it runs, with every argument already variable-expanded. The authority for the dependency boundary. |
| **Trace record** | One entry in that log — `{"cmd", "args", "file", "line"}`. |
| **CTest** | CMake's test runner. `ctest --show-only=json-v1` reports the **registered tests** and their commands. |
| **Registered test** | A test an `add_test()` call created. A target merely *named* `*_test` is not a test until it is registered — which is why the extractor asks CTest rather than pattern-matching names. |
| **`enable_testing()`** | Switches CTest on for a project. The extractor emits it before the carried-over tests, so `ctest` works in the extracted tree. |
| **Artifact** | The file a target produces (`artifacts[].path` in the codemodel). Matching a test's command against these recovers which target the test runs. |
| **Depfile** (`.d` file) | A small Makefile-syntax file the compiler writes next to each object file, listing that TU's header closure. Produced by `-MMD`. The authority for the header closure. |
| **Compile database** | `compile_commands.json`, the per-TU command lines, emitted with `-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`. Useful as a cross-check. |

### 3.4 Dependencies

| Term | Meaning |
|---|---|
| **FetchContent** | CMake's dependency fetcher. `FetchContent_Declare(<name> GIT_REPOSITORY … GIT_TAG …)` plus `FetchContent_MakeAvailable(<name>)` clones a pinned external at configure time and adds it with `add_subdirectory()`. `fmt` arrives this way. |
| **Declare block** | One regenerated `FetchContent_Declare(...)` call, rebuilt from the arguments the trace recorded and emitted into the extracted `CMakeLists.txt`. |
| **`find_package()`** | The other way a project names an external: ask the host toolchain for something already installed. Re-emitted verbatim rather than regenerated. |
| **Population directory** | Where FetchContent unpacks a dependency, by default `<build>/_deps/<name>-src`. That it lives *under the build directory* is the source of the tutorial's Trap 1. |
| **Generated code** | Sources or headers produced during the build rather than committed — here `build_info.hpp`, which `configure_file()` fills in from a `.in` template. The extractor freezes it into the tree as a plain file. |

---

## 4. This project

Terms this repository coined or uses in a specific sense. The **In the code**
column is the bridge: it names the identifiers that carry each concept, so you
can move from a document straight to the implementation.

| Term | Meaning | In the code |
|---|---|---|
| **Extractor** | `tools/extract_closure.py`, the program this repo is about. | — |
| **Source project** | The project being extracted *from*. Never "parent project". | `src_root`, `top_source` |
| **Extracted tree** | The standalone directory the extractor writes, at `extracted/<target>/`. | `out`, `out_root` |
| **Sample** | A self-contained example project under `samples/` to extract from. Never "fixture". | — |
| **Target closure** | Every target an application links, directly or indirectly — the output of the depth-first walk. | `transitive_closure()`, `closure` |
| **Header closure** | Every header a translation unit actually included, transitively. Read from depfiles, never guessed. | `parse_depfile()`, `headers` |
| **First-party** | Code the source project owns. Copied into the extracted tree. | `first_party` |
| **Third-party** | Code the source project pulls in from outside. Re-declared, never copied. "External" is the accepted synonym. | `externals` |
| **Dependency boundary** | The line between what gets copied and what gets re-declared. Drawn from the command trace, never from a path pattern. | `classify()`, `regions` |
| **Region** | A directory belonging to one third-party dependency — both its source and its build side. Any file inside one is third-party by definition. The map from region to owning dependency is how the boundary is enforced. | `external_regions()`, `region_owner()`, `regions` |
| **Include root** | A directory on the include path that a public header is found beneath. A header's placement in the extracted tree is its path relative to the deepest include root containing it. | `inc_roots`, `src_inc_roots`, `gen_inc_roots`, `longest_root()` |
| **Origin** | For a first-party source or private header, the target it belongs to. Names the `src/<origin>/` sub-directory the file is copied into. | `origin`, `collect_sources()` |
| **Fold in** | A first-party library is *folded into* its consumer: the library target disappears and its sources compile straight into the executable (and into each carried-over test). Files keep their sub-directory structure — nothing is flattened. | Stage 11 |
| **Place** | Choosing a file's path inside the extracted tree, as distinct from the **copy** that then writes it there. | `place_source()`, `place_header()` |
| **Collision** | Two distinct source files placed at the same path in the extracted tree. Detected before any copying; `--allow-collisions` downgrades the error to a warning. | `allow_collisions` |
| **Covering test** | A registered test whose linked libraries are all already in the application's closure — so carrying it over validates the extracted code without enlarging the tree. | `select_tests()` |
| **Link line** | The `target_link_libraries(<exe> PRIVATE …)` the extractor emits for one executable. | `write_cmakelists()` |
| **Link token** | One entry on a link line — `fmt::fmt`, `input`. | `traced_link_tokens()` |
| **Ground truth** | A fact recorded by the tool that did the work (CMake, the compiler), rather than one re-derived by inspecting files afterwards. The whole design prefers the former. | — |
| **Stage** | One numbered step of the pipeline. The numbering is shared by `extract()`, its comments, and ALGORITHM.md. | `extract()` |
| **POC** | Proof of concept. This repo demonstrates the approach rather than being production-hardened. | — |

---

## 5. Naming rules

Where several words could serve, these win. Most exist because the losing term
was actively misleading.

| Use | Not | Why |
|---|---|---|
| source project | parent project | Nothing is nested; "parent" wrongly suggests a hierarchy. |
| sample | fixture, example project | `samples/` is the directory name; "fixture" implies a test harness. |
| third-party | external *(in prose)* | One word for one idea. `externals` survives as the identifier because it names a list of dependency names. |
| fold in | flatten | Stage 11 **preserves** directory structure. "Flatten" describes what it deliberately stopped doing. |
| extracted tree | output tree, generated project | Matches `extracted/`, and "generated" already means `configure_file()` output. |
| registered test | test target | A target is only a test once `add_test()` registers it — the distinction the extractor depends on. |
| place | assign, map | Reserved for choosing a destination path, so it stays distinct from the copy. |
| region | zone, area | Matches `external_regions()` / `region_owner()`. |
| translation unit (TU) | compilation unit | Matches the C++ standard's wording. Expand on first use in each document. |
| depfile | dependency file | Unambiguous, and matches `.d` conventions. |
