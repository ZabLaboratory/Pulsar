if(TARGET pulsar-frontend-stub)
    add_executable(pulsar-media-transition-config-probe config-probe.cpp)
    target_compile_features(pulsar-media-transition-config-probe PRIVATE cxx_std_17)
    target_include_directories(pulsar-media-transition-config-probe PRIVATE
        "${PULSAR_UPSTREAM_DIR}/libobs" "${PULSAR_UPSTREAM_BUILD_DIR}/libobs"
        "${PULSAR_UPSTREAM_BUILD_DIR}/config")
    target_link_libraries(pulsar-media-transition-config-probe PRIVATE pulsar-frontend-stub)
    # The parser is a separate, pure object in the static frontend library;
    # this executable does not need libobs DLLs. Match the pipeline's probe artifact.
    set_target_properties(pulsar-media-transition-config-probe PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
    add_test(NAME pulsar-media-transition-config-probe COMMAND pulsar-media-transition-config-probe)
    set_tests_properties(pulsar-media-transition-config-probe PROPERTIES LABELS "unit")
endif()
