add_library(data src/data.cpp)
target_include_directories(data PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(data PUBLIC nlohmann_json::nlohmann_json base)

if(BUILD_TESTING)
  add_executable(data_test test/data_test.cpp)
  target_link_libraries(data_test PRIVATE data)
  add_test(NAME data_test COMMAND data_test)
endif()
