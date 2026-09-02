add_library(base src/base.cpp)
target_include_directories(base PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(base PUBLIC cd_version)
target_compile_features(base PUBLIC cxx_std_17)

add_subdirectory(mathx)

if(BUILD_TESTING)
  add_executable(base_test test/base_test.cpp)
  target_link_libraries(base_test PRIVATE base)
  add_test(NAME base_test COMMAND base_test)
endif()
