# pulsar-websocket -- vendor fork of obsproject/obs-websocket v5.7.3.
#
# Phase 4d build wiring. Compiles every .cpp under src/ (forms/ was
# dropped in Phase 4c, see README.md), links against the libobs +
# obs-frontend-api that upstream produced under
# upstream/build_x64/, plus Qt6 Core/Network and the header-only
# deps (Asio, websocketpp, nlohmann_json) staged by upstream's
# windows-x64 preset under upstream/.deps/.
#
# Output: obs-websocket.dll dropped into
#   upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/
# so libobs's default module path (../../obs-plugins/64bit/ relative
# to bin/64bit/) picks it up at obs_load_all_modules() time.

cmake_minimum_required(VERSION 3.28)
project(pulsar-websocket VERSION 5.7.3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# --- Resolve upstream paths -------------------------------------------------

set(PULSAR_UPSTREAM_DIR "${PROJECT_SOURCE_DIR}/../../upstream")
get_filename_component(PULSAR_UPSTREAM_DIR "${PULSAR_UPSTREAM_DIR}" ABSOLUTE)

set(PULSAR_LIBOBS_HEADERS  "${PULSAR_UPSTREAM_DIR}/libobs")
set(PULSAR_LIBOBS_BUILD    "${PULSAR_UPSTREAM_DIR}/build_x64/libobs")
set(PULSAR_LIBOBS_CONFIG   "${PULSAR_UPSTREAM_DIR}/build_x64/config")
set(PULSAR_LIBOBS_LIB      "${PULSAR_LIBOBS_BUILD}/RelWithDebInfo/obs.lib")

set(PULSAR_FRONTEND_HEADERS "${PULSAR_UPSTREAM_DIR}/frontend/api")
set(PULSAR_FRONTEND_BUILD   "${PULSAR_UPSTREAM_DIR}/build_x64/frontend/api")
set(PULSAR_FRONTEND_LIB     "${PULSAR_FRONTEND_BUILD}/RelWithDebInfo/obs-frontend-api.lib")

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs.lib not found at ${PULSAR_LIBOBS_LIB}. "
        "Build upstream first via scripts/build-win.ps1 -Stage build.")
endif()

if(NOT EXISTS "${PULSAR_FRONTEND_LIB}")
    message(FATAL_ERROR
        "obs-frontend-api.lib not found at ${PULSAR_FRONTEND_LIB}. "
        "Build upstream first.")
endif()

# --- Locate obs-deps (header-only Asio/Websocketpp + nlohmann_json) ---------

file(GLOB _deps_dirs LIST_DIRECTORIES true
     "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-*-x64")
list(FILTER _deps_dirs EXCLUDE REGEX "obs-deps-qt6-")
list(GET _deps_dirs 0 PULSAR_OBS_DEPS)

if(NOT EXISTS "${PULSAR_OBS_DEPS}/include/asio.hpp")
    message(FATAL_ERROR
        "obs-deps not found at ${PULSAR_OBS_DEPS}. "
        "Run scripts/build-win.ps1 -Stage configure first.")
endif()

# --- Locate Qt6 -------------------------------------------------------------

file(GLOB _qt6_dirs LIST_DIRECTORIES true
     "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-qt6-*-x64")
list(GET _qt6_dirs 0 PULSAR_QT6_ROOT)

list(APPEND CMAKE_PREFIX_PATH "${PULSAR_QT6_ROOT}" "${PULSAR_OBS_DEPS}")

# Upstream's source pulls in Qt Widgets + Gui types beyond just
# Core/Network -- screenshot helpers (QImageWriter, QGuiApplication),
# Platform.h (QSystemTrayIcon), and a QMainWindow cast in
# RequestHandler_Config. The widgets are never instantiated at
# runtime in headless mode (no menu, no UI requests), but the
# headers must compile. Link them; the Qt6 Widgets/Gui DLLs are
# already staged in the rundir by Phase 4b.
find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Network)
find_package(nlohmann_json 3.11 REQUIRED)

