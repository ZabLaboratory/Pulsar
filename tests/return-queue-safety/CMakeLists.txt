cmake_minimum_required(VERSION 3.28)
if(CMAKE_CURRENT_SOURCE_DIR STREQUAL CMAKE_SOURCE_DIR)
  project(return_queue_runtime LANGUAGES C)
  enable_testing()
endif()
if(NOT WIN32)
  return()
endif()
if(NOT DEFINED OBS_SOURCE_DIR)
  set(OBS_SOURCE_DIR "${PULSAR_UPSTREAM_DIR}")
endif()
add_executable(return-queue-runtime queue-runtime-test.c
  "${OBS_SOURCE_DIR}/shared/obs-shared-memory-queue/shared-memory-queue.c"
  "${OBS_SOURCE_DIR}/shared/obs-tiny-nv12-scale/tiny-nv12-scale.c")
target_include_directories(return-queue-runtime PRIVATE
  "${OBS_SOURCE_DIR}/shared/obs-shared-memory-queue"
  "${OBS_SOURCE_DIR}/shared/obs-tiny-nv12-scale")
target_compile_options(return-queue-runtime PRIVATE /UNDEBUG)
set_property(TARGET return-queue-runtime PROPERTY C_STANDARD 11)
set_target_properties(return-queue-runtime PROPERTIES RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
add_test(NAME return-queue-runtime COMMAND return-queue-runtime)
set_tests_properties(return-queue-runtime PROPERTIES TIMEOUT 30)
