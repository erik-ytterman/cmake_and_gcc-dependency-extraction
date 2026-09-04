add_library(core src/core.cpp src/util.cpp src/detail/util.cpp)
target_include_directories(core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(core PUBLIC cd_version)

add_subdirectory(codec)     # an OBJECT library, one level deeper

if(BUILD_TESTING)
  add_executable(core_test test/core_test.cpp)
  target_link_libraries(core_test PRIVATE core)
  add_test(NAME core_test COMMAND core_test)
endif()
