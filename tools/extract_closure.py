#!/usr/bin/env python3
"""Extract the minimal build closure of a single CMake application target into a
flat, standalone, buildable directory.

Inputs (all derived from an already-configured build dir):
  * CMake File API codemodel  -> authoritative target/link graph
  * per-TU .d files            -> precise header closure actually #included
  * top-level CMakeLists.txt   -> FetchContent_Declare blocks for third-party deps
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


# --- CMake File API -----------------------------------------------------------

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


# --- Third-party (FetchContent) parsing ---------------------------------------

def parse_fetchcontent(cml_text: str) -> dict:
    """Map declared name -> {'block': original text, 'link': alias to link}."""
    decls = {}
    for m in re.finditer(r"FetchContent_Declare\s*\(\s*(\w+)(.*?)\n\)",
                          cml_text, re.S):
        name = m.group(1)
        decls[name] = {"block": m.group(0), "link": f"{name}::{name}"}
    return decls


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


def region_owner(path: Path, regions: dict[Path, str]) -> str | None:
    """Name of the dependency owning `path`, or None if it is not third-party."""
    best_root, best_name = None, None
    for root, name in regions.items():
        if is_under(path, root) and (best_root is None
                                     or len(str(root)) > len(str(best_root))):
            best_root, best_name = root, name
    return best_name


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


# --- test discovery (--with-tests) --------------------------------------------

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


# --- .d dependency-file parsing -----------------------------------------------

def parse_depfile(path: Path) -> set[Path]:
    text = path.read_text().replace("\\\n", " ")
    if ":" in text:
        text = text.split(":", 1)[1]
    return {Path(tok) for tok in text.split() if tok and tok != "\\"}


# --- helpers ------------------------------------------------------------------

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


# --- main extraction ----------------------------------------------------------

def extract(target: str, src_root: Path, build_dir: Path, out_root: Path,
            verify: bool, with_tests: bool) -> None:
    reply, cm = load_codemodel(build_dir)
    top_source = Path(cm["paths"]["source"])
    top_build = Path(cm["paths"]["build"])
    by_id, name_to_id, dir_of = load_targets(reply, cm)

    if target not in name_to_id:
        sys.exit(f"error: target '{target}' not found. "
                 f"available: {', '.join(sorted(name_to_id))}")

    fetch = parse_fetchcontent((src_root / "CMakeLists.txt").read_text())
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

    # Test targets contribute compile settings, sources and .d files too.
    contributing = first_party + [by_id[t["id"]] for t in tests]

    # Include roots (from compile groups).
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

    # Sources (from the codemodel): the app's, plus each test's own closure.
    # Because the libraries are flattened away, a test must compile the library
    # sources it used to link -- all of which the app closure already provides.
    app_sources = collect_sources(first_party, top_source)
    all_sources = set(app_sources)
    for t in tests:
        t["sources"] = collect_sources(t["first_party"], top_source)
        all_sources |= set(t["sources"])

    # Headers (from .d files).
    headers: set[Path] = set()
    for tj in contributing:
        for dep in build_dir.glob(f"**/CMakeFiles/{tj['name']}.dir/**/*.d"):
            for pre in parse_depfile(dep):
                pre = pre.resolve()
                if pre.suffix in SOURCE_EXTS:
                    continue
                if region_owner(pre, regions) is not None:
                    continue  # third-party: returns via FetchContent, not a copy
                if is_under(pre, top_source) or is_under(pre, top_build):
                    headers.add(pre)

    # --- lay out the flat output tree ---
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


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


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
    args = ap.parse_args()

    extract(args.target, args.src.resolve(), args.build.resolve(),
            args.out.resolve(), args.verify, args.with_tests)


if __name__ == "__main__":
    main()
