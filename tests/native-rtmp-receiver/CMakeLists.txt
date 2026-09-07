if(WIN32 AND DEFINED PULSAR_UPSTREAM_BUILD_DIR)
  file(GLOB _obs_deps_includes "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-*/include")
  find_path(PULSAR_RECEIVER_AV_INCLUDE libavcodec/avcodec.h HINTS ${_obs_deps_includes} REQUIRED)
  foreach(component avcodec avformat avutil)
    find_library(PULSAR_RECEIVER_${component}_LIB ${component}
      HINTS "${PULSAR_RECEIVER_AV_INCLUDE}/../lib" REQUIRED)
  endforeach()
  add_executable(native-rtmp-receiver native-rtmp-receiver.c)
  target_include_directories(native-rtmp-receiver PRIVATE
    "${PULSAR_UPSTREAM_DIR}/libobs" "${PULSAR_UPSTREAM_BUILD_DIR}/config" "${PULSAR_RECEIVER_AV_INCLUDE}")
  target_link_libraries(native-rtmp-receiver PRIVATE
    "${PULSAR_UPSTREAM_BUILD_DIR}/libobs/$<CONFIG>/obs.lib"
    "${PULSAR_RECEIVER_avcodec_LIB}" "${PULSAR_RECEIVER_avformat_LIB}" "${PULSAR_RECEIVER_avutil_LIB}")
  set_target_properties(native-rtmp-receiver PROPERTIES C_STANDARD 11
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/tests/nv-probe")
endif()
