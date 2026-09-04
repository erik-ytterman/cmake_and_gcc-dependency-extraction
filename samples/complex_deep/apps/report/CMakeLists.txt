add_executable(report src/main.cpp)
target_link_libraries(report PRIVATE core corecodec jsonio textutil cd_version)
