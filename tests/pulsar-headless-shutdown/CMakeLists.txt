cmake_minimum_required(VERSION 3.28)
project(pulsar-audio-callback-gate-test LANGUAGES CXX)
enable_testing()

add_executable(pulsar-audio-callback-gate-test audio-callback-gate-test.cpp)
target_compile_features(pulsar-audio-callback-gate-test PRIVATE cxx_std_17)
target_include_directories(pulsar-audio-callback-gate-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-browser"
)
set_target_properties(pulsar-audio-callback-gate-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-audio-callback-gate-test COMMAND pulsar-audio-callback-gate-test)
set_tests_properties(pulsar-audio-callback-gate-test PROPERTIES LABELS "unit")
