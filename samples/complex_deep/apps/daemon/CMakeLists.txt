add_executable(daemon src/main.cpp)
target_link_libraries(daemon PRIVATE net)    # Threads arrives through net (PUBLIC)
