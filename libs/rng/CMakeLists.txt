add_library(rng src/rng.cpp)
target_include_directories(rng PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(rng PUBLIC cxx_std_17)

if(BUILD_TESTING)
  add_executable(rng_test test/rng_test.cpp)
  target_link_libraries(rng_test PRIVATE rng)
  add_test(NAME rng_test COMMAND rng_test)
endif()
