# pulsar-scene-source -- libobs plugin (loaded as DLL by obs_load_all_modules)
# providing the Pulsar vendor request `pulsar:SetCaptureSource` so a host
# application (Prism today, future apps tomorrow) can swap the broadcast
# capture source from the default `window_capture` set up by
# pulsar-frontend-stub to a `browser_source` pointed at a scene-server URL.
#
# Phase 13.4 ships browser_source kind only ; window_capture revert is
# not exposed yet (Phase 13.4.b will expose `revert_to_window_capture`
# along with the probe-script validation).

cmake_minimum_required(VERSION 3.28)
project(pulsar-scene-source LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(PULSAR_UPSTREAM_DIR "${PROJECT_SOURCE_DIR}/../../upstream")
get_filename_component(PULSAR_UPSTREAM_DIR "${PULSAR_UPSTREAM_DIR}" ABSOLUTE)
if(NOT DEFINED PULSAR_UPSTREAM_BUILD_DIR)
    set(PULSAR_UPSTREAM_BUILD_DIR "${PULSAR_UPSTREAM_DIR}/build_x64")
    get_filename_component(PULSAR_UPSTREAM_BUILD_DIR "${PULSAR_UPSTREAM_BUILD_DIR}" ABSOLUTE)
endif()

set(PULSAR_LIBOBS_HEADERS         "${PULSAR_UPSTREAM_DIR}/libobs")
set(PULSAR_LIBOBS_BUILD           "${PULSAR_UPSTREAM_BUILD_DIR}/libobs")
set(PULSAR_LIBOBS_CONFIG          "${PULSAR_UPSTREAM_BUILD_DIR}/config")
set(PULSAR_LIBOBS_LIB             "${PULSAR_LIBOBS_BUILD}/RelWithDebInfo/obs.lib")
set(PULSAR_FRONTEND_API_HEADERS   "${PULSAR_UPSTREAM_DIR}/frontend/api")
set(PULSAR_FRONTEND_API_LIB       "${PULSAR_UPSTREAM_BUILD_DIR}/frontend/api/RelWithDebInfo/obs-frontend-api.lib")

# obs-websocket-api.h is header-only, vendored inside our pulsar-websocket
# fork. Same dep pattern as pulsar-multi-stream.
set(PULSAR_WEBSOCKET_API_HEADERS "${PROJECT_SOURCE_DIR}/../pulsar-websocket/lib")

# DLL lands in upstream/build_x64/rundir/.../obs-plugins/64bit/ so libobs's
# add_default_module_paths picks it up at obs_load_all_modules time.
set(PULSAR_OBS_PLUGINS_DIR "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/obs-plugins/64bit")

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs import library not found at ${PULSAR_LIBOBS_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1")
endif()

if(NOT EXISTS "${PULSAR_FRONTEND_API_LIB}")
    message(FATAL_ERROR
        "obs-frontend-api import library not found at ${PULSAR_FRONTEND_API_LIB}.")
endif()

add_library(pulsar-scene-source MODULE
    src/plugin-main.cpp
)

target_include_directories(pulsar-scene-source PRIVATE
    "${PULSAR_LIBOBS_HEADERS}"
    "${PULSAR_LIBOBS_BUILD}"
    "${PULSAR_LIBOBS_CONFIG}"
    "${PULSAR_FRONTEND_API_HEADERS}"
    "${PULSAR_WEBSOCKET_API_HEADERS}"
)

target_link_libraries(pulsar-scene-source PRIVATE
    "${PULSAR_LIBOBS_LIB}"
    "${PULSAR_FRONTEND_API_LIB}"
)

set_target_properties(pulsar-scene-source PROPERTIES
    PREFIX ""           # no "lib" prefix on Windows DLL
    OUTPUT_NAME "pulsar-scene-source"
    LIBRARY_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_DEBUG          "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_RELEASE        "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG          "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE        "${PULSAR_OBS_PLUGINS_DIR}"
)
