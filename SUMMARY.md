# Executive summary

**What this is.** A tool that takes one application out of a shared C/C++
codebase and produces a self-contained project containing that application and
nothing else — buildable, testable, and with no reference back to the codebase it
came from.

**Why it matters.** Shipping one component of a monorepo today means either
handing over the whole repository or hand-curating a subset. The first
over-discloses; the second is slow, error-prone, and stale the moment anyone
touches the source. This makes the subset a *derived artefact*: produced on
demand in under a second, from what the build system and compiler already
recorded.

| Doc | Role |
|---|---|
| **SUMMARY.md** (you are here) | Value, and the process end to end |
| [README.md](README.md) | Quickstart — run it |
| [TUTORIAL.md](TUTORIAL.md) | Learn the concepts and the APIs, hands-on |
| [ALGORITHM.md](ALGORITHM.md) | Reference — every stage, input/output/algorithm |
| [GLOSSARY.md](GLOSSARY.md) | Canonical vocabulary, shared by the code and every document |
| [SAMPLES.md](SAMPLES.md) | The file formats it reads and writes, with real samples |

---

## The business case

**Deliver a component without delivering the codebase.** Customer handovers,
partner integrations, open-sourcing one library, escrow deposits. The recipient
gets a project that builds; they do not get the products next to it in your repo.

**Narrow the scope of an audit.** A security review, licence audit or
certification exercise costs in proportion to the code in scope. Handing an
auditor exactly the code one product compiles — provably, not by assertion — is a
smaller and more defensible engagement.

**Keep the supply-chain story intact.** Third-party dependencies are *not*
copied into the deliverable. They are re-declared with their original pinned
versions, so the recipient fetches the same upstream you did. Nothing is
silently forked, and your SBOM still describes reality.

**Retire a class of manual error.** "Did we ship more than we meant to?" and
"does the thing we shipped actually build?" both become questions a machine
answers, on every run, rather than questions a person signs off.

## What makes the result trustworthy

The tool never reads your `CMakeLists.txt` files, and never maintains a parallel
description of the project that could drift from it.

Instead it consumes what the build **already produced**: CMake's machine-readable
model of the configured build, the trace of the commands CMake actually executed,
the per-file dependency records the compiler emitted while compiling, and the
test registry. Each answers a question no other source can answer honestly.

Two consequences follow, and they are the architectural point:

- **It cannot go stale.** There is no curated list to maintain. Change the
  source, rebuild, re-extract; the output follows automatically.
- **Minimality is a property, not a promise.** A file is in the deliverable only
  because the compiler recorded that it was used. Unused code cannot be included
  by accident, and used code cannot be omitted by oversight.

## The process, end to end

Four steps. Only the second is new work; the rest is ordinary CMake.

**1 — Configure.** Build the source project the way you already do, with one
extra compiler flag (`-MMD`) so the compiler records which headers each file
used. Nothing else about the project changes: no restructuring, no annotations,
no second build system. The flag is harmless to normal builds and is commonly
enabled already.

**2 — Extract.** Point the tool at one application target. It reads the
configured build, works out exactly which first-party sources and headers that
application reaches, which third-party dependencies it needs, and which tests
cover the result — then writes a standalone project. Sub-second for a single
application.

**3 — Build.** The output is an ordinary CMake project with a single generated
`CMakeLists.txt`. `cmake -S . -B build && cmake --build build`. It has no
dependency on the originating repository, and no knowledge that it was ever part
of one.

**4 — Test.** The tests that cover the extracted code are carried across and run
with `ctest`. This is the acceptance gate: the deliverable is not merely
*claimed* complete, it is demonstrated to compile and pass its own tests in
isolation. The tool can run steps 3 and 4 itself as a self-check.

## Evidence

Measured against the two sample projects in this repository. They are
demonstration-scale, so read the ratios as illustrating the mechanism rather than
predicting numbers for a large codebase.

| Property | Result |
|---|---|
| Build configuration delivered | **1** generated `CMakeLists.txt`, from 15 across the source project |
| References back to the source repository | **0** files, in any deliverable |
| Third-party source copied into deliverables | **0 bytes** — 1,359 files and ~197 MB across two dependencies, replaced by 12 lines of pinned declaration |
| First-party libraries included | only those reached: 2 of 6 for one application, 6 of 6 for the one that uses everything |
| Extraction time | **0.68 s** for one application against a warm build |
| Verification | 9 of 9 applications across both samples extract, build and pass their tests standalone |

The last row is the one that matters operationally: verification is a build and a
test run, not a code review.

## What it costs, and what it does not do

Stated plainly, because these determine whether it fits.

**Prerequisites.** CMake 3.21 or newer, a configured and built tree, and `-MMD`
on the compile line. GCC and Clang are supported; **MSVC is not** — it produces
header-dependency information in a different form that the tool does not yet
read.

**Dependency mechanisms.** FetchContent and `find_package` are handled directly.
vcpkg and Conan mostly operate *through* `find_package`, so they come out the
same way — with the caveat that the toolchain file which made those packages
findable does not travel with the deliverable and has to be documented for the
recipient. A vendored git submodule brought in with `add_subdirectory()` is a
genuine gap: it has no declaration to re-emit, so it needs work before a codebase
using one can be extracted reliably.

**Maturity.** This is a proof of concept. The approach is validated end to end
against two sample projects, including a deliberately awkward one with a deep
library tree, an object library, several dependencies, and filename collisions.
It has not been run against a large production codebase, and
[TUTORIAL.md §10–11](TUTORIAL.md) documents the specific issues to expect at
scale.

**Not a substitute for a build system.** This extracts a deliverable from a
build; it does not replace one. The source project remains the source of truth,
and every extraction is disposable.

## Recommendation

The approach is sound and the mechanism is proven. The natural next step is a
trial extraction from one real product in an existing codebase — which will
surface the dependency mechanisms and scale characteristics that the samples
cannot, at a cost of roughly the effort of one build.
