# log-handler-probe -- regression gate for issue #183 (ADR-005 §3.1-§3.2).
#
# Links the REAL plugins/pulsar-headless/log-handler.cpp translation unit
# (not a mirror): that file is deliberately obs.h/Qt-free so it can be
# built standalone here, the same way tests/nv-probe and tests/ws-debug-probe
# already build their own subjects without libobs.

cmake_minimum_required(VERSION 3.28)

add_executable(log-handler-probe-test
    log-handler-probe-test.cpp
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless/log-handler.cpp"
)
target_include_directories(log-handler-probe-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless"
)
set_target_properties(log-handler-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # Same rationale as tests/ws-debug-probe/CMakeLists.txt: pipeline.yml's
    # pulsar-rundir artifact upload only whitelists build/tests/nv-probe/**
    # for a standalone test binary; land here instead of leaving the CTest
    # entry "Not Run" in CI. Route to Keeper to give this probe its own
    # artifact path and drop this override.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

if(WIN32)
    target_link_libraries(log-handler-probe-test PRIVATE Advapi32)
endif()

add_test(NAME pulsar-log-handler-probe COMMAND log-handler-probe-test)
set_tests_properties(pulsar-log-handler-probe PROPERTIES
    TIMEOUT 60
    LABELS "security;unit"
)
