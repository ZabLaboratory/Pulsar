# pulsar-browser -- libobs plugin DLL (forked from upstream obs-browser)
# providing browser_source rendering via Chromium Embedded Framework.
#
# Why a fork (Phase 13.4):
#   - We build without Qt linkage (full headless ; every Qt usage in
#     upstream is gated behind ENABLE_BROWSER_QT_LOOP, which we leave
#     undefined and hand-wrap where upstream forgot the guard).
#   - We need to inject `--no-sandbox` via OnBeforeCommandLineProcessing
#     because we build CEF without the sandbox SDK ; the renderer
#     subprocess fails to init otherwise.
#   - We rename the subprocess helper to `pulsar-browser-page.exe` so
#     it cannot be confused with the upstream binary.
#
# build-win.ps1 deletes the upstream obs-browser.dll from the rundir
# before our plugin builds, so libobs only sees our pulsar-browser.dll
# at obs_load_all_modules time. Same `browser_source` source kind,
# identical from libobs's perspective.

cmake_minimum_required(VERSION 3.28)
project(pulsar-browser LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# CEF on Windows ships its libcef.dll built against the static MSVC
# runtime (/MT). Anything that links libcef.lib + libcef_dll_wrapper.lib
# must compile with the same /MT flag, otherwise the linker reports
# RuntimeLibrary mismatches against every .obj in libcef_dll_wrapper.
# Set the modern CMake variable BEFORE add_subdirectory(libcef_dll) so
# the wrapper inherits the right flag too.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
cmake_policy(SET CMP0091 NEW)

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
set(PULSAR_WEBSOCKET_API_HEADERS  "${PROJECT_SOURCE_DIR}/../pulsar-websocket/lib")
set(PULSAR_OBS_PLUGINS_DIR        "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/obs-plugins/64bit")
set(PULSAR_OBS_BIN_DIR            "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/bin/64bit")

if(NOT EXISTS "${PULSAR_LIBOBS_LIB}")
    message(FATAL_ERROR
        "libobs import library not found at ${PULSAR_LIBOBS_LIB}.\n"
        "Build upstream first: scripts/build-win.ps1 -Full")
endif()

# CEF — vendored in upstream/.deps/cef_binary_6533_windows_x64/
file(GLOB CEF_DEPS_DIRS "${PULSAR_UPSTREAM_DIR}/.deps/cef_binary_*")
if(NOT CEF_DEPS_DIRS)
    message(FATAL_ERROR
        "CEF binary distribution not found under ${PULSAR_UPSTREAM_DIR}/.deps/.\n"
        "Run scripts/build-win.ps1 -Full first to fetch it.")
endif()
list(GET CEF_DEPS_DIRS 0 CEF_ROOT_DIR)
message(STATUS "Pulsar browser : using CEF at ${CEF_ROOT_DIR}")

# nlohmann_json — from obs-deps
file(GLOB OBS_DEPS_X64 "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-*-x64")
if(NOT OBS_DEPS_X64)
    message(FATAL_ERROR
        "obs-deps not found under ${PULSAR_UPSTREAM_DIR}/.deps/.\n"
        "Run scripts/build-win.ps1 -Full first to fetch it.")
endif()
list(GET OBS_DEPS_X64 0 PULSAR_OBS_DEPS_X64)
set(nlohmann_json_DIR "${PULSAR_OBS_DEPS_X64}/share/cmake/nlohmann_json")
find_package(nlohmann_json 3.11 REQUIRED)

# PULSAR: NO Qt linkage. The fork is full headless — every Qt usage
# in upstream obs-browser is gated behind ENABLE_BROWSER_QT_LOOP
# (which we deliberately leave undefined) or has been wrapped in
# `#ifdef ENABLE_BROWSER_QT_LOOP` in our fork (browser-client.cpp,
# obs-browser-source.cpp). The `OnTooltip` callback became a no-op
# returning false so CEF falls back to native default behaviour.
# Result : zero Qt headers, zero Qt6 link, no QApplication anywhere.

# CEF cmake helpers + targets (CEF::Wrapper, CEF::Library). FindCEF.cmake
# requires CEF_ROOT to be set before find_package — not just on the
# module path.
set(CEF_ROOT "${CEF_ROOT_DIR}")
list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT_DIR}/cmake")
find_package(CEF 95 REQUIRED)
add_subdirectory("${CEF_ROOT_DIR}/libcef_dll" libcef_dll_wrapper)

