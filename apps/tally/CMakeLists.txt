add_executable(tally src/main.cpp)
target_link_libraries(tally PRIVATE rng build_info)
