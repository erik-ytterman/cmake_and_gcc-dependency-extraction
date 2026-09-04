#!/usr/bin/env python3
"""Extract the minimal build closure of a single CMake application target into a
standalone, buildable directory.

Inputs (all derived from an already-configured build dir):
  * CMake File API codemodel  -> authoritative target graph
  * .d files, one per TU       -> precise header closure actually #included
    (TU = translation unit: one .cpp plus every header it includes)
  * CMake command trace        -> every FetchContent_Declare, find_package and
    (cmake --trace, json-v1)      target_link_libraries the project's own CMake
                                  runs, with arguments already variable-expanded
  * ctest --show-only          -> the registered tests (only with --with-tests)

Output (for target T, under --out, default `extracted/`):
  extracted/<T>/
    CMakeLists.txt   standalone; third-party dependencies re-declared, not copied
    src/<origin>/..  first-party sources (the application plus the libraries it
                     links) and private headers, each under a `src/<origin>/`
                     namespace and keeping its sub-directory structure
    include/..       first-party public headers, at their original include path
    generated/..     generated headers, frozen as plain files

Third-party dependencies (e.g. fmt) are NOT copied, they are re-declared:
  * a FetchContent dependency  -> its FetchContent_Declare block, regenerated
                                  from the arguments the trace recorded
  * a find_package() dependency -> the same find_package() call, re-emitted
                                  verbatim for the host toolchain to satisfy
Either way the tree stays standalone yet minimal.

With --with-tests, the CTest tests covering the extracted code are carried over
too (as extra executables plus add_test()), so the tree can be validated and not
just compiled. A test is taken only when every library it links is already in the
closure, so tests never enlarge the tree.

Vocabulary: every term used in this file -- closure, region, origin, include
root, first-party/third-party, place, covering test -- is defined in
GLOSSARY.md at the repo root, which is shared with the documentation.
ALGORITHM.md documents each numbered stage below in full.
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
#    1. load_codemodel      -- (re)configure the build, capturing the File API
#                              reply and the command trace
#    2. load_targets        -- index every target JSON by id / name / directory
#    3. load_trace          -- read the command trace, then:
#       + gather_fetchcontent   regenerate a FetchContent_Declare block per
#       + traced_find_packages  third-party dependency, collect the project's own
#       + traced_link_tokens    find_package() calls, and record the link tokens
#                              of every target
#    4. external_regions    -- map every third-party directory to the dependency
#                              that owns it. region_owner() then queries that map;
#                              it and is_under() are reused by stages 3, 6, 8, 10
#    5. transitive_closure  -- walk the target graph from the requested target to
#                              every target it links, directly or indirectly
#    6. classify            -- split that closure into first-party vs. third-party
#    7. ctest_registry + select_tests -- (--with-tests) pick the covering tests
#    8. (inline)            -- gather include roots + the C++ standard
#    9. collect_sources     -- list the source files actually compiled
#   10. parse_depfile       -- read the per-TU *.o.d depfiles for the header closure
#   11. (inline, longest_root) -- place every file, collision-check, then copy
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
    reply, codemodel, trace_file = load_codemodel(build_dir)
    top_source = Path(codemodel["paths"]["source"])
    top_build = Path(codemodel["paths"]["build"])
    by_id, name_to_id, dir_of = load_targets(reply, codemodel)

    if target not in name_to_id:
        sys.exit(f"error: target '{target}' not found. "
                 f"available: {', '.join(sorted(name_to_id))}")

    trace = load_trace(trace_file)
    # --deps-file: text-scanned declarations, for a dependency declared behind an
    # if() the trace never enters. Needed both before regions (as bare names, for
    # Stage 4) and after (as full blocks), so scan once here.
    text_extra = text_declares(src_root, deps_files or [])
    declared = traced_declare_names(trace, top_source) | set(text_extra)
    regions = external_regions(codemodel, by_id, dir_of, declared,
                               top_source, top_build)

    fetch = gather_fetchcontent(trace, regions, top_source, text_extra)
    find_pkgs = traced_find_packages(trace, regions, top_source)
    link_tokens = traced_link_tokens(trace, regions, top_source, set(name_to_id))

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
    # A public header is placed relative to the include root it sits under, so
    # the existing `#include <a/b.hpp>` lines keep resolving via `-I include`.
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
    # sources it used to link (the libraries are folded in, so there is no
    # library target left to link) -- and those are already in app_sources.
    app_sources = collect_sources(first_party, top_source)
    all_sources = set(app_sources)
    for t in tests:
        t["sources"] = collect_sources(t["first_party"], top_source)
        all_sources |= set(t["sources"])

    # --- Stage 10: the exact header set to copy (from the per-TU depfiles) ---
    # Kept per origin -- the target whose translation units pulled the header --
    # so a private header (one under no include root) can be placed next to that
    # target's sources and stay reachable by a file-relative
    # `#include "sibling.hpp"`.
    headers_by_origin: dict[str, set[Path]] = {}
    for target_json in contributing:
        # Match `<source>.o.d` only. CMake >= 4.0 also writes a link-step depfile
        # `link.d` into the same `.dir/`; it lists object files, static archives
        # and system libraries -- never headers -- so a plain `*.d` glob is wrong.
        pattern = f"**/CMakeFiles/{target_json['name']}.dir/**/*.o.d"
        bucket = headers_by_origin.setdefault(target_json["name"], set())
        for depfile in build_dir.glob(pattern):
            for prereq in parse_depfile(depfile):
                prereq = prereq.resolve()
                if prereq.suffix in SOURCE_EXTS:
                    continue  # a source, not a header
                if region_owner(prereq, regions) is not None:
                    continue  # third-party: comes back via FetchContent, not a copy
                if is_under(prereq, top_source) or is_under(prereq, top_build):
                    bucket.add(prereq)  # first-party or generated -- keep it
    header_count = len({h for hs in headers_by_origin.values() for h in hs})

    # --- Stage 11: place every file, collision-check, then copy ---
    # The original directory structure is preserved, not flattened. A source or
    # private header keeps its path relative to its origin target's directory,
    # under the one-level `src/<origin>/` namespace; a public header keeps its
    # path relative to the include root that exposed it, under `include/`;
    # generated files go under `generated/`. This keeps every `#include`
    # resolving -- angle-bracket via `-I include` / `-I generated`, file-relative
    # quotes because siblings stay siblings -- and makes collisions rare.
    origin_dir = {
        tj["name"]: (top_source / tj["paths"]["source"]).resolve()
        for tj in first_party + [x for t in tests for x in t["first_party"]]}
    deeper_first = sorted(origin_dir.items(),
                          key=lambda nd: (len(str(nd[1])), nd[0]), reverse=True)

    def place_source(path: Path, origin: str) -> Path:
        """The source's path in the extracted tree, relative to `out`."""
        if is_under(path, top_build):  # a generated source
            root = longest_root(path, gen_inc_roots)
            return Path("generated") / path.relative_to(root or top_build)
        base = origin_dir.get(origin)
        if base is not None and is_under(path, base):
            return Path("src") / origin / path.relative_to(base)
        for name, d in deeper_first:  # source listed from outside its own dir
            if is_under(path, d):
                return Path("src") / name / path.relative_to(d)
        print(f"warning: source {path} is outside every target directory; "
              f"placing it at src/{origin}/{path.name}", file=sys.stderr)
        return Path("src") / origin / path.name

    def place_header(path: Path, origin: str) -> Path:
        """The header's path in the extracted tree, relative to `out`: `include/`
        for a public header (under an include root), otherwise `src/<origin>/`
        beside the sources that include it."""
        if is_under(path, top_build):  # generated (also catches in-source builds)
            root = longest_root(path, gen_inc_roots)
            return Path("generated") / path.relative_to(root or top_build)
        root = longest_root(path, src_inc_roots)
        if root is not None:  # public header -- keep its include-relative path
            return Path("include") / path.relative_to(root)
        base = origin_dir.get(origin)
        if base is not None and is_under(path, base):
            return Path("src") / origin / path.relative_to(base)
        for name, d in deeper_first:
            if is_under(path, d):
                return Path("src") / name / path.relative_to(d)
        print(f"warning: header {path} has no include root and is outside every "
              f"target directory; placing it at src/{origin}/{path.name}",
              file=sys.stderr)
        return Path("src") / origin / path.name

    # Place every file, then check that no two distinct files want the same path.
    src_dest = {(o, s): place_source(s, o) for o, s in all_sources}
    hdr_dest = {(o, h): place_header(h, o)
                for o, hs in headers_by_origin.items() for h in hs}

    files_at: dict[Path, set[Path]] = {}
    for (_, source_or_header), d in {**src_dest, **hdr_dest}.items():
        files_at.setdefault(d, set()).add(source_or_header)
    collisions = {d: fs for d, fs in sorted(files_at.items()) if len(fs) > 1}
    for d, fs in collisions.items():
        label = "warning" if allow_collisions else "error"
        print(f"{label}: {len(fs)} different files collide at {d}:",
              file=sys.stderr)
        for f in sorted(fs):
            print(f"    {f}", file=sys.stderr)
    if collisions and not allow_collisions:
        sys.exit("error: rename the colliding files, or pass --allow-collisions "
                 "to keep only the last one")

    out = out_root / target
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Copy each source to its placed path; remember that path, keyed by
    # (origin, absolute source), so write_cmakelists() can list it.
    rel: dict[tuple[str, Path], str] = {}
    for (origin, source), d in sorted(src_dest.items(), key=lambda kv: str(kv[1])):
        dest = out / d
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        rel[(origin, source)] = str(d)

    cmake_sources = [rel[s] for s in app_sources]
    for t in tests:
        t["cmake_sources"] = [rel[s] for s in t["sources"]]

    # Copy each header to its placed path.
    for (_, header), d in sorted(hdr_dest.items(), key=lambda kv: str(kv[1])):
        dest = out / d
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(header, dest)

    tops = {d.parts[0] for d in list(src_dest.values()) + list(hdr_dest.values())}
    used_include, used_generated = "include" in tops, "generated" in tops

    # Build the link line for one extracted executable: the libraries it must
    # name in target_link_libraries(). The first-party libraries are folded in,
    # so every link edge they carried to a third-party dependency has to be
    # re-homed onto the executable -- walk the executable *and* each first-party
    # library folded into it, and keep every link token whose base name (before
    # `::`) is a FetchContent or a find_package dependency. `fc_from_graph` is the
    # Stage 6 safety net: a FetchContent dependency in the target graph that no
    # link token happened to name (linked through a generator expression, say)
    # still gets its `<name>::<name>` imported-target name.
    # Declaration names and imported-target namespaces differ in case by
    # convention -- FetchContent_Declare(boost ...) yields Boost:: targets -- so
    # every base-name comparison below is case-insensitive, as Stage 4's region
    # matching already is.
    fetch_lc = {name.lower(): name for name in fetch}
    find_pkgs_lc = {name.lower(): name for name in find_pkgs}

    def link_line(owner: str, folded: set[str],
                  fc_from_graph: set[str]) -> list[str]:
        line: list[str] = []
        for src_target in [owner, *sorted(folded)]:
            for token in link_tokens.get(src_target, []):
                base = token.split("::", 1)[0].lower()
                if (base in fetch_lc or base in find_pkgs_lc) and token not in line:
                    line.append(token)
        seen = {token.split("::", 1)[0].lower() for token in line}
        for name in sorted(fc_from_graph):
            if name in fetch and name.lower() not in seen:
                line.append(fetch[name]["link"])
        return line

    link_lines = {target: link_line(
        target, {t["name"] for t in first_party} - {target}, set(externals))}
    for t in tests:
        link_lines[t["target"]] = link_line(
            t["target"], {tj["name"] for tj in t["first_party"]} - {t["target"]},
            set(t["externals"]))

    # FetchContent blocks to regenerate: every dependency reachable in the target
    # graph from the app or a carried-over test, plus any named on a link line.
    # find_package calls to re-emit: every package named on a link line.
    graph_fc = (set(externals).union(*(t["externals"] for t in tests))
                if tests else set(externals))
    # Map each link token's base back to the declaration name it belongs to,
    # case-insensitively (Boost::algorithm -> the `boost` declaration).
    line_bases = {token.split("::", 1)[0].lower()
                  for line in link_lines.values() for token in line}
    ext_names = sorted(graph_fc | {fetch_lc[b] for b in line_bases if b in fetch_lc})
    fp_names = sorted(find_pkgs_lc[b] for b in line_bases if b in find_pkgs_lc)
    find_package_blocks = [find_pkgs[name] for name in fp_names]

    write_cmakelists(out, target, cxx_std, cmake_sources, used_include,
                     used_generated, fetch, ext_names, find_package_blocks,
                     link_lines, tests)
    write_readme(out, target, first_party, ext_names, fp_names, tests)

    print(f"Extracted '{target}' -> {out}")
    print(f"  sources      : {len(cmake_sources)}")
    print(f"  headers      : {header_count}")
    print(f"  FetchContent : {', '.join(ext_names) or '(none)'}")
    print(f"  find_package : {', '.join(fp_names) or '(none)'}")
    if with_tests:
        print(f"  tests        : {', '.join(t['name'] for t in tests) or '(none)'}")

    if verify:
        verify_build(out, target, bool(tests))


