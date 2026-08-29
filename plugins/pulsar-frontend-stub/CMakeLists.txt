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

# The frontend runtime owns the scene-switch vendor adapter and includes
# nlohmann/json directly. Resolve the dependency locally instead of relying on
# the sibling pulsar-websocket directory's CMake scope or build order.
file(GLOB _pulsar_obs_deps LIST_DIRECTORIES true
     "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-*-x64")
list(FILTER _pulsar_obs_deps EXCLUDE REGEX "obs-deps-qt6-")
list(SORT _pulsar_obs_deps)
list(LENGTH _pulsar_obs_deps _pulsar_obs_deps_count)
if(_pulsar_obs_deps_count EQUAL 0)
    message(FATAL_ERROR "No non-Qt x64 obs-deps directory found. Build upstream configure first.")
endif()
list(GET _pulsar_obs_deps 0 PULSAR_OBS_DEPS)
if(NOT EXISTS "${PULSAR_OBS_DEPS}/include/nlohmann/json.hpp")
    message(FATAL_ERROR
        "nlohmann_json headers not found in ${PULSAR_OBS_DEPS}. Build upstream configure first.")
endif()
list(APPEND CMAKE_PREFIX_PATH "${PULSAR_OBS_DEPS}")
set(nlohmann_json_DIR "${PULSAR_OBS_DEPS}/lib/cmake/nlohmann_json")
find_package(nlohmann_json 3.11 REQUIRED)

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
    nlohmann_json::nlohmann_json
)
