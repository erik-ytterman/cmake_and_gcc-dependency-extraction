cmake_minimum_required(VERSION 3.21)
project(complex_deep VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(CTest)

# --- third-party dependencies -------------------------------------------------
# fmt is declared in an include()d file; nlohmann_json inside a function. Both
# are invisible to a text scan of this file -- the point of driving Stage 3 off
# the command trace.
include(cmake/deps.cmake)
declare_json()                          # defines the nlohmann_json declaration
FetchContent_MakeAvailable(fmt nlohmann_json)

# A system dependency, recreated as the same find_package() call, not fetched.
find_package(Threads REQUIRED)

# --- generated code ----------------------------------------------------------
configure_file(
  ${CMAKE_CURRENT_SOURCE_DIR}/cmake/version.hpp.in
  ${CMAKE_CURRENT_BINARY_DIR}/generated/complex_deep/version.hpp
  @ONLY)
add_library(cd_version INTERFACE)
target_include_directories(cd_version INTERFACE ${CMAKE_CURRENT_BINARY_DIR}/generated)

# --- first-party libraries (a tree several add_subdirectory levels deep) -----
add_subdirectory(libs)

# --- applications, each touching a different subset --------------------------
add_subdirectory(apps)
