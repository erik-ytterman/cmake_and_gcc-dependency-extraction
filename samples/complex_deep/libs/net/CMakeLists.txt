add_subdirectory(codec)   # OBJECT library

add_library(net src/net.cpp)
target_include_directories(net PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(net
  PUBLIC  Threads::Threads base
  PRIVATE codec                # OBJECT: its objects fold into libnet.a
)

if(BUILD_TESTING)
  add_executable(net_test test/net_test.cpp)
  target_link_libraries(net_test PRIVATE net)
  add_test(NAME net_test COMMAND net_test)
endif()