# Stage 1: (re)configure the build dir and read the CMake File API codemodel.
def load_codemodel(build_dir: Path) -> tuple[Path, dict, Path]:
    """Return (reply_dir, codemodel, trace_file) for an already-configured build.

    The File API is request/reply: you leave an empty query file named for what
    you want, re-run CMake, and it writes JSON into a reply directory. Filenames
    there are content-hashed, so the only stable entry point is `index-*.json`.

    The same reconfigure is run under `--trace-expand --trace-format=json-v1`,
    redirected to a file -- the command trace: one trace record per command
    invocation, arguments already variable-expanded. Stage 3 reads it back to
    recover the third-party dependencies. `--trace-redirect` needs CMake >= 3.21.
    """
    api = build_dir / ".cmake" / "api" / "v1"
    query = api / "query"
    query.mkdir(parents=True, exist_ok=True)
    (query / "codemodel-v2").touch()  # the name *is* the request

    # A no-op reconfigure against the existing cache; it refreshes the reply and
    # re-runs every CMakeLists, so the trace is complete each time.
    trace_file = build_dir / ".cmake" / "extract-trace.json"
    subprocess.run(["cmake", str(build_dir), "--trace-expand",
                    "--trace-format=json-v1", f"--trace-redirect={trace_file}"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    reply = api / "reply"
    newest_index = sorted(reply.glob("index-*.json"))[-1]
    index = json.loads(newest_index.read_text())
    codemodel_ref = next(o for o in index["objects"] if o["kind"] == "codemodel")
    codemodel = json.loads((reply / codemodel_ref["jsonFile"]).read_text())
    return reply, codemodel, trace_file


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


# Stage 3: recover the third-party dependencies from the command trace.
#
# `cmake --trace-expand --trace-format=json-v1` writes one trace record per
# command invocation -- `{"cmd": ..., "args": [...], "file": ..., "line": ...}`
# -- with every argument already variable-expanded and every if()/foreach()/
# function() resolved. That is a far more reliable source than grepping
# CMakeLists.txt: it follows `FetchContent_Declare(${name} ...)`, a declaration
# wrapped in a function or a loop, and the FetchContent_Declare calls CPM and
# similar wrappers synthesise internally -- none of which a text scan can see.
#
# A record is kept only when it comes from the project's own CMake code: its
# `file` is under the source root and outside every third-party region. Records
# from a dependency's own CMake, or from CMake's bundled modules, are dropped.

_FC_KEYWORD_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")
_CMAKE_BOOLS = {"TRUE", "FALSE", "YES", "NO", "ON", "OFF"}
_TLL_KEYWORDS = {"PRIVATE", "PUBLIC", "INTERFACE", "LINK_PRIVATE", "LINK_PUBLIC",
                 "LINK_INTERFACE_LIBRARIES", "debug", "optimized", "general"}


def parse_fetchcontent(cmake_text: str) -> dict:
    """Text fallback for --deps-file: copy verbatim FetchContent_Declare(...)
    blocks out of raw CMake, mapping each name to {block, link}. The trace is
    the primary source; this only covers a declaration the trace never reaches.
    """
    declarations = {}
    for match in re.finditer(r"FetchContent_Declare\s*\(\s*(\w+)(.*?)\n\)",
                             cmake_text, re.S):
        name = match.group(1)
        declarations[name] = {"block": match.group(0), "link": f"{name}::{name}"}
    return declarations


def text_declares(src_root: Path, globs: list[str]) -> dict:
    """Run parse_fetchcontent() over every file matched by the --deps-file
    globs (relative to the source root)."""
    found = {}
    for pattern in globs:
        for cmake_file in sorted(src_root.glob(pattern)):
            found.update(parse_fetchcontent(cmake_file.read_text()))
    return found


def load_trace(trace_file: Path) -> list[dict]:
    """Parse the command trace, keeping only the trace records Stage 3 needs.
    The first line is a `{"version": ...}` header with no `cmd` and is dropped;
    command names are matched case-insensitively (CMake commands are)."""
    wanted = {"fetchcontent_declare", "find_package", "target_link_libraries"}
    records = []
    for line in trace_file.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        cmd = record.get("cmd", "")
        if cmd.lower() in wanted:
            record["cmd"] = cmd.lower()
            records.append(record)
    return records


def _from_project_cmake(record: dict, top_source: Path,
                        regions: dict | None) -> bool:
    """True when a trace record was authored in the project's own CMake code: its
    `file` is under the source root and, once regions are known, outside every
    third-party region."""
    where = Path(record["file"])
    if not is_under(where, top_source):
        return False
    return regions is None or region_owner(where.resolve(), regions) is None


def traced_declare_names(trace: list[dict], top_source: Path) -> set[str]:
    """Just the names of the project's own FetchContent_Declare calls. Stage 4
    needs the declared names before regions can be computed, so this runs without
    the region filter; over-collection is harmless (Stage 4 only matches the
    names against real directories and targets)."""
    return {r["args"][0] for r in trace
            if r["cmd"] == "fetchcontent_declare" and r["args"]
            and _from_project_cmake(r, top_source, None)}


def _quote(token: str) -> str:
    return f'"{token}"' if token == "" or any(c.isspace() for c in token) else token


def _render_declare(args: list[str]) -> str:
    """Regenerate a FetchContent_Declare() block from the argument list the trace
    recorded, one `KEYWORD value...` group per line. The original formatting and
    comments are lost; the arguments are exactly what CMake received."""
    lines = ["FetchContent_Declare(", f"  {args[0]}"]
    group: list[str] = []
    for token in args[1:]:
        # a keyword like GIT_REPOSITORY -- but not an all-caps value (GIT_SHALLOW
        # TRUE) or a cache type in a -D flag
        if _FC_KEYWORD_RE.fullmatch(token) and token not in _CMAKE_BOOLS:
            if group:
                lines.append("  " + " ".join(_quote(t) for t in group))
            group = [token]
        else:
            group.append(token)
    if group:
        lines.append("  " + " ".join(_quote(t) for t in group))
    lines.append(")")
    return "\n".join(lines)


# Stage 3 (FetchContent): regenerate a FetchContent_Declare block per dependency.
def gather_fetchcontent(trace: list[dict], regions: dict, top_source: Path,
                        text_extra: dict) -> dict:
    """Map each FetchContent dependency the project declares to:

      block : its FetchContent_Declare(...) block, regenerated from the traced
              arguments (see _render_declare)
      link  : the conventional `<name>::<name>` imported-target name, used only
              as a link-line fallback -- the real link line comes from the
              traced target_link_libraries

    Declarations found only by the --deps-file text scan (text_extra) are merged
    in for the case the trace never reached them.
    """
    declarations: dict = {}
    for record in trace:
        if record["cmd"] != "fetchcontent_declare" or not record["args"]:
            continue
        if not _from_project_cmake(record, top_source, regions):
            continue
        name = record["args"][0]
        declarations.setdefault(name, {"block": _render_declare(record["args"]),
                                       "link": f"{name}::{name}"})
    for name, decl in text_extra.items():
        declarations.setdefault(name, decl)
    return declarations


# Stage 3 (find_package): re-emit the project's find_package() calls verbatim.
def traced_find_packages(trace: list[dict], regions: dict,
                         top_source: Path) -> dict:
    """Map each package the project looks up with find_package() to a verbatim
    `find_package(...)` call. The extracted tree cannot recreate these -- the
    host toolchain must provide them -- but re-emitting the call keeps the build
    honest about what it needs. Only calls from the project's own CMake code are
    kept, so find_package(Git) from inside FetchContent.cmake is dropped."""
    packages: dict = {}
    for record in trace:
        if record["cmd"] != "find_package" or not record["args"]:
            continue
        if not _from_project_cmake(record, top_source, regions):
            continue
        name = record["args"][0]
        packages.setdefault(
            name, "find_package(" + " ".join(_quote(a) for a in record["args"])
            + ")")
    return packages


# Stage 3 (link tokens): what each target names in target_link_libraries().
def traced_link_tokens(trace: list[dict], regions: dict, top_source: Path,
                       targets: set[str]) -> dict[str, list[str]]:
    """target name -> its link tokens: the libraries it names in
    target_link_libraries(), in order, with the PRIVATE / PUBLIC keywords
    removed. Calls on a name that is not a known project target are ignored (this
    drops try_compile's scratch targets). Accumulates across multiple calls on
    the same target."""
    tokens: dict[str, list[str]] = {}
    for record in trace:
        if record["cmd"] != "target_link_libraries" or not record["args"]:
            continue
        if not _from_project_cmake(record, top_source, regions):
            continue
        name = record["args"][0]
        if name not in targets:
            continue
        for token in record["args"][1:]:
            if token and token not in _TLL_KEYWORDS:
                tokens.setdefault(name, []).append(token)
    return tokens


# Stage 4: map every third-party source/build directory to the dependency
# that owns it.
def external_regions(codemodel: dict, by_id: dict, dir_of: dict,
                     declared: set[str], top_source: Path,
                     top_build: Path) -> dict[Path, str]:
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
    declared_lc = {name.lower(): name for name in declared}
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
        if target_json["name"] in declared and directory_idx not in top_level:
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
    pair, where `origin` is the name of the target it belongs to (Stage 11 uses
    it as the `src/<origin>/` namespace).
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
    placed relative to that dir, not to some shorter root it also sits under.
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
                     find_package_blocks: list[str],
                     link_lines: dict[str, list[str]],
                     tests: list[dict]) -> None:
    lines = [
        "cmake_minimum_required(VERSION 3.20)",
        f"project({target}_standalone LANGUAGES CXX)",
        "",
        f"set(CMAKE_CXX_STANDARD {cxx_std})",
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
        "",
    ]
    if find_package_blocks:
        lines.append("# supplied by the host toolchain, not built by this tree")
        lines.extend(find_package_blocks)
        lines.append("")
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

    def emit_executable(name: str, sources: list[str]) -> None:
        """Append one add_executable() + its target_* calls. Its link line is
        link_lines[name]: the third-party tokens the source project's
        target_link_libraries named, re-homed onto this executable."""
        lines.append(f"add_executable({name}")
        for source in sources:
            lines.append(f"  {source}")
        lines.append(")")
        if include_dirs:
            lines.append(f"target_include_directories({name} PRIVATE "
                         f"{' '.join(include_dirs)})")
        links = link_lines.get(name, [])
        if links:
            lines.append(f"target_link_libraries({name} PRIVATE "
                         f"{' '.join(links)})")

    emit_executable(target, cmake_sources)

    if tests:
        lines.append("")
        lines.append("enable_testing()")
        for test in tests:
            lines.append("")
            emit_executable(test["target"], test["cmake_sources"])
            lines.append(f"add_test(NAME {test['name']} COMMAND {test['target']})")

    lines.append("")
    (out / "CMakeLists.txt").write_text("\n".join(lines))


# Stage 13: emit the README.md for the extracted tree.
def write_readme(out: Path, target: str, first_party: list[dict],
                 externals: list[str], find_packages: list[str],
                 tests: list[dict]) -> None:
    first_party_names = ", ".join(sorted(t["name"] for t in first_party))
    fetchcontent_names = ", ".join(externals) or "(none)"
    carried_test_names = ", ".join(t["name"] for t in tests)
    body = (
        f"# {target} (extracted standalone closure)\n\n"
        f"Minimal build closure for `{target}`, extracted from its source "
        f"CMake project into a standalone tree.\n\n"
        f"- First-party targets folded in: {first_party_names}\n"
        f"- Third-party dependencies (via FetchContent): {fetchcontent_names}\n")
    if find_packages:
        body += ("- Provided by the host toolchain (find_package): "
                 f"{', '.join(find_packages)}\n")
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
                    help="extra CMake file(s) to text-scan for FetchContent_Declare "
                         "(glob relative to --src; repeatable). Rarely needed: the "
                         "command trace already captures every declaration that "
                         "runs at configure time; use this only for one guarded "
                         "behind an if() the trace never enters")
    ap.add_argument("--allow-collisions", action="store_true",
                    help="proceed when two different files map to the same path "
                         "in the extracted tree (last one wins) instead of "
                         "aborting. Rare now that sub-directory structure is "
                         "kept; mostly two libraries exposing the same "
                         "include-relative header")
    args = ap.parse_args()

    extract(args.target, args.src.resolve(), args.build.resolve(),
            args.out.resolve(), args.verify, args.with_tests,
            args.deps_file, args.allow_collisions)


if __name__ == "__main__":
    main()
