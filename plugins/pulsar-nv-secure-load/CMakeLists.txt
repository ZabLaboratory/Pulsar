# pulsar-nv-secure-load -- header-only, no DLL of its own.
#
# It is a plugins/ entry rather than a stray include/ directory because its
# three consumers live in three different builds (upstream's nv-filters via
# patches/0003, pulsar-multi-stream, and tests/nv-probe) and an INTERFACE
# target is the only thing CMake lets all three name identically.
#
# The three system libraries are PART OF THE CONTRACT, not implementation
# details:
#   version  -- SDK versions are read with GetFileVersionInfo* off an
#               absolute path, so the probe answers before anything loads.
#   shell32  -- SHGetKnownFolderPath(FOLDERID_ProgramFiles) resolves the VFX
#               fallback root FROM THE SYSTEM. Amendment 3 §A3.4 rules an
#               environment read out of path resolution, and %ProgramFiles%
#               is chosen by whoever spawned the process.
#   ole32    -- CoTaskMemFree, for what SHGetKnownFolderPath allocates.
#
# NOTE: upstream/plugins/nv-filters/ cannot link this target -- it is built
# by upstream's own CMake project, not this one -- so patches/0003 adds the
# include directory by relative path and links version.lib itself. Any move
# of this directory has to be mirrored there in the same commit.

cmake_minimum_required(VERSION 3.28)

add_library(pulsar-nv-secure-load INTERFACE)
add_library(Pulsar::nv-secure-load ALIAS pulsar-nv-secure-load)

target_include_directories(pulsar-nv-secure-load INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}")

if(WIN32)
    target_link_libraries(pulsar-nv-secure-load INTERFACE version shell32 ole32)
endif()
