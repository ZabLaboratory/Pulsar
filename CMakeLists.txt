cmake_minimum_required(VERSION 3.28)

project(Pulsar
    VERSION 0.0.0
    DESCRIPTION "Headless broadcast engine — fork of OBS Studio for embedding"
    LANGUAGES C CXX
)

# Pulsar is a fork of obs-studio kept in upstream/ as a git submodule.
# Top-level configuration is intentionally minimal at this stage —
# Phase 1 of the build pipeline (see docs/DEVELOPMENT.md) wires up:
#   - upstream/ submodule init + patches/ application
#   - libobs and core obs-studio plugins
#   - Pulsar-owned plugins under plugins/
#   - service-mode entry point (no Qt)
#   - per-platform packaging
#
# Until then, this file declares the project metadata and reserves the
# subdirectories so editors and CMake-aware tooling can navigate the
# tree without errors.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(PULSAR_BUILD_PLUGINS "Build Pulsar-owned plugins under plugins/" OFF)
option(PULSAR_BUILD_UPSTREAM "Build vendored obs-studio from upstream/" OFF)

if(PULSAR_BUILD_UPSTREAM)
    message(STATUS "Pulsar: upstream build path not yet implemented (Phase 1)")
endif()

if(PULSAR_BUILD_PLUGINS)
    message(STATUS "Pulsar: plugin build path not yet implemented (Phase 1)")
    # add_subdirectory(plugins/pulsar-headless)
    # add_subdirectory(plugins/pulsar-websocket)
    # add_subdirectory(plugins/pulsar-multi-stream)
endif()
