# output-attempt-settled-probe -- regression gate for issue #186 / ADR-005
# §3.5 (pulsar:OutputAttemptSettled). Same rationale as
# tests/output-classify-probe: what's under test is a pure function of
# (is_local_output, code, last_error) plus the outcome/reason_class-absence
# contract this issue owns -- no libobs/Qt/OBS runtime needed.

cmake_minimum_required(VERSION 3.28)

add_executable(output-attempt-settled-probe-test output-attempt-settled-probe-test.cpp)
target_link_libraries(output-attempt-settled-probe-test PRIVATE Pulsar::output-classify)
set_target_properties(output-attempt-settled-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # Same artifact-whitelist constraint as tests/output-classify-probe (see
    # that CMakeLists.txt): land in build/tests/nv-probe/** so pipeline.yml's
    # pulsar-rundir upload glob picks it up instead of leaving the CTest
    # entry silently "Not Run" in CI.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-output-attempt-settled-probe COMMAND output-attempt-settled-probe-test)
set_tests_properties(pulsar-output-attempt-settled-probe PROPERTIES
    TIMEOUT 30
    LABELS "unit"
)
