add_executable(greeter src/main.cpp)
target_link_libraries(greeter PRIVATE input fmt::fmt build_info)
