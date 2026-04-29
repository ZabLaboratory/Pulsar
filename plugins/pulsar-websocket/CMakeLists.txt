# pulsar-websocket -- vendor fork of obsproject/obs-websocket.
#
# Phase 4c: source vendored, Qt UI / frontend-api migration calls
# stripped, CMakeLists.txt build wiring is the Phase 4d deliverable.
# The build pipeline does not yet compile this plugin -- the
# top-level Pulsar CMakeLists.txt option PULSAR_BUILD_WEBSOCKET
# defaults to OFF.
#
# Phase 4d will:
#   - locate the vendored deps (Websocketpp, Asio, nlohmann_json,
#     qrcodegencpp) under upstream/.deps/obs-deps-*-x64/
#   - link against OBS::libobs from upstream's build_x64/libobs/
#   - link Qt6::Core + Qt6::Network only (no Widgets / Gui / Svg
#     -- the source no longer pulls them in)
#   - declare the source list (mirrors upstream's CMakeLists.txt
#     minus src/forms/*)
#   - produce obs-websocket.dll alongside the other plugins so
#     libobs's default search path picks it up

cmake_minimum_required(VERSION 3.28)
project(pulsar-websocket LANGUAGES CXX)

if(PULSAR_BUILD_WEBSOCKET)
    message(FATAL_ERROR
        "pulsar-websocket build not yet wired (Phase 4d). The vendored\n"
        "source is in plugins/pulsar-websocket/src/ but the CMake build\n"
        "rules are not. Set PULSAR_BUILD_WEBSOCKET=OFF for now.")
else()
    message(STATUS "pulsar-websocket: source vendored, build wiring is Phase 4d")
endif()
