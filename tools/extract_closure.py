#!/usr/bin/env python3
"""Extract the minimal build closure of a single CMake application target into a
flat, standalone, buildable directory.

Inputs (all derived from an already-configured build dir):
  * CMake File API codemodel  -> authoritative target/link graph
  * per-TU .d files            -> precise header closure actually #included
  * top-level CMakeLists.txt   -> FetchContent_Declare blocks for third-party deps

Output (for target T):
  out/<T>/
    CMakeLists.txt   standalone; third-party deps kept via FetchContent
    src/<origin>/..  first-party sources (app + linked libs), flattened
    include/..       first-party headers, at their original include-relative path
    generated/..     generated headers, frozen as plain files

Third-party dependencies (e.g. fmt) are NOT copied; they are re-declared via the
same FetchContent block found in the source project, so the tree stays standalone
yet minimal.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


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


def load_targets(reply: Path, cm: dict) -> tuple[dict, dict]:
    """Return (id -> target_json, name -> id) for the first configuration."""
    config = cm["configurations"][0]
    by_id, name_to_id = {}, {}
    for t in config["targets"]:
        tj = json.loads((reply / t["jsonFile"]).read_text())
        by_id[t["id"]] = tj
        name_to_id[tj["name"]] = t["id"]
    return by_id, name_to_id


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


# --- .d dependency-file parsing -----------------------------------------------

def parse_depfile(path: Path) -> set[Path]:
    text = path.read_text().replace("\\\n", " ")
    if ":" in text:
        text = text.split(":", 1)[1]
    return {Path(tok) for tok in text.split() if tok and tok != "\\"}


# --- helpers ------------------------------------------------------------------

SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx"}


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
            verify: bool) -> None:
    reply, cm = load_codemodel(build_dir)
    top_source = Path(cm["paths"]["source"])
    top_build = Path(cm["paths"]["build"])
    by_id, name_to_id = load_targets(reply, cm)

    if target not in name_to_id:
        sys.exit(f"error: target '{target}' not found. "
                 f"available: {', '.join(sorted(name_to_id))}")

    fetch = parse_fetchcontent((src_root / "CMakeLists.txt").read_text())

    closure = transitive_closure(name_to_id[target], by_id)
    closure_targets = [by_id[i] for i in closure]

    first_party, externals = [], []
    for tj in closure_targets:
        if tj["name"] in fetch:
            externals.append(tj["name"])
        else:
            first_party.append(tj)

    # Include roots (from compile groups of first-party targets).
    inc_roots: set[Path] = set()
    cxx_std = "17"
    for tj in first_party:
        for cg in tj.get("compileGroups", []):
            for inc in cg.get("includes", []):
                inc_roots.add(Path(inc["path"]))
            std = cg.get("languageStandard", {}).get("standard")
            if std:
                cxx_std = std
    src_inc_roots = [r for r in inc_roots if is_under(r, top_source)]
    gen_inc_roots = [r for r in inc_roots if is_under(r, top_build)]

    # Collect first-party sources (from codemodel) and headers (from .d files).
    sources: list[tuple[str, Path]] = []       # (origin target, abs path)
    for tj in first_party:
        for s in tj.get("sources", []):
            if s.get("compileGroupIndex") is None:
                continue  # not actually compiled (e.g. header listed as source)
            p = (top_source / s["path"]).resolve()
            if p.suffix in SOURCE_EXTS:
                sources.append((tj["name"], p))

    headers: set[Path] = set()
    for tj in first_party:
        for dep in build_dir.glob(f"**/CMakeFiles/{tj['name']}.dir/**/*.d"):
            for pre in parse_depfile(dep):
                pre = pre.resolve()
                if pre.suffix in SOURCE_EXTS:
                    continue
                if is_under(pre, top_source) or is_under(pre, top_build):
                    headers.add(pre)

    # --- lay out the flat output tree ---
    out = out_root / target
    if out.exists():
        shutil.rmtree(out)
    (out / "src").mkdir(parents=True)

    cmake_sources: list[str] = []
    for origin, p in sorted(set(sources)):
        dest = out / "src" / origin / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        cmake_sources.append(str(dest.relative_to(out)))

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

    write_cmakelists(out, target, cxx_std, cmake_sources,
                     used_include, used_generated,
                     [fetch[e] for e in sorted(set(externals))])
    write_readme(out, target, first_party, sorted(set(externals)))

    print(f"Extracted '{target}' -> {out}")
    print(f"  sources : {len(cmake_sources)}")
    print(f"  headers : {len(headers)}")
    print(f"  external: {', '.join(sorted(set(externals))) or '(none)'}")

    if verify:
        verify_build(out, target)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def write_cmakelists(out: Path, target: str, cxx_std: str,
                     sources: list[str], used_include: bool,
                     used_generated: bool, externals: list[dict]) -> None:
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
        for ext in externals:
            lines.append(ext["block"])
        names = " ".join(re.match(r"FetchContent_Declare\s*\(\s*(\w+)",
                                  e["block"]).group(1) for e in externals)
        lines.append(f"FetchContent_MakeAvailable({names})")
        lines.append("")

    lines.append(f"add_executable({target}")
    for s in sources:
        lines.append(f"  {s}")
    lines.append(")")

    incs = []
    if used_include:
        incs.append("include")
    if used_generated:
        incs.append("generated")
    if incs:
        lines.append(f"target_include_directories({target} PRIVATE "
                     f"{' '.join(incs)})")

    if externals:
        links = " ".join(e["link"] for e in externals)
        lines.append(f"target_link_libraries({target} PRIVATE {links})")

    lines.append("")
    (out / "CMakeLists.txt").write_text("\n".join(lines))


def write_readme(out: Path, target: str, first_party: list[dict],
                 externals: list[str]) -> None:
    fp = ", ".join(sorted(t["name"] for t in first_party))
    ext = ", ".join(externals) or "(none)"
    (out / "README.md").write_text(
        f"# {target} (extracted standalone closure)\n\n"
        f"Minimal build closure for `{target}`, extracted from the parent "
        f"CMake project into a flat, standalone tree.\n\n"
        f"- First-party targets folded in: {fp}\n"
        f"- Third-party deps (via FetchContent): {ext}\n\n"
        "## Build\n\n"
        "```sh\ncmake -S . -B build\ncmake --build build -j\n```\n")


def verify_build(out: Path, target: str) -> None:
    print(f"\n--- verifying build of extracted '{target}' ---")
    subprocess.run(["cmake", "-S", str(out), "-B", str(out / "build")],
                   check=True)
    subprocess.run(["cmake", "--build", str(out / "build"), "-j"], check=True)
    print(f"--- OK: {out / 'build'} built successfully ---")


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
                    help="configure+build the extracted tree to prove it stands alone")
    args = ap.parse_args()

    extract(args.target, args.src.resolve(), args.build.resolve(),
            args.out.resolve(), args.verify)


if __name__ == "__main__":
    main()
