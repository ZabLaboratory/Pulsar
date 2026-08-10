# output-classify-probe -- regression gate for issue #182 / ADR-005 §3.4
# (the closed reason_class set behind pulsar:OutputFailed). Built without
# libobs/Qt for the same reason as tests/ws-debug-probe: what's under test
# is a pure function, not anything that needs a running OBS process.

cmake_minimum_required(VERSION 3.28)

add_executable(output-classify-probe-test output-classify-probe-test.cpp)
target_link_libraries(output-classify-probe-test PRIVATE Pulsar::output-classify)
set_target_properties(output-classify-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # Same artifact-whitelist constraint as tests/ws-debug-probe (see that
    # CMakeLists.txt): pipeline.yml's pulsar-rundir upload only globs
    # build/tests/nv-probe/** for a standalone test binary. Land here
    # instead of leaving the CTest entry silently "Not Run" in CI.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-output-classify-probe COMMAND output-classify-probe-test)
set_tests_properties(pulsar-output-classify-probe PROPERTIES
    TIMEOUT 30
    LABELS "unit"
)
