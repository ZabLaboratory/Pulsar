if(WIN32 AND DEFINED PULSAR_UPSTREAM_BUILD_DIR)
  add_executable(async-gpu-runtime async-gpu-runtime.c)
  target_include_directories(async-gpu-runtime PRIVATE
    "${PULSAR_UPSTREAM_DIR}/libobs" "${PULSAR_UPSTREAM_BUILD_DIR}/config")
  target_link_libraries(async-gpu-runtime PRIVATE
    "${PULSAR_UPSTREAM_BUILD_DIR}/libobs/$<CONFIG>/obs.lib")
  target_compile_options(async-gpu-runtime PRIVATE /UNDEBUG)
  set_target_properties(async-gpu-runtime PROPERTIES C_STANDARD 11
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
  # Pulsar's Windows libobs deliberately resolves its effects relative to the
  # executable, not cwd or user-controlled data search paths.
  add_custom_command(TARGET async-gpu-runtime POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_directory
      "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/$<CONFIG>/data/libobs"
      "${CMAKE_BINARY_DIR}/tests/data/libobs")
  add_test(NAME async-gpu-runtime COMMAND async-gpu-runtime)
  add_test(NAME async-gpu-pending-error COMMAND async-gpu-runtime --pending-error)
  set_tests_properties(async-gpu-runtime async-gpu-pending-error PROPERTIES TIMEOUT 25
    ENVIRONMENT_MODIFICATION "PATH=path_list_prepend:${PULSAR_UPSTREAM_BUILD_DIR}/rundir/$<CONFIG>/bin/64bit")
endif()
