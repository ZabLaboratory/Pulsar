cmake_minimum_required(VERSION 3.28)
project(pulsar-dual-lane-config-probe LANGUAGES CXX)

add_executable(pulsar-dual-lane-config-probe dual-lane-config-probe.cpp)
target_compile_features(pulsar-dual-lane-config-probe PRIVATE cxx_std_17)
target_include_directories(pulsar-dual-lane-config-probe PRIVATE
    "${CMAKE_SOURCE_DIR}/plugins/pulsar-frontend-stub/include"
)

add_test(NAME pulsar-dual-lane-config-probe COMMAND pulsar-dual-lane-config-probe)
set_tests_properties(pulsar-dual-lane-config-probe PROPERTIES LABELS "unit")