# ----------------------------------------------------------------------------
# pulsar-browser.dll — the libobs plugin loaded by obs_load_all_modules.
# ----------------------------------------------------------------------------
add_library(pulsar-browser MODULE
    browser-app.cpp
    browser-app.hpp
    browser-client.cpp
    browser-client.hpp
    browser-render-callback-gate.hpp
    browser-source-task-state.hpp
    browser-scheme.cpp
    browser-scheme.hpp
    browser-version.h
    cef-headers.hpp
    deps/base64/base64.cpp
    deps/base64/base64.hpp
    deps/ip-string-windows.cpp
    deps/ip-string.hpp
    deps/signal-restore.cpp
    deps/signal-restore.hpp
    deps/wide-string.cpp
    deps/wide-string.hpp
    obs-browser-plugin.cpp
    obs-browser-source.cpp
    obs-browser-source.hpp
)

target_include_directories(pulsar-browser PRIVATE
    "${PROJECT_SOURCE_DIR}"
    "${PROJECT_SOURCE_DIR}/deps"
    "${PULSAR_LIBOBS_HEADERS}"
    "${PULSAR_LIBOBS_BUILD}"
    "${PULSAR_LIBOBS_CONFIG}"
    "${PULSAR_FRONTEND_API_HEADERS}"
    "${PULSAR_WEBSOCKET_API_HEADERS}"
    "${PULSAR_UPSTREAM_DIR}/deps/w32-pthreads"  # libobs's util/threading.h pulls pthread.h
    "${CEF_ROOT_DIR}"
)

target_compile_options(pulsar-browser PRIVATE $<IF:$<CONFIG:DEBUG>,/MTd,/MT>)
target_compile_definitions(pulsar-browser PRIVATE ENABLE_BROWSER_SHARED_TEXTURE)
target_link_options(pulsar-browser PRIVATE /IGNORE:4099)

target_link_libraries(pulsar-browser PRIVATE
    "${PULSAR_LIBOBS_LIB}"
    "${PULSAR_FRONTEND_API_LIB}"
    libcef_dll_wrapper
    "${CEF_ROOT_DIR}/Release/libcef.lib"
    nlohmann_json::nlohmann_json
    d3d11
    dxgi
    Ws2_32
)

set_target_properties(pulsar-browser PROPERTIES
    PREFIX ""
    OUTPUT_NAME "pulsar-browser"
    LIBRARY_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    LIBRARY_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
)

# ----------------------------------------------------------------------------
# pulsar-browser-page.exe — CEF subprocess helper. CEF spawns this
# binary for renderer / GPU subprocesses. Path is set by the plugin
# at CefInitialize via cef_settings_t.browser_subprocess_path.
# ----------------------------------------------------------------------------
add_executable(pulsar-browser-page WIN32
    browser-app.cpp
    browser-app.hpp
    cef-headers.hpp
    obs-browser-page.manifest
    obs-browser-page/obs-browser-page-main.cpp
)

target_include_directories(pulsar-browser-page PRIVATE
    "${PROJECT_SOURCE_DIR}"
    "${PROJECT_SOURCE_DIR}/deps"
    "${PROJECT_SOURCE_DIR}/obs-browser-page"
    "${CEF_ROOT_DIR}"
)

target_compile_options(pulsar-browser-page PRIVATE $<IF:$<CONFIG:DEBUG>,/MTd,/MT>)
target_compile_definitions(pulsar-browser-page PRIVATE ENABLE_BROWSER_SHARED_TEXTURE)
target_link_options(pulsar-browser-page PRIVATE /IGNORE:4099 /SUBSYSTEM:WINDOWS)

target_link_libraries(pulsar-browser-page PRIVATE
    libcef_dll_wrapper
    "${CEF_ROOT_DIR}/Release/libcef.lib"
    nlohmann_json::nlohmann_json
)

# pulsar-browser-page.exe must ship in the SAME directory as
# pulsar-browser.dll (and libcef.dll), not in bin/64bit/. Two reasons :
#   1. obs-browser-plugin.cpp::BrowserInit() builds the subprocess
#      path from `obs_get_module_binary_path(pulsar-browser.dll)` which
#      resolves to obs-plugins/64bit/ — if the helper lives anywhere
#      else, CEF's CreateProcess gets "file not found" and every
#      subprocess (GPU, network, renderer) fails to launch with
#      error_code=63, then "GPU process isn't usable. Goodbye."
#   2. The upstream obs-browser CEF copy step lands libcef.dll and the
#      whole CEF runtime payload in obs-plugins/64bit/. Co-locating the
#      helper exe there means the Windows loader resolves libcef.dll
#      naturally — no extra copy step needed.
# This mirrors upstream OBS Studio layout exactly.
set_target_properties(pulsar-browser-page PROPERTIES
    OUTPUT_NAME "pulsar-browser-page"
    RUNTIME_OUTPUT_DIRECTORY                "${PULSAR_OBS_PLUGINS_DIR}"
    RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO "${PULSAR_OBS_PLUGINS_DIR}"
)
