# pulsar-frontend-stub -- static library linked into pulsar-headless.
#
# Implements obs_frontend_callbacks and registers the implementation
# via obs_frontend_set_callbacks_internal so plugins (notably
# obs-websocket) find a populated frontend table when their
# obs_module_load runs. See README.md for the rationale.

cmake_minimum_required(VERSION 3.28)
project(pulsar-frontend-stub LANGUAGES CXX)

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

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs import library not found at ${PULSAR_LIBOBS_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1 -Stage build")
endif()

if(NOT EXISTS "${PULSAR_FRONTEND_API_LIB}")
    message(FATAL_ERROR
        "obs-frontend-api import library not found at ${PULSAR_FRONTEND_API_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1 -Stage build")
endif()

if(NOT EXISTS "${PULSAR_FRONTEND_API_HEADERS}/obs-frontend-internal.hpp")
    message(FATAL_ERROR
        "obs-frontend-internal.hpp not found at ${PULSAR_FRONTEND_API_HEADERS}.\n"
        "The upstream/ submodule must be initialised.")
endif()

add_library(pulsar-frontend-stub STATIC
    src/pulsar-frontend-stub.cpp
)

target_include_directories(pulsar-frontend-stub PUBLIC
    "${PROJECT_SOURCE_DIR}/include"
)

target_include_directories(pulsar-frontend-stub PRIVATE
    "${PULSAR_LIBOBS_HEADERS}"
    "${PULSAR_LIBOBS_BUILD}"
    "${PULSAR_LIBOBS_CONFIG}"
    "${PULSAR_FRONTEND_API_HEADERS}"   # obs-frontend-internal.hpp
    "${PROJECT_SOURCE_DIR}/../pulsar-websocket/lib" # obs-websocket vendor API
)

target_link_libraries(pulsar-frontend-stub PUBLIC
    "${PULSAR_LIBOBS_LIB}"
    "${PULSAR_FRONTEND_API_LIB}"
    bcrypt
)
