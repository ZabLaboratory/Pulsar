# runtime-isolation-probe -- regression gate for issue #243 / ADR-PULSAR-
# DUAL-LANE-001 §6.4.  It links the exact production runtime identity and
# lease implementation; no test mirror is permitted.

cmake_minimum_required(VERSION 3.28)

add_executable(runtime-isolation-probe-test
    runtime-isolation-probe-test.cpp
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless/runtime-identity.cpp"
)

target_include_directories(runtime-isolation-probe-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless"
)

set_target_properties(runtime-isolation-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # Keep the binary under the path already whitelisted by the Windows
    # pipeline artifact upload, as the other standalone probes do.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

if(WIN32)
    target_link_libraries(runtime-isolation-probe-test PRIVATE Ws2_32)
endif()

add_test(NAME pulsar-runtime-isolation-probe COMMAND runtime-isolation-probe-test)
set_tests_properties(pulsar-runtime-isolation-probe PROPERTIES
    TIMEOUT 60
    LABELS "runtime;unit;integration"
)
