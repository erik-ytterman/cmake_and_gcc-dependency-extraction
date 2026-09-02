# samples/

The CMake projects the extractor operates on. Each is **self-contained** — its
own top-level `project()`, its own build directory, its own dependencies — so it
can be developed, built, and (later) split into its own repository independently
of the tool.

| Project | Role |
|---|---|
| [`basic/`](basic/) | The teaching fixture. Four apps, two libraries, one FetchContent dependency (`fmt`). Every [TUTORIAL.md](../TUTORIAL.md) lab (§2–§9, §13) runs here; `tools/test_tutorial.sh` drives it. |
| [`complex_deep/`](complex_deep/) | The porting fixture for [TUTORIAL.md §11](../TUTORIAL.md). A three-level library tree, `fmt` + `nlohmann_json` + `find_package(Threads)`, an `OBJECT` library, and a target with two same-basename sources. `complex_deep/extract_all.sh` extracts every app in it. |

Point the extractor at one with `--src` / `--build`:

```sh
python3 tools/extract_closure.py <app> \
    --src samples/<project> --build samples/<project>/build [--with-tests] [--verify]
```
