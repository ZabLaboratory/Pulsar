# dir-hardening-probe -- regression gate for issue #201 (ADR-005 §5 R10).
#
# Links the REAL plugins/pulsar-headless/dir-hardening.cpp translation unit
# (not a mirror): that file is deliberately obs.h/Qt-free so it can be
# built standalone here, the same way tests/log-handler-probe already
# builds its own subject without libobs.

cmake_minimum_required(VERSION 3.28)

add_executable(dir-hardening-probe-test
    dir-hardening-probe-test.cpp
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless/dir-hardening.cpp"
)
target_include_directories(dir-hardening-probe-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless"
)
set_target_properties(dir-hardening-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # Same rationale as tests/log-handler-probe/CMakeLists.txt: pipeline.yml's
    # pulsar-rundir artifact upload only whitelists build/tests/nv-probe/**
    # for a standalone test binary; land here instead of leaving the CTest
    # entry "Not Run" in CI. Route to Keeper to give this probe its own
    # artifact path and drop this override.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

if(WIN32)
    target_link_libraries(dir-hardening-probe-test PRIVATE Advapi32)
endif()

add_test(NAME pulsar-dir-hardening-probe COMMAND dir-hardening-probe-test)
set_tests_properties(pulsar-dir-hardening-probe PROPERTIES
    TIMEOUT 60
    LABELS "security;unit"
)
