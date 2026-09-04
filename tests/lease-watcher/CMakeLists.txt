# lease-watcher -- native Win32 regression gate for the #254 registration pipe.

cmake_minimum_required(VERSION 3.28)

add_executable(lease-registration-pipe-probe-test
    registration-pipe-probe.cpp
)

set_target_properties(lease-registration-pipe-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-lease-registration-pipe-probe COMMAND lease-registration-pipe-probe-test)
set_tests_properties(pulsar-lease-registration-pipe-probe PROPERTIES
    TIMEOUT 30
    LABELS "runtime;unit;security"
)
