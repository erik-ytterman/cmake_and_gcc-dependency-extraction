# Dependency declarations, pulled in by the top CMakeLists via include().
# Stage 3 finds these because they *run* at configure time, not because the
# extractor knows to look here.

include(FetchContent)

# Boost, whole. Deliberately *not* narrowed with BOOST_INCLUDE_LIBRARIES: that
# is a plain variable rather than part of the declaration, so it would not
# survive extraction and the two builds would not be comparable. See
# SUMMARY.md on what does and does not travel.
FetchContent_Declare(
  boost
  URL https://github.com/boostorg/boost/releases/download/boost-1.87.0/boost-1.87.0-cmake.tar.gz
  URL_HASH SHA256=78fbf579e3caf0f47517d3fb4d9301852c3154bfecdc5eeebd9b2b0292366f5b
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE
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
