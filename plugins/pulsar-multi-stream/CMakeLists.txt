# pulsar-multi-stream -- libobs plugin (loaded as DLL by obs_load_all_modules)
# providing first-class multi-destination streaming over an obs-websocket
# vendor namespace ("pulsar").
#
# Phase 7 ships the destinations API (kinds: rtmp_custom, vod_local) sharing
# encoders with frontend-stub's recording output. Phase 7e adds the Twitch
# kind (an rtmp_custom alias with a pinned base URL).

cmake_minimum_required(VERSION 3.28)
project(pulsar-multi-stream LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(PULSAR_UPSTREAM_DIR "${PROJECT_SOURCE_DIR}/../../upstream")
get_filename_component(PULSAR_UPSTREAM_DIR "${PULSAR_UPSTREAM_DIR}" ABSOLUTE)

set(PULSAR_LIBOBS_HEADERS         "${PULSAR_UPSTREAM_DIR}/libobs")
set(PULSAR_LIBOBS_BUILD           "${PULSAR_UPSTREAM_DIR}/build_x64/libobs")
set(PULSAR_LIBOBS_CONFIG          "${PULSAR_UPSTREAM_DIR}/build_x64/config")
set(PULSAR_LIBOBS_LIB             "${PULSAR_LIBOBS_BUILD}/RelWithDebInfo/obs.lib")
set(PULSAR_FRONTEND_API_HEADERS   "${PULSAR_UPSTREAM_DIR}/frontend/api")
set(PULSAR_FRONTEND_API_LIB       "${PULSAR_UPSTREAM_DIR}/build_x64/frontend/api/RelWithDebInfo/obs-frontend-api.lib")

# obs-websocket-api.h is header-only, vendored inside our pulsar-websocket
# fork. We only depend on the header; runtime registration discovers
# obs-websocket via libobs's proc handler at module post-load time.
set(PULSAR_WEBSOCKET_API_HEADERS "${PROJECT_SOURCE_DIR}/../pulsar-websocket/lib")

# ADR-005 §3.6: log-handler.h is header-only from this module's point of
# view -- only pulsar_log::DiagnosticsRing::kServerMaxLines (a constexpr) is
# used, never anything requiring log-handler.cpp's definitions, so no new
# link dependency on pulsar-headless is introduced.
set(PULSAR_LOG_HANDLER_HEADERS "${PROJECT_SOURCE_DIR}/../pulsar-headless")

# DLL lands in upstream/build_x64/rundir/.../obs-plugins/64bit/ so libobs's
# add_default_module_paths picks it up at obs_load_all_modules time.
set(PULSAR_OBS_PLUGINS_DIR "${PULSAR_UPSTREAM_DIR}/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit")

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs import library not found at ${PULSAR_LIBOBS_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1 -Stage build")
endif()

if(NOT EXISTS "${PULSAR_FRONTEND_API_LIB}")
    message(FATAL_ERROR
        "obs-frontend-api import library not found at ${PULSAR_FRONTEND_API_LIB}.")
endif()

add_library(pulsar-multi-stream MODULE
    src/plugin-main.cpp
)

target_include_directories(pulsar-multi-stream PRIVATE
    "${PULSAR_LIBOBS_HEADERS}"
    "${PULSAR_LIBOBS_BUILD}"
    "${PULSAR_LIBOBS_CONFIG}"
    "${PULSAR_FRONTEND_API_HEADERS}"
    "${PULSAR_WEBSOCKET_API_HEADERS}"
    "${PULSAR_LOG_HANDLER_HEADERS}"
)

target_link_libraries(pulsar-multi-stream PRIVATE
    "${PULSAR_LIBOBS_LIB}"
    "${PULSAR_FRONTEND_API_LIB}"
    # #167 -- header-only + version.lib. Carries the NVIDIA SDK capability
    # probe the manifest publishes, shared verbatim with the nv-filters
    # loaders so the two cannot disagree.
    Pulsar::nv-secure-load
    # #182 -- header-only. The pulsar:OutputFailed reason_class classifier,
    # shared verbatim with tests/output-classify-probe.
    Pulsar::output-classify
)

set_target_properties(pulsar-multi-stream PROPERTIES
    PREFIX ""           # no "lib" prefix on Windows DLL
    OUTPUT_NAME "pulsar-multi-stream"
    LIBRARY_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_DEBUG          "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_RELEASE        "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG          "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE        "${PULSAR_OBS_PLUGINS_DIR}"
)
