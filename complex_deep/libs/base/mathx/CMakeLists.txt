# Two sources with the same basename in one target (src/util.cpp and
# src/detail/util.cpp), plus a private header src/internal.hpp reached only by a
# file-relative #include. Under the old flatten-to-basename layout this could
# not be extracted; now the sub-directory structure is kept.
add_library(mathx
  src/util.cpp
  src/detail/util.cpp
)
target_include_directories(mathx PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(mathx PUBLIC base)

if(BUILD_TESTING)
  add_executable(mathx_test test/mathx_test.cpp)
  target_link_libraries(mathx_test PRIVATE mathx)
  add_test(NAME mathx_test COMMAND mathx_test)
endif()
