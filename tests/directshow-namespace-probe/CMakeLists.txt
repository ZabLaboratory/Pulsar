# directshow-namespace-probe -- executes the shared production C-compatible
# namespace helper against the #248 tri-state environment matrix.

cmake_minimum_required(VERSION 3.28)

add_executable(directshow-namespace-probe-test directshow-namespace-probe-test.cpp)

target_include_directories(directshow-namespace-probe-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../upstream/plugins/win-dshow"
)

set_target_properties(directshow-namespace-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-directshow-namespace-probe COMMAND directshow-namespace-probe-test)
set_tests_properties(pulsar-directshow-namespace-probe PROPERTIES
    TIMEOUT 30
    LABELS "runtime;unit;security"
)
