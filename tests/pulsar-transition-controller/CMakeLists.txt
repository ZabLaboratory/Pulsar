cmake_minimum_required(VERSION 3.28)
project(pulsar-transition-controller-probe LANGUAGES CXX)

add_executable(pulsar-transition-controller-probe transition-controller-probe.cpp)
target_compile_features(pulsar-transition-controller-probe PRIVATE cxx_std_17)
target_include_directories(pulsar-transition-controller-probe PRIVATE
    "${CMAKE_SOURCE_DIR}/plugins/pulsar-frontend-stub/include")
set_target_properties(pulsar-transition-controller-probe PROPERTIES
    CXX_EXTENSIONS OFF
    # The Windows pipeline uploads build/tests/nv-probe/** as the native probe
    # aggregate consumed by its downstream CTest job. Keep this target in that
    # artifact contract so the registered test and downloaded executable agree.
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
enable_testing()
add_test(NAME pulsar-transition-controller-probe COMMAND pulsar-transition-controller-probe)
set_tests_properties(pulsar-transition-controller-probe PROPERTIES LABELS "unit")
