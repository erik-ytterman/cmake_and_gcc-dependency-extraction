add_executable(guess src/main.cpp)
target_link_libraries(guess PRIVATE input rng fmt::fmt build_info)
