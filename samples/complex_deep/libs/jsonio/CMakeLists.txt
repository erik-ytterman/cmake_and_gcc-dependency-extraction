add_library(jsonio src/jsonio.cpp)
target_include_directories(jsonio PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(jsonio PUBLIC nlohmann_json::nlohmann_json core)

if(BUILD_TESTING)
  add_executable(jsonio_test test/jsonio_test.cpp)
  target_link_libraries(jsonio_test PRIVATE jsonio)
  add_test(NAME jsonio_test COMMAND jsonio_test)
endif()
