add_library(text src/text.cpp)
target_include_directories(text PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(text PUBLIC fmt::fmt base)

if(BUILD_TESTING)
  add_executable(text_test test/text_test.cpp)
  target_link_libraries(text_test PRIVATE text)
  add_test(NAME text_test COMMAND text_test)
endif()
