cmake_minimum_required(VERSION 3.20)
project(guessing_poc VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Enables testing and defines BUILD_TESTING (default ON).
include(CTest)

# --- third-party dependency: fmt (terminal formatting) via FetchContent ---
include(FetchContent)
FetchContent_Declare(
  fmt
  GIT_REPOSITORY https://github.com/fmtlib/fmt.git
  GIT_TAG        10.2.1
  GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(fmt)

# --- generated code: build_info.hpp produced at configure time ---
configure_file(
  ${CMAKE_CURRENT_SOURCE_DIR}/cmake/build_info.hpp.in
  ${CMAKE_CURRENT_BINARY_DIR}/generated/build_info.hpp
  @ONLY)
add_library(build_info INTERFACE)
target_include_directories(build_info INTERFACE ${CMAKE_CURRENT_BINARY_DIR}/generated)

# --- first-party libraries (shared pool) ---
add_subdirectory(libs/rng)
add_subdirectory(libs/input)

# --- applications, each with a different dependency subset ---
add_subdirectory(apps/guess)
add_subdirectory(apps/roller)
add_subdirectory(apps/greeter)
add_subdirectory(apps/tally)
