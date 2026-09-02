add_executable(render src/main.cpp)
target_link_libraries(render PRIVATE text)   # fmt arrives through text (PUBLIC)
