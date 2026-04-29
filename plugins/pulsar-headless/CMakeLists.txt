# pulsar-headless plugin -- service-mode entry for Pulsar.
#
# Builds an executable that links against libobs, the artefact
# upstream produces under upstream/build_x64/libobs/. Configure +
# build the upstream submodule first via scripts/build-win.ps1
# before invoking this directory.
#
# Phase 3b ships only the bootstrap-and-exit smoke. Phase 4+ adds
# video/audio reset, module loading, signal handling, and the
# websocket server hookup.

cmake_minimum_required(VERSION 3.28)
project(pulsar-headless LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Resolve upstream paths from the top-level project root.
set(PULSAR_UPSTREAM_DIR "${PROJECT_SOURCE_DIR}/../../upstream")
get_filename_component(PULSAR_UPSTREAM_DIR "${PULSAR_UPSTREAM_DIR}" ABSOLUTE)

set(PULSAR_LIBOBS_HEADERS "${PULSAR_UPSTREAM_DIR}/libobs")
set(PULSAR_LIBOBS_BUILD   "${PULSAR_UPSTREAM_DIR}/build_x64/libobs")
set(PULSAR_LIBOBS_CONFIG  "${PULSAR_UPSTREAM_DIR}/build_x64/config")
set(PULSAR_LIBOBS_LIB     "${PULSAR_LIBOBS_BUILD}/RelWithDebInfo/obs.lib")
set(PULSAR_RUNDIR_BIN     "${PULSAR_UPSTREAM_DIR}/build_x64/rundir/RelWithDebInfo/bin/64bit")

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs import library not found at ${PULSAR_LIBOBS_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1 -Stage build")
endif()

if(NOT EXISTS "${PULSAR_LIBOBS_HEADERS}/obs.h")
    message(FATAL_ERROR
        "libobs headers not found at ${PULSAR_LIBOBS_HEADERS}/obs.h.\n"
        "Initialise the upstream/ submodule.")
endif()

add_executable(pulsar-headless main.cpp)

target_include_directories(pulsar-headless PRIVATE
    "${PULSAR_LIBOBS_HEADERS}"   # obs.h, obs-source.h, ...
    "${PULSAR_LIBOBS_BUILD}"     # generated libobs_EXPORT.h
    "${PULSAR_LIBOBS_CONFIG}"    # generated obsconfig.h
)

target_link_libraries(pulsar-headless PRIVATE
    "${PULSAR_LIBOBS_LIB}"
)

set_target_properties(pulsar-headless PROPERTIES
    OUTPUT_NAME "pulsar"
    # Land next to obs.dll so the loader resolves libobs at runtime
    # without touching PATH. Multi-config generators (Visual Studio)
    # honour the per-config _$<CONFIG> variants, so explicitly point
    # RelWithDebInfo at the upstream rundir.
    RUNTIME_OUTPUT_DIRECTORY                "${PULSAR_RUNDIR_BIN}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_RUNDIR_BIN}"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG          "${PULSAR_RUNDIR_BIN}"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE        "${PULSAR_RUNDIR_BIN}"
)
