cmake_minimum_required(VERSION 3.28)
project(pulsar-telemetry-selector-probe LANGUAGES CXX)

add_executable(pulsar-telemetry-selector-probe telemetry-selector-probe.cpp)
target_compile_features(pulsar-telemetry-selector-probe PRIVATE cxx_std_17)
target_include_directories(pulsar-telemetry-selector-probe PRIVATE
    "${CMAKE_SOURCE_DIR}/plugins/pulsar-frontend-stub/include"
)
add_test(NAME pulsar-telemetry-selector-probe COMMAND pulsar-telemetry-selector-probe)
set_tests_properties(pulsar-telemetry-selector-probe PROPERTIES LABELS "unit")
