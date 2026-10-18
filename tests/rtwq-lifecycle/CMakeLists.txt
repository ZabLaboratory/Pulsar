cmake_minimum_required(VERSION 3.28)

add_executable(rtwq-lifecycle-test
    rtwq-lifecycle-test.cpp
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless/realtime-work-queue.cpp"
)
target_include_directories(rtwq-lifecycle-test PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../../plugins/pulsar-headless"
)
target_link_libraries(rtwq-lifecycle-test PRIVATE Rtworkq)
set_target_properties(rtwq-lifecycle-test PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)
add_test(NAME pulsar-rtwq-lifecycle COMMAND rtwq-lifecycle-test)
set_tests_properties(pulsar-rtwq-lifecycle PROPERTIES TIMEOUT 30 LABELS "runtime;integration")
