# nv-probe -- the CI gate for issue #167 (Prism ADR 023 Amendment 3).
#
# Deliberately built WITHOUT libobs: what is under test is a loader
# property, so dragging an OBS process into it would make the gate slower,
# flakier and no more conclusive. It runs anywhere Windows runs -- no GPU,
# no NVIDIA SDK -- which is exactly why criterion 3 (first level) can gate
# the merge while criterion 3 bis cannot.
#
# Four fixture DLLs, in two pairs:
#
#   good / evil SDK DLL  -- differ by an exported marker AND by their
#                           FILEVERSION resource (1.6.1.2 vs 0.0.0.1), so
#                           one fixture serves both the "which copy loaded"
#                           question and the version probe.
#   good / evil dep DLL  -- both named PulsarNvFakeDep.dll, statically
#                           imported by the SDK DLLs. Same question, one
#                           level down: which copy did the IMPORT resolve
#                           to. Hence the two output directories -- two
#                           files cannot share a name in one.
#
# Static CRT throughout: a vcruntime140.dll dependency would add a third
# search to a test about search order.

cmake_minimum_required(VERSION 3.28)

if(NOT WIN32)
    return()
endif()

set(PULSAR_NV_FIXTURE_CONFIGS Debug Release RelWithDebInfo MinSizeRel)

# Both dep DLLs must carry the SAME file name, or the import descriptor
# baked into the SDK DLLs would differ between them and the test would
# prove nothing. Distinct output directories are how CMake tolerates that.
function(pulsar_nv_add_dep target marker subdir)
    add_library(${target} SHARED fake-nv-dep.c)
    target_compile_definitions(${target} PRIVATE PULSAR_NV_FAKE_DEP_MARKER=${marker})
    set_target_properties(${target} PROPERTIES
        OUTPUT_NAME "PulsarNvFakeDep"
        MSVC_RUNTIME_LIBRARY "MultiThreaded"
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
        ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
    )
    foreach(cfg IN LISTS PULSAR_NV_FIXTURE_CONFIGS)
        string(TOUPPER "${cfg}" cfg_upper)
        set_target_properties(${target} PROPERTIES
            RUNTIME_OUTPUT_DIRECTORY_${cfg_upper} "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
            ARCHIVE_OUTPUT_DIRECTORY_${cfg_upper} "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
        )
    endforeach()
endfunction()

function(pulsar_nv_add_sdk target marker version_comma version_dot dep subdir)
    set(PULSAR_NV_FAKE_VER_COMMA "${version_comma}")
    set(PULSAR_NV_FAKE_VER_DOT "${version_dot}")
    set(rc "${CMAKE_CURRENT_BINARY_DIR}/${target}.rc")
    configure_file(fake-nv-sdk.rc.in "${rc}" @ONLY)

    add_library(${target} SHARED fake-nv-sdk.c "${rc}")
    target_compile_definitions(${target} PRIVATE PULSAR_NV_FAKE_MARKER=${marker})
    target_link_libraries(${target} PRIVATE ${dep})
    set_target_properties(${target} PROPERTIES
        MSVC_RUNTIME_LIBRARY "MultiThreaded"
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
        ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
    )
    foreach(cfg IN LISTS PULSAR_NV_FIXTURE_CONFIGS)
        string(TOUPPER "${cfg}" cfg_upper)
        set_target_properties(${target} PROPERTIES
            RUNTIME_OUTPUT_DIRECTORY_${cfg_upper} "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
            ARCHIVE_OUTPUT_DIRECTORY_${cfg_upper} "${CMAKE_CURRENT_BINARY_DIR}/${subdir}"
        )
    endforeach()
endfunction()

pulsar_nv_add_dep(pulsar-nv-fake-dep-good 11 fixtures-good)
pulsar_nv_add_dep(pulsar-nv-fake-dep-evil 12 fixtures-evil)

pulsar_nv_add_sdk(pulsar-nv-fake-sdk-good 1 "1,6,1,2" "1.6.1.2" pulsar-nv-fake-dep-good fixtures-good)
pulsar_nv_add_sdk(pulsar-nv-fake-sdk-evil 2 "0,0,0,1" "0.0.0.1" pulsar-nv-fake-dep-evil fixtures-evil)

add_executable(nv-probe-test nv-probe-test.c)
# advapi32: the fixture names its deny principal by the token's own user SID
# (ConvertSidToStringSid) rather than by account name, which icacls cannot
# always resolve.
target_link_libraries(nv-probe-test PRIVATE Pulsar::nv-secure-load advapi32)
add_dependencies(nv-probe-test
    pulsar-nv-fake-sdk-good pulsar-nv-fake-sdk-evil
    pulsar-nv-fake-dep-good pulsar-nv-fake-dep-evil
)

# Fixture paths reach the test as arguments rather than as a generated
# header: generator expressions resolve per-config, so one add_test entry
# stays correct under both single- and multi-config generators.
add_test(
    NAME    pulsar-nv-probe
    COMMAND nv-probe-test
            "$<TARGET_FILE:pulsar-nv-fake-sdk-good>"
            "$<TARGET_FILE:pulsar-nv-fake-sdk-evil>"
            "$<TARGET_FILE:pulsar-nv-fake-dep-good>"
            "$<TARGET_FILE:pulsar-nv-fake-dep-evil>"
)
set_tests_properties(pulsar-nv-probe PROPERTIES
    TIMEOUT 120
    LABELS "security;unit"
)
