cmake_minimum_required(VERSION 3.28)

project(Pulsar
    VERSION 0.0.0
    DESCRIPTION "Headless broadcast engine -- fork of OBS Studio for embedding"
    LANGUAGES C CXX
)

# Pulsar's top-level project. This file does NOT build upstream/
# itself; that happens via upstream's own CMakePresets.json driven
# by scripts/build-win.ps1. This file is invoked AFTER upstream's
# build_x64/ exists and links Pulsar-owned plugins against the
# libobs that upstream produced.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(PULSAR_BUILD_HEADLESS  "Build the pulsar-headless service exe"          ON)
option(PULSAR_BUILD_WEBSOCKET "Build the pulsar-websocket plugin (Phase 4)"    OFF)
option(PULSAR_BUILD_MULTISTREAM "Build the pulsar-multi-stream plugin (Phase 4)" OFF)

if(PULSAR_BUILD_HEADLESS)
    add_subdirectory(plugins/pulsar-headless)
endif()

if(PULSAR_BUILD_WEBSOCKET)
    add_subdirectory(plugins/pulsar-websocket)
endif()

if(PULSAR_BUILD_MULTISTREAM)
    add_subdirectory(plugins/pulsar-multi-stream)
endif()
