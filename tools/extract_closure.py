#!/usr/bin/env python3
"""Extract the minimal build closure of a single CMake application target into a
flat, standalone, buildable directory.

Inputs (all derived from an already-configured build dir):
  * CMake File API codemodel  -> authoritative target graph
  * .d files, one per TU       -> precise header closure actually #included
    (TU = translation unit: one .cpp plus every header it includes)
  * CMakeLists.txt + the .cmake files it include()s (and --deps-file globs)
                              -> the FetchContent_Declare block per dependency
  * ctest --show-only          -> the registered tests (only with --with-tests)

Output (for target T, under --out, default `extracted/`):
  extracted/<T>/
    CMakeLists.txt   standalone; third-party dependencies kept via FetchContent
    src/<origin>/..  first-party sources (the application plus the libraries it
                     links), flattened and namespaced by the target they came from
    include/..       first-party headers, at their original include-relative path
    generated/..     generated headers, frozen as plain files

Third-party dependencies (e.g. fmt) are NOT copied; they are re-declared using the
same FetchContent block found in the source project, so the tree stays standalone
yet minimal.

With --with-tests, the CTest tests covering the extracted code are carried over
too (as extra executables plus add_test()), so the tree can be validated and not
just compiled. A test is taken only when every library it links is already in the
closure, so tests never enlarge the tree.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx"}


# ---------------------------------------------------------------------------
# Execution order
# ---------------------------------------------------------------------------
# main() parses the CLI and calls extract(), which drives these stages. The
# numbering matches ALGORITHM.md, where each stage is documented in full.
#
#    1. load_codemodel      -- (re)configure the build, read the File API reply
#    2. load_targets        -- index every target JSON by id / name / directory
#    3. gather_fetchcontent -- collect the CMake text (top CMakeLists.txt, every
#       + parse_fetchcontent   file it include()s, and any --deps-file globs) and
#                              lift the FetchContent_Declare blocks out of it
#    4. external_regions    -- map every third-party directory to the dependency
#                              that owns it. region_owner() then queries that map;
#                              it and is_under() are reused by stages 6, 8 and 10
#    5. transitive_closure  -- walk the target graph from the requested target to
#                              every target it links, directly or indirectly
#    6. classify            -- split that closure into first-party vs. third-party
#    7. ctest_registry + select_tests -- (--with-tests) pick the covering tests
#    8. (inline)            -- gather include roots + the C++ standard
#    9. collect_sources     -- list the source files actually compiled
#   10. parse_depfile       -- read the per-TU *.o.d depfiles for the header closure
#   11. (inline, longest_root) -- collision-check, then lay out the flat tree
#   12. write_cmakelists    -- emit the standalone CMakeLists.txt
#   13. write_readme        -- emit the README.md
#   14. verify_build        -- (--verify) configure + build + ctest the result
#
# The functions below are defined in this order, so reading top to bottom
# follows the flow of a single extract() run.
# ---------------------------------------------------------------------------


# Pipeline driver: runs stages 1..14, each implemented by a function (or an
# inline block) defined below in the order it is first reached here.
def extract(target: str, src_root: Path, build_dir: Path, out_root: Path,
            verify: bool, with_tests: bool, deps_files: list[str] | None = None,
            allow_collisions: bool = False) -> None:
    reply, codemodel = load_codemodel(build_dir)
    top_source = Path(codemodel["paths"]["source"])
    top_build = Path(codemodel["paths"]["build"])
    by_id, name_to_id, dir_of = load_targets(reply, codemodel)

    if target not in name_to_id:
        sys.exit(f"error: target '{target}' not found. "
                 f"available: {', '.join(sorted(name_to_id))}")

    fetch = gather_fetchcontent(src_root, deps_files or [])
    regions = external_regions(codemodel, by_id, dir_of, fetch, top_source, top_build)

    closure = transitive_closure(name_to_id[target], by_id)
    first_party, externals = classify(closure, by_id, regions, fetch,
                                      top_source)

    tests = []
    if with_tests:
        tests, skipped = select_tests(
            ctest_registry(build_dir, top_build, by_id),
            by_id, first_party, regions, fetch, top_source)
        for name, missing in skipped:
            print(f"note: skipping test '{name}' -- it needs "
                  f"{', '.join(missing)}, not in {target}'s closure",
                  file=sys.stderr)

    # A carried-over test has compile settings, sources and depfiles of its own,
    # so stages 8-10 work over the first-party target JSONs *plus* each test's
    # target JSON.
    contributing = first_party + [by_id[t["id"]] for t in tests]

    # --- Stage 8: the include roots and the C++ standard (from compile groups) ---
    # Every header will be re-filed relative to one of these roots so that the
    # existing `#include "a/b.hpp"` lines keep resolving.
    inc_roots: set[Path] = set()
    cxx_std = "17"
    for target_json in contributing:
        for group in target_json.get("compileGroups", []):
            for inc in group.get("includes", []):
                root = Path(inc["path"])
                if region_owner(root, regions) is None:  # skip a dependency's -I
                    inc_roots.add(root)
            std = group.get("languageStandard", {}).get("standard")
            if std:
                cxx_std = std
    src_inc_roots = [r for r in inc_roots if is_under(r, top_source)]
    gen_inc_roots = [r for r in inc_roots if is_under(r, top_build)]

    # --- Stage 9: the source files to copy (from the codemodel) ---
    # The application's own, plus every test's. A test must compile the library
    # sources it used to link (the libraries are flattened away, so there is no
    # library target left to link) -- and those are already in app_sources.
    app_sources = collect_sources(first_party, top_source)
    all_sources = set(app_sources)
    for t in tests:
        t["sources"] = collect_sources(t["first_party"], top_source)
        all_sources |= set(t["sources"])

    # --- Stage 10: the exact header set to copy (from the per-TU depfiles) ---
    headers: set[Path] = set()
    for target_json in contributing:
        # Match `<source>.o.d` only. CMake >= 4.0 also writes a link-step depfile
        # `link.d` into the same `.dir/`; it lists object files, static archives
        # and system libraries -- never headers -- so a plain `*.d` glob is wrong.
        pattern = f"**/CMakeFiles/{target_json['name']}.dir/**/*.o.d"
        for depfile in build_dir.glob(pattern):
            for prereq in parse_depfile(depfile):
                prereq = prereq.resolve()
                if prereq.suffix in SOURCE_EXTS:
                    continue  # a source, not a header
                if region_owner(prereq, regions) is not None:
                    continue  # third-party: comes back via FetchContent, not a copy
                if is_under(prereq, top_source) or is_under(prereq, top_build):
                    headers.add(prereq)  # first-party or generated -- keep it

    # --- Stage 11: lay out the flat extracted tree ---

    # Collision check, first, before writing anything. Each source is copied to
    # src/<origin>/<basename>, which drops its directory; two sources of one
    # target that share a basename would land on the same path and one would
    # silently overwrite the other. --allow-collisions downgrades this to a
    # warning (the last copy wins).
    sources_by_dest: dict[str, list[Path]] = {}
    for origin, source in all_sources:
        dest = f"src/{origin}/{source.name}"
        sources_by_dest.setdefault(dest, []).append(source)
    clashes = {dest: sources for dest, sources in sorted(sources_by_dest.items())
               if len(sources) > 1}
    for dest, sources in clashes.items():
        label = "warning" if allow_collisions else "error"
        print(f"{label}: {len(sources)} sources flatten onto {dest}:",
              file=sys.stderr)
        for source in sorted(sources):
            print(f"    {source}", file=sys.stderr)
    if clashes and not allow_collisions:
        sys.exit("error: rename the colliding files, or pass --allow-collisions "
                 "to keep only the last one")

    out = out_root / target
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)

    # Copy each source to src/<origin>/<basename> and remember its new path,
    # keyed by (origin, absolute source path), so the CMakeLists can list it.
    rel: dict[tuple[str, Path], str] = {}
    for origin, source in sorted(all_sources):
        dest = out / "src" / origin / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        rel[(origin, source)] = str(dest.relative_to(out))

    cmake_sources = [rel[s] for s in app_sources]
    for t in tests:
        t["cmake_sources"] = [rel[s] for s in t["sources"]]

    # Copy each header under include/ or generated/, keeping its include-relative
    # path so `#include "a/b.hpp"` still resolves.
    used_include, used_generated = False, False
    for header in sorted(headers):
        # Try the build-tree roots first: with an in-source build dir a build
        # path is *also* under the source root, so a generated header would
        # otherwise be misfiled into include/.
        root = longest_root(header, gen_inc_roots)
        if root is not None:
            dest = out / "generated" / header.relative_to(root)
            used_generated = True
        else:
            root = longest_root(header, src_inc_roots)
            if root is None:
                print(f"warning: no include root for {header}, using basename",
                      file=sys.stderr)
                dest = out / "include" / header.name
            else:
                dest = out / "include" / header.relative_to(root)
            used_include = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(header, dest)

    # The full external set: the application's, plus any a carried-over test
    # pulls in that the application itself does not.
    ext_names = set(externals)
    for t in tests:
        ext_names |= set(t["externals"])
    ext_names = sorted(ext_names)

    write_cmakelists(out, target, cxx_std, cmake_sources,
                     used_include, used_generated, fetch, ext_names, tests)
    write_readme(out, target, first_party, ext_names, tests)

    print(f"Extracted '{target}' -> {out}")
    print(f"  sources : {len(cmake_sources)}")
    print(f"  headers : {len(headers)}")
    print(f"  external: {', '.join(ext_names) or '(none)'}")
    if with_tests:
        print(f"  tests   : {', '.join(t['name'] for t in tests) or '(none)'}")

    if verify:
        verify_build(out, target, bool(tests))


# Stage 1: (re)configure the build dir and read the CMake File API codemodel.
def load_codemodel(build_dir: Path) -> tuple[Path, dict]:
    """Return (reply_dir, codemodel) for an already-configured build.

    The File API is request/reply: you leave an empty query file named for what
    you want, re-run CMake, and it writes JSON into a reply directory. Filenames
    there are content-hashed, so the only stable entry point is `index-*.json`.
    """
    api = build_dir / ".cmake" / "api" / "v1"
    query = api / "query"
    query.mkdir(parents=True, exist_ok=True)
    (query / "codemodel-v2").touch()  # the name *is* the request

    # A no-op reconfigure against the existing cache; it regenerates the reply.
    subprocess.run(["cmake", str(build_dir)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    reply = api / "reply"
    newest_index = sorted(reply.glob("index-*.json"))[-1]
    index = json.loads(newest_index.read_text())
    codemodel_ref = next(o for o in index["objects"] if o["kind"] == "codemodel")
    codemodel = json.loads((reply / codemodel_ref["jsonFile"]).read_text())
    return reply, codemodel


# Stage 2: index every target's JSON by id, by name, and by owning directory.
def load_targets(reply: Path, codemodel: dict) -> tuple[dict, dict, dict]:
    """Load every target's detail file and return three lookup tables:

      by_id       : target id   -> the target's parsed JSON
      name_to_id  : target name -> target id   (the CLI passes a name)
      dir_of      : target id   -> index into configurations[0].directories[]
    """
    config = codemodel["configurations"][0]
    by_id, name_to_id, dir_of = {}, {}, {}
    for ref in config["targets"]:
        target_json = json.loads((reply / ref["jsonFile"]).read_text())
        by_id[ref["id"]] = target_json
        name_to_id[target_json["name"]] = ref["id"]
        dir_of[ref["id"]] = ref["directoryIndex"]
    return by_id, name_to_id, dir_of


# Stage 3: lift the FetchContent_Declare blocks out of the CMake text.
def parse_fetchcontent(cmake_text: str) -> dict:
    """Map each declared dependency name to:

      block : the verbatim FetchContent_Declare(...) text, re-emitted as-is
      link  : the conventional `<name>::<name>` alias to link against
    """
    declarations = {}
    for match in re.finditer(r"FetchContent_Declare\s*\(\s*(\w+)(.*?)\n\)",
                             cmake_text, re.S):
        name = match.group(1)
        declarations[name] = {"block": match.group(0), "link": f"{name}::{name}"}
    return declarations


_INCLUDE_RE = re.compile(r"^[ \t]*include[ \t]*\(\s*([^)\s]+)", re.M)
_VAR_PREFIX_RE = re.compile(r"^\$\{[^}]+\}[\\/]")


def _resolve_include(arg: str, from_file: Path, src_root: Path) -> Path | None:
    """Best-effort resolution of an `include(<arg>)` argument to a file.

    Handles the common forms -- a path relative to the including file or to the
    source root, a `cmake/<Module>` name, and a single leading `${VAR}/`. A path
    that stays variable-dependent after that is skipped, not guessed.
    """
    arg = arg.strip('"').strip("'")
    arg = _VAR_PREFIX_RE.sub("", arg)
    if "$" in arg:  # still has an unresolved variable -- do not guess
        return None
    candidates = [from_file.parent / arg, src_root / arg,
                  src_root / "cmake" / arg]
    if not arg.endswith(".cmake"):  # a bare module name like `include(Deps)`
        candidates += [from_file.parent / f"{arg}.cmake",
                       src_root / f"{arg}.cmake",
                       src_root / "cmake" / f"{arg}.cmake"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


# Stage 3 (gather): collect the CMake text a FetchContent_Declare could live in.
def gather_fetchcontent(src_root: Path, extra_globs: list[str]) -> dict:
    """Scan the top CMakeLists.txt, every file it transitively `include()`s, and
    any `--deps-file` globs, then hand the concatenated text to
    parse_fetchcontent(). Over-collecting is harmless: a declaration whose name
    never lands in the closure is simply never emitted.
    """
    texts: list[str] = []
    visited: set[Path] = set()

    def scan(cmake_file: Path) -> None:
        cmake_file = cmake_file.resolve()
        if cmake_file in visited or not cmake_file.is_file():
            return
        visited.add(cmake_file)
        text = cmake_file.read_text()
        texts.append(text)
        for match in _INCLUDE_RE.finditer(text):
            included = _resolve_include(match.group(1), cmake_file, src_root)
            if included is not None:
                scan(included)

    scan(src_root / "CMakeLists.txt")
    for pattern in extra_globs:
        for extra_file in sorted(src_root.glob(pattern)):
            scan(extra_file)
    return parse_fetchcontent("\n".join(texts))


# Stage 4: map every third-party source/build directory to the dependency
# that owns it.
def external_regions(codemodel: dict, by_id: dict, dir_of: dict, fetch: dict,
                     top_source: Path, top_build: Path) -> dict[Path, str]:
    """Map every third-party directory (source *and* build side) to the
    FetchContent declaration that owns it.

    This is what keeps `_deps/` content out of the extracted tree. It cannot be
    done by root containment alone: FetchContent populates under the build dir,
    so a dependency's headers are "under top_build" exactly like genuinely
    generated ones. Two signals identify the third-party directories instead,
    both read off the configured build rather than guessed from file names:

    * a CMake directory that defines a target named after a
      `FetchContent_Declare` -- the common case (`fmt` -> target `fmt`);
    * a directory FetchContent populated at `<build>/_deps/<name>-src`, which
      also catches deps whose target names differ from the declared name.

    Child directories inherit their parent's owner, so a dependency that calls
    `add_subdirectory()` internally is covered too.
    """
    dirs = codemodel["configurations"][0]["directories"]
    owner: dict[int, str] = {}  # directory index -> owning dependency name

    # Signal 1: the directory FetchContent populates at <build>/_deps/<name>-src.
    deps_base = (top_build / "_deps").resolve()
    declared_lc = {name.lower(): name for name in fetch}
    for idx, directory in enumerate(dirs):
        src = (top_source / directory["source"]).resolve()
        if src.parent == deps_base and src.name.endswith("-src"):
            name = declared_lc.get(src.name[:-len("-src")].lower())
            if name:
                owner[idx] = name

    # Signal 2: a non-top-level directory that defines a target named after a
    # declaration. A top-level directory is the project's own by definition, so a
    # name collision there must never mark the whole tree third-party.
    top_level = {idx for idx, d in enumerate(dirs) if "parentIndex" not in d}
    for target_id, target_json in by_id.items():
        directory_idx = dir_of[target_id]
        if target_json["name"] in fetch and directory_idx not in top_level:
            owner.setdefault(directory_idx, target_json["name"])

    # Ownership flows down to child directories.
    stack = list(owner)
    while stack:
        idx = stack.pop()
        for child in dirs[idx].get("childIndexes", []):
            if child not in owner:
                owner[child] = owner[idx]
                stack.append(child)

    # Emit both the source-side and build-side path of every owned directory.
    regions: dict[Path, str] = {}
    for idx, name in owner.items():
        regions[(top_source / dirs[idx]["source"]).resolve()] = name
        regions[(top_build / dirs[idx]["build"]).resolve()] = name
    return regions


# Stage 5: transitively walk the target graph from the requested target.
def transitive_closure(root_id: str, by_id: dict) -> set[str]:
    """Every target id reachable from `root_id` by following `dependencies[]`
    edges -- i.e. everything the root links, directly or through another
    library. A plain depth-first graph walk with a `seen` set to stop cycles.
    """
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        target_id = stack.pop()
        if target_id in seen:
            continue
        seen.add(target_id)
        for edge in by_id[target_id].get("dependencies", []):
            stack.append(edge["id"])
    return seen


# Stage 6: split a closure of target ids into first-party vs. third-party.
def classify(target_ids, by_id: dict, regions: dict, fetch: dict,
             top_source: Path) -> tuple[list, list]:
    """Partition a set of target ids into:

      first_party : the target JSONs whose sources we copy
      externals   : the names of the FetchContent dependencies we re-declare
    """
    first_party, externals = [], []
    for target_id in sorted(target_ids):
        target_json = by_id[target_id]
        target_dir = (top_source / target_json["paths"]["source"]).resolve()
        # Third-party if the target's own directory is inside a dependency's
        # region. The name fallback still catches a dependency that is declared
        # but not yet populated (so it has no directory of its own yet).
        owner = region_owner(target_dir, regions)
        if owner is None and target_json["name"] in fetch:
            owner = target_json["name"]
        if owner is not None:
            externals.append(owner)
        else:
            first_party.append(target_json)
    return first_party, externals


# Queries the `regions` map from Stage 4. Called by classify (6), the include-
# root scan (8) and the header filter (10).
def region_owner(path: Path, regions: dict[Path, str]) -> str | None:
    """The dependency that owns `path`, or None if `path` is first-party.

    A path can sit under several region roots (a dependency's own
    subdirectories); the deepest (longest) matching root wins.
    """
    best_root, best_name = None, None
    for root, name in regions.items():
        if is_under(path, root) and (best_root is None
                                     or len(str(root)) > len(str(best_root))):
            best_root, best_name = root, name
    return best_name


# Low-level path test, used by region_owner and the Stage 8 / 10 filtering.
def is_under(path: Path, root: Path) -> bool:
    """True when `path` is `root` or sits somewhere beneath it."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# Stage 7a (--with-tests): map each registered CTest test name to its target id.
