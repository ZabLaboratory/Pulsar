# ws-debug-probe -- regression gate for issue #177
# (plugins/pulsar-websocket/src/obs-websocket.cpp:181-184 IsDebugEnabled()
# fail-open). Built without libobs/Qt for the same reason as tests/nv-probe:
# what's under test is a boolean predicate, not anything that needs a
# running OBS process.

cmake_minimum_required(VERSION 3.28)

add_executable(ws-debug-probe-test ws-debug-probe-test.cpp)
set_target_properties(ws-debug-probe-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
)

add_test(NAME pulsar-ws-debug-probe COMMAND ws-debug-probe-test)
set_tests_properties(pulsar-ws-debug-probe PROPERTIES
    TIMEOUT 30
    LABELS "security;unit"
)