# --- Plugin module ----------------------------------------------------------

# Every .cpp/.h under src/. forms/ was dropped in Phase 4c so the
# glob excludes it implicitly (the directory does not exist).
file(GLOB_RECURSE _pulsar_ws_sources CONFIGURE_DEPENDS
     "${PROJECT_SOURCE_DIR}/src/*.cpp"
     "${PROJECT_SOURCE_DIR}/src/*.h")

add_library(pulsar-websocket MODULE ${_pulsar_ws_sources})

set_target_properties(pulsar-websocket PROPERTIES
    OUTPUT_NAME "obs-websocket"   # match upstream so v5 client tooling
                                  # sees the same module name
    PREFIX ""
    AUTOMOC ON
)

# Generated plugin-macros header carries OBS_WEBSOCKET_VERSION etc.
configure_file(
    "${PROJECT_SOURCE_DIR}/src/plugin-macros.h.in"
    "${CMAKE_CURRENT_BINARY_DIR}/plugin-macros.generated.h")
target_sources(pulsar-websocket PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}/plugin-macros.generated.h")

# Plugin-macros.h.in expects OBS_WEBSOCKET_RPC_VERSION too -- mirror
# upstream's CMakeLists definitions so the generated header populates.
target_compile_definitions(pulsar-websocket PRIVATE
    ASIO_STANDALONE
    _WEBSOCKETPP_CPP11_STL_
    _WIN32_WINNT=0x0603
)
set(OBS_WEBSOCKET_RPC_VERSION 1)
set(obs-websocket_VERSION ${PROJECT_VERSION})

# Re-run configure_file now that the version vars are set so the
# generated header has them substituted.
configure_file(
    "${PROJECT_SOURCE_DIR}/src/plugin-macros.h.in"
    "${CMAKE_CURRENT_BINARY_DIR}/plugin-macros.generated.h" @ONLY)

target_compile_options(pulsar-websocket PRIVATE
    /wd4267   # size_t conversion warnings inside websocketpp
    /wd4996   # deprecated function warnings in Qt + Asio
)

target_include_directories(pulsar-websocket PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}"        # plugin-macros.generated.h
    "${PROJECT_SOURCE_DIR}/src"
    "${PROJECT_SOURCE_DIR}/lib"
    "${PULSAR_LIBOBS_HEADERS}"
    "${PULSAR_LIBOBS_BUILD}"
    "${PULSAR_LIBOBS_CONFIG}"
    "${PULSAR_FRONTEND_HEADERS}"
    "${PULSAR_FRONTEND_BUILD}"
    "${PULSAR_OBS_DEPS}/include"          # asio.hpp, websocketpp/
)

target_link_libraries(pulsar-websocket PRIVATE
    "${PULSAR_LIBOBS_LIB}"
    "${PULSAR_FRONTEND_LIB}"
    Qt6::Core
    Qt6::Gui
    Qt6::Widgets
    Qt6::Network
    nlohmann_json::nlohmann_json
)

# Drop the .dll into the rundir so libobs's default module path
# picks it up. obs-plugins/64bit/ relative to bin/64bit/ is what
# add_default_module_paths() searches.
set(_rundir_plugins
    "${PULSAR_UPSTREAM_DIR}/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit")

set_target_properties(pulsar-websocket PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY                "${_rundir_plugins}"
    LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${_rundir_plugins}"
    RUNTIME_OUTPUT_DIRECTORY                "${_rundir_plugins}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${_rundir_plugins}"
)

# Stage the locale data so obs_module_text() resolves at runtime.
set(_rundir_data
    "${PULSAR_UPSTREAM_DIR}/build_x64/rundir/RelWithDebInfo/data/obs-plugins/obs-websocket")
add_custom_command(TARGET pulsar-websocket POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${_rundir_data}/locale"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${PROJECT_SOURCE_DIR}/data/locale/en-US.ini"
            "${_rundir_data}/locale/en-US.ini")
