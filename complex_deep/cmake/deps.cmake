# Dependency declarations, pulled in by the top CMakeLists via include().
# Stage 3 finds these because they *run* at configure time, not because the
# extractor knows to look here.

include(FetchContent)

FetchContent_Declare(
  fmt
  GIT_REPOSITORY https://github.com/fmtlib/fmt.git
  GIT_TAG        10.2.1
  GIT_SHALLOW    TRUE
)

# Wrapped in a function: the declaration only exists once declare_json() runs.
function(declare_json)
  FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
  )
endfunction()
