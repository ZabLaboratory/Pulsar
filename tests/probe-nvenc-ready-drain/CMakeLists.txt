if(WIN32 AND DEFINED PULSAR_UPSTREAM_BUILD_DIR)
  add_executable(ready-drain-runtime ready-drain-runtime.c)
  target_include_directories(ready-drain-runtime PRIVATE
    "${PULSAR_UPSTREAM_DIR}/libobs" "${PULSAR_UPSTREAM_BUILD_DIR}/config")
  target_link_libraries(ready-drain-runtime PRIVATE
    "${PULSAR_UPSTREAM_BUILD_DIR}/libobs/$<CONFIG>/obs.lib")
  target_compile_options(ready-drain-runtime PRIVATE /UNDEBUG)
  set_target_properties(ready-drain-runtime PROPERTIES C_STANDARD 11
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
  add_test(NAME ready-drain-runtime COMMAND ready-drain-runtime)
  set_tests_properties(ready-drain-runtime PROPERTIES TIMEOUT 15
    ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${PULSAR_UPSTREAM_BUILD_DIR}/rundir/$<CONFIG>/bin/64bit")
endif()