def ctest_registry(build_dir: Path, top_build: Path,
                   by_id: dict) -> dict[str, str]:
    """Map registered test name -> target id.

    `ctest --show-only=json-v1` is the authority for what is actually a test;
    the owning target is then recovered by matching each test's command against
    the targets' build artifacts. Nothing is inferred from naming conventions
    like `*_test`, so a test target named anything at all is still found.
    """
    try:
        out = subprocess.run(["ctest", "--show-only=json-v1"], cwd=build_dir,
                             check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        print("warning: could not query ctest; extracting no tests",
              file=sys.stderr)
        return {}

    # Index every target by the absolute path of the artifact it produces.
    target_of_artifact = {}
    for target_id, target_json in by_id.items():
        for artifact in target_json.get("artifacts", []):
            target_of_artifact[(top_build / artifact["path"]).resolve()] = target_id

    registry = {}
    for test in json.loads(out).get("tests", []):
        command = test.get("command") or []
        if not command:
            continue
        target_id = target_of_artifact.get(Path(command[0]).resolve())
        if target_id:  # skip tests that run a script or an external program
            registry[test["name"]] = target_id
    return registry


# Stage 7b (--with-tests): keep only tests fully covered by the app's closure.
def select_tests(registry: dict, by_id: dict, first_party: list, regions: dict,
                 fetch: dict, top_source: Path) -> tuple[list, list]:
    """Pick the tests that exercise code the extracted tree already contains.

    A test is taken only when every first-party target it links (beyond itself)
    is already in the application's closure. That preserves minimality: adding
    tests can never drag in a library the application itself does not use. A
    test covering code outside the closure is reported as skipped rather than
    silently dropped.
    """
    already_have = {target["name"] for target in first_party}
    selected, skipped = [], []
    for test_name, test_id in sorted(registry.items()):
        test_first_party, test_externals = classify(
            transitive_closure(test_id, by_id), by_id, regions, fetch, top_source)
        # The first-party libraries this test needs, beyond the test itself.
        needs = ({target["name"] for target in test_first_party}
                 - {by_id[test_id]["name"]})
        if not needs:
            continue  # self-contained: exercises none of the closure's code
        if needs <= already_have:
            selected.append({"name": test_name, "target": by_id[test_id]["name"],
                             "id": test_id, "first_party": test_first_party,
                             "externals": sorted(set(test_externals))})
        else:
            skipped.append((test_name, sorted(needs - already_have)))
    return selected, skipped


# Stage 9: list the source files actually compiled for the given targets.
def collect_sources(targets: list, top_source: Path) -> list:
    """For every source that is actually compiled, a `(origin, absolute path)`
    pair, where `origin` is the name of the target it belongs to (used to
    namespace it on copy).
    """
    found = []
    for target_json in targets:
        for source in target_json.get("sources", []):
            if source.get("compileGroupIndex") is None:
                continue  # listed but not compiled (e.g. a header in the target)
            path = (top_source / source["path"]).resolve()
            if path.suffix in SOURCE_EXTS:
                found.append((target_json["name"], path))
    return sorted(set(found))


# Stage 10: read one GCC/Clang `.d` depfile into the set of paths it names.
def parse_depfile(path: Path) -> set[Path]:
    """A depfile is a Make rule: `foo.o: /path/a.hpp /path/b.hpp \\<newline> ...`.
    Join the backslash-newline continuations, drop everything up to the first
    `:` (the target), and split the remaining prerequisites on whitespace.
    """
    text = path.read_text().replace("\\\n", " ")
    if ":" in text:
        text = text.split(":", 1)[1]
    return {Path(token) for token in text.split() if token and token != "\\"}


# Helper for stage 11: pick the most specific include root a header sits under.
def longest_root(path: Path, roots: list[Path]) -> Path | None:
    """The root in `roots` that contains `path` and is the deepest (its string
    is longest). "Deepest wins" so that a header under a nested include dir is
    filed relative to that dir, not to some shorter root it also sits under.
    Returns None if no root contains `path`.
    """
    best = None
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue  # path is not under this root
        if best is None or len(str(root)) > len(str(best)):
            best = root
    return best


# Stage 12: emit the standalone CMakeLists.txt for the extracted tree.
def write_cmakelists(out: Path, target: str, cxx_std: str,
                     cmake_sources: list[str], used_include: bool,
                     used_generated: bool, fetch: dict, externals: list[str],
                     tests: list[dict]) -> None:
    lines = [
        "cmake_minimum_required(VERSION 3.20)",
        f"project({target}_standalone LANGUAGES CXX)",
        "",
        f"set(CMAKE_CXX_STANDARD {cxx_std})",
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
        "",
    ]
    if externals:
        lines.append("include(FetchContent)")
        for name in externals:
            lines.append(fetch[name]["block"])
        lines.append(f"FetchContent_MakeAvailable({' '.join(externals)})")
        lines.append("")

    include_dirs = []
    if used_include:
        include_dirs.append("include")
    if used_generated:
        include_dirs.append("generated")

    def emit_executable(name: str, sources: list[str],
                        links: list[str]) -> None:
        """Append one add_executable() + its target_* calls. `links` is the list
        of external dependency names this executable links."""
        lines.append(f"add_executable({name}")
        for source in sources:
            lines.append(f"  {source}")
        lines.append(")")
        if include_dirs:
            lines.append(f"target_include_directories({name} PRIVATE "
                         f"{' '.join(include_dirs)})")
        if links:
            aliases = " ".join(fetch[dep]["link"] for dep in links)
            lines.append(f"target_link_libraries({name} PRIVATE {aliases})")

    emit_executable(target, cmake_sources, externals)

    if tests:
        lines.append("")
        lines.append("enable_testing()")
        for test in tests:
            lines.append("")
            emit_executable(test["target"], test["cmake_sources"],
                            test["externals"])
            lines.append(f"add_test(NAME {test['name']} COMMAND {test['target']})")

    lines.append("")
    (out / "CMakeLists.txt").write_text("\n".join(lines))


# Stage 13: emit the README.md for the extracted tree.
def write_readme(out: Path, target: str, first_party: list[dict],
                 externals: list[str], tests: list[dict]) -> None:
    first_party_names = ", ".join(sorted(t["name"] for t in first_party))
    external_names = ", ".join(externals) or "(none)"
    carried_test_names = ", ".join(t["name"] for t in tests)
    body = (
        f"# {target} (extracted standalone closure)\n\n"
        f"Minimal build closure for `{target}`, extracted from the parent "
        f"CMake project into a flat, standalone tree.\n\n"
        f"- First-party targets folded in: {first_party_names}\n"
        f"- Third-party deps (via FetchContent): {external_names}\n")
    if tests:
        body += f"- Tests carried over: {carried_test_names}\n"
    body += ("\n## Build\n\n"
             "```sh\ncmake -S . -B build\ncmake --build build -j\n```\n")
    if tests:
        body += "\n## Test\n\n```sh\nctest --test-dir build\n```\n"
    (out / "README.md").write_text(body)


# Stage 14 (--verify): configure, build and (if any tests) ctest the extracted tree.
def verify_build(out: Path, target: str, run_tests: bool) -> None:
    print(f"\n--- verifying build of extracted '{target}' ---")
    subprocess.run(["cmake", "-S", str(out), "-B", str(out / "build")],
                   check=True)
    subprocess.run(["cmake", "--build", str(out / "build"), "-j"], check=True)
    if run_tests:
        subprocess.run(["ctest", "--output-on-failure"], cwd=out / "build",
                       check=True)
    print(f"--- OK: {out / 'build'} built"
          f"{' and tested' if run_tests else ''} successfully ---")


# Entry point: parse CLI args, then hand off to extract().
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="application target to extract")
    ap.add_argument("--src", type=Path, default=Path("."),
                    help="source root (default: .)")
    ap.add_argument("--build", type=Path, default=Path("build"),
                    help="configured build dir (default: build)")
    ap.add_argument("--out", type=Path, default=Path("extracted"),
                    help="output root (default: extracted)")
    ap.add_argument("--verify", action="store_true",
                    help="configure+build the extracted tree to prove it stands alone "
                         "(also runs ctest when --with-tests carried tests over)")
    ap.add_argument("--with-tests", action="store_true",
                    help="also carry over the registered CTest tests that cover "
                         "the extracted code")
    ap.add_argument("--deps-file", action="append", default=[], metavar="GLOB",
                    help="extra CMake file(s) to scan for FetchContent_Declare "
                         "(glob relative to --src; repeatable). The top "
                         "CMakeLists.txt and every file it include()s are always "
                         "scanned")
    ap.add_argument("--allow-collisions", action="store_true",
                    help="proceed when two sources of one target flatten to the "
                         "same src/<origin>/<basename> path (last one wins) "
                         "instead of aborting")
    args = ap.parse_args()

    extract(args.target, args.src.resolve(), args.build.resolve(),
            args.out.resolve(), args.verify, args.with_tests,
            args.deps_file, args.allow_collisions)


if __name__ == "__main__":
    main()
