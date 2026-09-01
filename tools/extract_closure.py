#!/usr/bin/env python3
"""Extract the minimal build closure of a single CMake application target into a
flat, standalone, buildable directory.

Inputs (all derived from an already-configured build dir):
  * CMake File API codemodel  -> authoritative target graph
  * .d files, one per TU       -> precise header closure actually #included
    (TU = translation unit: one .cpp plus every header it includes)
  * CMakeLists.txt + include()d .cmake files (and --deps-file globs)
                              -> FetchContent_Declare blocks for third-party deps
  * ctest --show-only          -> the registered tests (only with --with-tests)

Output (for target T):
  out/<T>/
    CMakeLists.txt   standalone; third-party deps kept via FetchContent
    src/<origin>/..  first-party sources (app + linked libs), flattened
    include/..       first-party headers, at their original include-relative path
    generated/..     generated headers, frozen as plain files

Third-party dependencies (e.g. fmt) are NOT copied; they are re-declared via the
same FetchContent block found in the source project, so the tree stays standalone
yet minimal.

With --with-tests, the CTest tests covering the extracted code are carried over
too (as extra executables plus add_test()), so the tree can be validated and not
just compiled. A test is taken only when everything it links is already in the
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
#    4. external_regions    -- map every third-party directory to its dep
#                              (region_owner / is_under are its path lookups,
#                              reused by stages 6, 8 and 10)
#    5. transitive_closure  -- walk the target graph from the requested target
#    6. classify            -- split that closure into first-party vs external
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
    reply, cm = load_codemodel(build_dir)
    top_source = Path(cm["paths"]["source"])
    top_build = Path(cm["paths"]["build"])
    by_id, name_to_id, dir_of = load_targets(reply, cm)

    if target not in name_to_id:
        sys.exit(f"error: target '{target}' not found. "
                 f"available: {', '.join(sorted(name_to_id))}")

    fetch = gather_fetchcontent(src_root, deps_files or [])
    regions = external_regions(cm, by_id, dir_of, fetch, top_source, top_build)

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

    # Test targets contribute compile settings, sources and depfiles too.
    contributing = first_party + [by_id[t["id"]] for t in tests]

    # Stage 8: include roots + the C++ standard (from the compile groups).
    inc_roots: set[Path] = set()
    cxx_std = "17"
    for tj in contributing:
        for cg in tj.get("compileGroups", []):
            for inc in cg.get("includes", []):
                root = Path(inc["path"])
                if region_owner(root, regions) is None:
                    inc_roots.add(root)
            std = cg.get("languageStandard", {}).get("standard")
            if std:
                cxx_std = std
    src_inc_roots = [r for r in inc_roots if is_under(r, top_source)]
    gen_inc_roots = [r for r in inc_roots if is_under(r, top_build)]

    # Stage 9: sources (from the codemodel) -- the app's, plus each test's own
    # closure. Because the libraries are flattened away, a test must compile the
    # library sources it used to link -- all already in the app's closure.
    app_sources = collect_sources(first_party, top_source)
    all_sources = set(app_sources)
    for t in tests:
        t["sources"] = collect_sources(t["first_party"], top_source)
        all_sources |= set(t["sources"])

    # Stage 10: headers (from the per-TU depfiles).
    headers: set[Path] = set()
    for tj in contributing:
        # Only per-TU compiler depfiles (`<source>.o.d`). CMake >= 4.0 also
        # writes a link-step depfile `link.d` in the same `.dir/`, listing
        # object files, static archives and system libraries -- never headers.
        for dep in build_dir.glob(f"**/CMakeFiles/{tj['name']}.dir/**/*.o.d"):
            for pre in parse_depfile(dep):
                pre = pre.resolve()
                if pre.suffix in SOURCE_EXTS:
                    continue
                if region_owner(pre, regions) is not None:
                    continue  # third-party: returns via FetchContent, not a copy
                if is_under(pre, top_source) or is_under(pre, top_build):
                    headers.add(pre)

    # --- Stage 11: lay out the flat extracted tree ---
    # src/<origin>/<basename> flattens directories away, so two sources of one
    # target that share a basename would silently overwrite. Catch that before
    # touching the filesystem; --allow-collisions downgrades it to a warning
    # (last writer wins).
    clashes: dict[str, list[Path]] = {}
    for origin, p in all_sources:
        clashes.setdefault(f"src/{origin}/{p.name}", []).append(p)
    clashes = {d: ps for d, ps in sorted(clashes.items()) if len(ps) > 1}
    for d, ps in clashes.items():
        print(f"{'warning' if allow_collisions else 'error'}: "
              f"{len(ps)} sources flatten onto {d}:", file=sys.stderr)
        for p in sorted(ps):
            print(f"    {p}", file=sys.stderr)
    if clashes and not allow_collisions:
        sys.exit("error: rename the colliding files, or pass --allow-collisions "
                 "to keep only the last one")

    out = out_root / target
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)

    rel: dict[tuple[str, Path], str] = {}
    for origin, p in sorted(all_sources):
        dest = out / "src" / origin / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        rel[(origin, p)] = str(dest.relative_to(out))

    cmake_sources = [rel[s] for s in app_sources]
    for t in tests:
        t["cmake_sources"] = [rel[s] for s in t["sources"]]

    used_include, used_generated = False, False
    for h in sorted(headers):
        # Prefer the build-tree (generated) root: with an in-source build dir it
        # is nested under the source root, so generated headers would otherwise
        # be misfiled as ordinary includes.
        root = longest_root(h, gen_inc_roots)
        if root is not None:
            dest = out / "generated" / h.relative_to(root)
            used_generated = True
        else:
            root = longest_root(h, src_inc_roots)
            if root is None:
                print(f"warning: no include root for {h}, using basename",
                      file=sys.stderr)
                dest = out / "include" / h.name
            else:
                dest = out / "include" / h.relative_to(root)
            used_include = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(h, dest)

    # A test may pull in an external the app itself does not use.
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
    """Ensure a codemodel reply exists and return (reply_dir, codemodel_json)."""
    api = build_dir / ".cmake" / "api" / "v1"
    query = api / "query"
    query.mkdir(parents=True, exist_ok=True)
    (query / "codemodel-v2").touch()

    # Reconfigure using the existing cache so the reply is (re)generated.
    subprocess.run(["cmake", str(build_dir)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    reply = api / "reply"
    index = sorted(reply.glob("index-*.json"))[-1]
    idx = json.loads(index.read_text())
    cm_ref = next(o for o in idx["objects"] if o["kind"] == "codemodel")
    cm = json.loads((reply / cm_ref["jsonFile"]).read_text())
    return reply, cm


# Stage 2: index every target's JSON by id, by name, and by owning directory.
def load_targets(reply: Path, cm: dict) -> tuple[dict, dict, dict]:
    """Return (id -> target_json, name -> id, id -> directory index)."""
    config = cm["configurations"][0]
    by_id, name_to_id, dir_of = {}, {}, {}
    for t in config["targets"]:
        tj = json.loads((reply / t["jsonFile"]).read_text())
        by_id[t["id"]] = tj
        name_to_id[tj["name"]] = t["id"]
        dir_of[t["id"]] = t["directoryIndex"]
    return by_id, name_to_id, dir_of


# Stage 3: lift the FetchContent_Declare blocks out of the CMake text.
def parse_fetchcontent(cml_text: str) -> dict:
    """Map declared name -> {'block': original text, 'link': alias to link}."""
    decls = {}
    for m in re.finditer(r"FetchContent_Declare\s*\(\s*(\w+)(.*?)\n\)",
                          cml_text, re.S):
        name = m.group(1)
        decls[name] = {"block": m.group(0), "link": f"{name}::{name}"}
    return decls


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
    if "$" in arg:
        return None
    candidates = [from_file.parent / arg, src_root / arg,
                  src_root / "cmake" / arg]
    if not arg.endswith(".cmake"):
        candidates += [from_file.parent / f"{arg}.cmake",
                       src_root / f"{arg}.cmake",
                       src_root / "cmake" / f"{arg}.cmake"]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


# Stage 3 (gather): collect the CMake text a FetchContent_Declare could live in.
def gather_fetchcontent(src_root: Path, extra_globs: list[str]) -> dict:
    """Scan the top CMakeLists.txt, every file it transitively `include()`s, and
    any `--deps-file` globs, then hand the concatenated text to
    parse_fetchcontent(). Over-collecting is harmless: a declaration whose name
    never lands in the closure is simply never emitted.
    """
    texts: list[str] = []
    seen: set[Path] = set()

    def walk(f: Path) -> None:
        f = f.resolve()
        if f in seen or not f.is_file():
            return
        seen.add(f)
        text = f.read_text()
        texts.append(text)
        for m in _INCLUDE_RE.finditer(text):
            nxt = _resolve_include(m.group(1), f, src_root)
            if nxt is not None:
                walk(nxt)

    walk(src_root / "CMakeLists.txt")
    for pat in extra_globs:
        for f in sorted(src_root.glob(pat)):
            walk(f)
    return parse_fetchcontent("\n".join(texts))


# Stage 4: map every third-party source/build directory to the dep that owns it.
def external_regions(cm: dict, by_id: dict, dir_of: dict, fetch: dict,
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
    dirs = cm["configurations"][0]["directories"]
    owner: dict[int, str] = {}

    deps_base = (top_build / "_deps").resolve()
    declared = {n.lower(): n for n in fetch}
    for i, d in enumerate(dirs):
        src = (top_source / d["source"]).resolve()
        if src.parent == deps_base and src.name.endswith("-src"):
            name = declared.get(src.name[:-len("-src")].lower())
            if name:
                owner[i] = name

    # A top-level directory is the project's own by definition; never let a
    # name collision there mark the whole tree third-party.
    top_level = {i for i, d in enumerate(dirs) if "parentIndex" not in d}
    for tid, tj in by_id.items():
        if tj["name"] in fetch and dir_of[tid] not in top_level:
            owner.setdefault(dir_of[tid], tj["name"])

    stack = list(owner)
    while stack:
        i = stack.pop()
        for child in dirs[i].get("childIndexes", []):
            if child not in owner:
                owner[child] = owner[i]
                stack.append(child)

    regions: dict[Path, str] = {}
    for i, name in owner.items():
        regions[(top_source / dirs[i]["source"]).resolve()] = name
        regions[(top_build / dirs[i]["build"]).resolve()] = name
    return regions


# Stage 5: transitively walk the target graph from the requested target.
def transitive_closure(root_id: str, by_id: dict) -> set[str]:
    seen, stack = set(), [root_id]
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        for dep in by_id[i].get("dependencies", []):
            stack.append(dep["id"])
    return seen


# Stage 6: split a target-id closure into (first-party target JSONs, dep names).
def classify(tids, by_id: dict, regions: dict, fetch: dict,
             top_source: Path) -> tuple[list, list]:
    """Split target ids into (first-party target JSONs, external dep names)."""
    first_party, externals = [], []
    for tid in sorted(tids):
        tj = by_id[tid]
        # A target is third-party if it lives in a dependency's directory; the
        # name check still covers a dep declared but not yet populated.
        owner = region_owner((top_source / tj["paths"]["source"]).resolve(),
                             regions)
        if owner is None and tj["name"] in fetch:
            owner = tj["name"]
        if owner is not None:
            externals.append(owner)
        else:
            first_party.append(tj)
    return first_party, externals


# Helper for stages 4, 6, 8 and 10: name of the dependency owning `path`, else None.
def region_owner(path: Path, regions: dict[Path, str]) -> str | None:
    """Name of the dependency owning `path`, or None if it is not third-party."""
    best_root, best_name = None, None
    for root, name in regions.items():
        if is_under(path, root) and (best_root is None
                                     or len(str(root)) > len(str(best_root))):
            best_root, best_name = root, name
    return best_name


# Low-level path test used by region_owner and throughout extract().
def is_under(path: Path, root: Path) -> bool:
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

    by_artifact = {}
    for tid, tj in by_id.items():
        for art in tj.get("artifacts", []):
            by_artifact[(top_build / art["path"]).resolve()] = tid

    registry = {}
    for t in json.loads(out).get("tests", []):
        cmd = t.get("command") or []
        if not cmd:
            continue
        tid = by_artifact.get(Path(cmd[0]).resolve())
        if tid:  # skip tests that run a script or an external program
            registry[t["name"]] = tid
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
    have = {tj["name"] for tj in first_party}
    selected, skipped = [], []
    for name, tid in sorted(registry.items()):
        t_first, t_ext = classify(transitive_closure(tid, by_id), by_id,
                                  regions, fetch, top_source)
        needs = {tj["name"] for tj in t_first} - {by_id[tid]["name"]}
        if not needs:
            continue  # self-contained: exercises none of the closure's code
        if needs <= have:
            selected.append({"name": name, "target": by_id[tid]["name"],
                             "id": tid, "first_party": t_first,
                             "externals": sorted(set(t_ext))})
        else:
            skipped.append((name, sorted(needs - have)))
    return selected, skipped


# Stage 9: list the source files actually compiled for the given targets.
def collect_sources(targets: list, top_source: Path) -> list:
    """(origin target name, absolute path) for every source actually compiled."""
    found = []
    for tj in targets:
        for s in tj.get("sources", []):
            if s.get("compileGroupIndex") is None:
                continue  # not actually compiled (e.g. header listed as source)
            p = (top_source / s["path"]).resolve()
            if p.suffix in SOURCE_EXTS:
                found.append((tj["name"], p))
    return sorted(set(found))


# Stage 10: read one GCC/Clang .d depfile into the set of paths it lists.
def parse_depfile(path: Path) -> set[Path]:
    text = path.read_text().replace("\\\n", " ")
    if ":" in text:
        text = text.split(":", 1)[1]
    return {Path(tok) for tok in text.split() if tok and tok != "\\"}


# Helper for stage 11: pick the most specific include root a header sits under.
def longest_root(path: Path, roots: list[Path]) -> Path | None:
    best = None
    for r in roots:
        try:
            path.relative_to(r)
        except ValueError:
            continue
        if best is None or len(str(r)) > len(str(best)):
            best = r
    return best


# Stage 12: emit the standalone CMakeLists.txt for the extracted tree.
def write_cmakelists(out: Path, target: str, cxx_std: str,
                     sources: list[str], used_include: bool,
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

    incs = []
    if used_include:
        incs.append("include")
    if used_generated:
        incs.append("generated")

    def emit_executable(name: str, srcs: list[str], links: list[str]) -> None:
        lines.append(f"add_executable({name}")
        for s in srcs:
            lines.append(f"  {s}")
        lines.append(")")
        if incs:
            lines.append(f"target_include_directories({name} PRIVATE "
                         f"{' '.join(incs)})")
        if links:
            aliases = " ".join(fetch[e]["link"] for e in links)
            lines.append(f"target_link_libraries({name} PRIVATE {aliases})")

    emit_executable(target, sources, externals)

    if tests:
        lines.append("")
        lines.append("enable_testing()")
        for t in tests:
            lines.append("")
            emit_executable(t["target"], t["cmake_sources"], t["externals"])
            lines.append(f"add_test(NAME {t['name']} COMMAND {t['target']})")

    lines.append("")
    (out / "CMakeLists.txt").write_text("\n".join(lines))


# Stage 13: emit the README.md for the extracted tree.
def write_readme(out: Path, target: str, first_party: list[dict],
                 externals: list[str], tests: list[dict]) -> None:
    fp = ", ".join(sorted(t["name"] for t in first_party))
    ext = ", ".join(externals) or "(none)"
    body = (
        f"# {target} (extracted standalone closure)\n\n"
        f"Minimal build closure for `{target}`, extracted from the parent "
        f"CMake project into a flat, standalone tree.\n\n"
        f"- First-party targets folded in: {fp}\n"
        f"- Third-party deps (via FetchContent): {ext}\n")
    if tests:
        body += f"- Tests carried over: {', '.join(t['name'] for t in tests)}\n"
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
