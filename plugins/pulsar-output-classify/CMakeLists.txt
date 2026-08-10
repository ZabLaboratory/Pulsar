# pulsar-output-classify -- header-only, no DLL of its own.
#
# Same rationale as pulsar-nv-secure-load (see that CMakeLists.txt): its two
# consumers, pulsar-frontend-stub and pulsar-multi-stream, live in two
# different builds, and an INTERFACE target is the only thing CMake lets both
# name identically without one depending on the other's translation unit.
# ADR-005 §3.4 / issue #182.

cmake_minimum_required(VERSION 3.28)

add_library(pulsar-output-classify INTERFACE)
add_library(Pulsar::output-classify ALIAS pulsar-output-classify)

target_include_directories(pulsar-output-classify INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}")
