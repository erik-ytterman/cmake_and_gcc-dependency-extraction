add_library(corecodec OBJECT src/codec.cpp)
target_include_directories(corecodec PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(corecodec PUBLIC core)
