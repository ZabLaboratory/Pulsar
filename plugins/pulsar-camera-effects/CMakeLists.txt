cmake_minimum_required(VERSION 3.28)
project(pulsar-camera-effects LANGUAGES CXX)

set(PULSAR_CAMERA_LIBOBS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../upstream/libobs" CACHE PATH "libobs source headers")
if(NOT DEFINED PULSAR_UPSTREAM_BUILD_DIR)
  set(PULSAR_UPSTREAM_BUILD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../upstream/build_x64")
endif()
add_library(pulsar-camera-effects MODULE camera-effects.cpp)
target_compile_features(pulsar-camera-effects PRIVATE cxx_std_17)
target_include_directories(pulsar-camera-effects PRIVATE
  "${PULSAR_CAMERA_LIBOBS_DIR}"
  "${PULSAR_UPSTREAM_BUILD_DIR}/libobs"
  "${PULSAR_UPSTREAM_BUILD_DIR}/config")
target_link_libraries(pulsar-camera-effects PRIVATE
  "${PULSAR_UPSTREAM_BUILD_DIR}/libobs/RelWithDebInfo/obs.lib")
set_target_properties(pulsar-camera-effects PROPERTIES PREFIX ""
  LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/obs-plugins/64bit"
  RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/obs-plugins/64bit")
