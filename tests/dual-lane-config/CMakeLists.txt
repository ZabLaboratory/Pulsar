cmake_minimum_required(VERSION 3.28)
project(pulsar-dual-lane-config-probe LANGUAGES CXX)

add_executable(pulsar-dual-lane-config-probe dual-lane-config-probe.cpp)
target_compile_features(pulsar-dual-lane-config-probe PRIVATE cxx_std_17)
target_include_directories(pulsar-dual-lane-config-probe PRIVATE
    "${CMAKE_SOURCE_DIR}/plugins/pulsar-frontend-stub/include"
)
set_target_properties(pulsar-dual-lane-config-probe PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    # The build job uploads the already-whitelisted nv-probe directory as
    # the standalone native-test aggregate. Keeping this probe there makes
    # the executable available to the downstream CTest artifact job.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe"
)

add_test(NAME pulsar-dual-lane-config-probe COMMAND pulsar-dual-lane-config-probe)
set_tests_properties(pulsar-dual-lane-config-probe PROPERTIES LABELS "unit")
