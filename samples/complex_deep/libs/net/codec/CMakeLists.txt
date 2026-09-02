add_library(codec OBJECT src/codec.cpp)
target_include_directories(codec PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(codec PUBLIC cxx_std_17)
