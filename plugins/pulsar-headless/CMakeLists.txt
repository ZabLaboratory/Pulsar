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
if(NOT DEFINED PULSAR_UPSTREAM_BUILD_DIR)
    set(PULSAR_UPSTREAM_BUILD_DIR "${PULSAR_UPSTREAM_DIR}/build_x64")
    get_filename_component(PULSAR_UPSTREAM_BUILD_DIR "${PULSAR_UPSTREAM_BUILD_DIR}" ABSOLUTE)
endif()

set(PULSAR_LIBOBS_HEADERS "${PULSAR_UPSTREAM_DIR}/libobs")
set(PULSAR_LIBOBS_BUILD   "${PULSAR_UPSTREAM_BUILD_DIR}/libobs")
set(PULSAR_LIBOBS_CONFIG  "${PULSAR_UPSTREAM_BUILD_DIR}/config")
set(PULSAR_LIBOBS_LIB     "${PULSAR_LIBOBS_BUILD}/RelWithDebInfo/obs.lib")
set(PULSAR_RUNDIR_BIN     "${PULSAR_UPSTREAM_BUILD_DIR}/rundir/RelWithDebInfo/bin/64bit")

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

# Locate Qt6 from the obs-deps Qt6 tarball that upstream's preset
# downloads under upstream/.deps/. The version stamp in the path
# tracks upstream's pinned obs-deps release.
file(GLOB _qt6_dirs LIST_DIRECTORIES true
     "${PULSAR_UPSTREAM_DIR}/.deps/obs-deps-qt6-*-x64")
list(FILTER _qt6_dirs INCLUDE REGEX ".*-x64$")
list(LENGTH _qt6_dirs _qt6_dirs_count)
if(_qt6_dirs_count EQUAL 0)
    message(FATAL_ERROR
        "Qt6 obs-deps not found under ${PULSAR_UPSTREAM_DIR}/.deps/. "
        "Run scripts/build-win.ps1 -Stage configure first.")
endif()
list(GET _qt6_dirs 0 PULSAR_QT6_ROOT)

list(APPEND CMAKE_PREFIX_PATH "${PULSAR_QT6_ROOT}")
find_package(Qt6 REQUIRED COMPONENTS Core Widgets)

add_executable(pulsar-headless
    main.cpp
    log-handler.cpp
    dir-hardening.cpp
    runtime-identity.cpp
)

target_include_directories(pulsar-headless PRIVATE
    "${PULSAR_LIBOBS_HEADERS}"   # obs.h, obs-source.h, ...
    "${PULSAR_LIBOBS_BUILD}"     # generated libobs_EXPORT.h
    "${PULSAR_LIBOBS_CONFIG}"    # generated obsconfig.h
)

target_link_libraries(pulsar-headless PRIVATE
    "${PULSAR_LIBOBS_LIB}"
    pulsar-frontend-stub
    Qt6::Core
    Qt6::Widgets
    "$<$<PLATFORM_ID:Windows>:Advapi32>" # log-handler.cpp / dir-hardening.cpp ACL calls (ADR-005 §3.1, §5 R10)
    "$<$<PLATFORM_ID:Windows>:Ws2_32>"   # runtime-identity.cpp loopback port allocation
)

# /SUBSYSTEM:WINDOWS so Windows does not allocate a console window for
# pulsar.exe when it has no parent terminal. Combined with the manual
# AttachConsole(ATTACH_PARENT_PROCESS) call in main.cpp, the binary
# behaves like:
#   - spawn from Prism / Start-Process / probe                -> no window
#   - direct invocation from cmd.exe / PowerShell terminal    -> output goes
#     to that terminal (operator can still tail logs interactively)
#   - any spawn with stdio:'pipe' / -RedirectStandardOutput   -> CRT picks
#     up the inherited handles, printf writes to the pipe/file as before
#
# /ENTRY:mainCRTStartup keeps `int main(int, char**)` as the entry point
# (the default for /SUBSYSTEM:WINDOWS would be `WinMain`).
target_link_options(pulsar-headless PRIVATE
    "$<$<PLATFORM_ID:Windows>:/SUBSYSTEM:WINDOWS>"
    "$<$<PLATFORM_ID:Windows>:/ENTRY:mainCRTStartup>"
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
