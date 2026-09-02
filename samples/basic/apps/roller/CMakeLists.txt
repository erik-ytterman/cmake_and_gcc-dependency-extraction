add_executable(roller src/main.cpp)
target_link_libraries(roller PRIVATE rng fmt::fmt build_info)
