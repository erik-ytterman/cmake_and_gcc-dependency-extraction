add_library(textutil src/textutil.cpp)
target_include_directories(textutil PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(textutil PUBLIC Boost::algorithm core)

if(BUILD_TESTING)
  add_executable(textutil_test test/textutil_test.cpp)
  target_link_libraries(textutil_test PRIVATE textutil)
  add_test(NAME textutil_test COMMAND textutil_test)
endif()
