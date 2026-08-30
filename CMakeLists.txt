cmake_minimum_required(VERSION 3.28)

# Source of truth for the version: top-level VERSION file. The packaging
# script + GitHub release workflow + npm package versions all read from
# the same place. Bumped manually on each release.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/VERSION" PULSAR_VERSION_RAW)
string(STRIP "${PULSAR_VERSION_RAW}" PULSAR_VERSION)

project(Pulsar
    VERSION "${PULSAR_VERSION}"
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

option(PULSAR_BUILD_HEADLESS      "Build the pulsar-headless service exe"          ON)
option(PULSAR_BUILD_FRONTEND_STUB "Build the pulsar-frontend-stub static library"  ON)
option(PULSAR_BUILD_WEBSOCKET     "Build the pulsar-websocket plugin"              ON)
option(PULSAR_BUILD_MULTISTREAM   "Build the pulsar-multi-stream plugin"            ON)
option(PULSAR_BUILD_SCENE_SOURCE  "Build the pulsar-scene-source plugin"            ON)
option(PULSAR_BUILD_BROWSER       "Build the pulsar-browser plugin (forked CEF)"    ON)

# Header-only, no build of its own, and needed by pulsar-multi-stream and
# by tests/nv-probe -- so it is added before both. (upstream's nv-filters
# consumes the same header by relative include path, from its own CMake
# project; see patches/0003.)
add_subdirectory(plugins/pulsar-nv-secure-load)

# Header-only, no build of its own -- the pulsar:OutputFailed reason_class
# classifier (ADR-005 §3.4, issue #182), needed identically by
# pulsar-frontend-stub and pulsar-multi-stream and by
# tests/output-classify-probe, so it is added before all three.
add_subdirectory(plugins/pulsar-output-classify)

# Order matters: pulsar-headless links pulsar-frontend-stub, so the
# subdirectory exposing that target must be added first.
if(PULSAR_BUILD_FRONTEND_STUB)
    add_subdirectory(plugins/pulsar-frontend-stub)
endif()

if(PULSAR_BUILD_HEADLESS)
    add_subdirectory(plugins/pulsar-headless)
endif()

if(PULSAR_BUILD_WEBSOCKET)
    add_subdirectory(plugins/pulsar-websocket)
endif()

if(PULSAR_BUILD_MULTISTREAM)
    add_subdirectory(plugins/pulsar-multi-stream)
endif()

if(PULSAR_BUILD_SCENE_SOURCE)
    add_subdirectory(plugins/pulsar-scene-source)
endif()

if(PULSAR_BUILD_BROWSER)
    add_subdirectory(plugins/pulsar-browser)
endif()

# ----------------------------------------------------------------------------
# CTest harness around the offline probes.
#
# The probes (scripts/probe-*.py) live OUTSIDE the build tree because they
# are integration tests, not unit tests -- they need a running pulsar.exe
# + obs-websocket on a real loopback socket. Hence a single CTest entry
# that delegates to scripts/run-probes.ps1, which spawns pulsar, waits
# for the PULSAR_READY sentinel, runs every probe sequentially, then
# stops the service. Local: `cmake --build build && ctest --test-dir build
# --output-on-failure`. CI: same, wired into build.yml.
#
# probe-twitch-live.py is intentionally NOT run from here -- it needs a
# real Twitch stream key + 5 min of bandwidth, lives in live-test.yml.
# ----------------------------------------------------------------------------
enable_testing()

if(WIN32)
    add_test(
        NAME    pulsar-offline-probes
        COMMAND powershell -NoProfile -ExecutionPolicy Bypass
                -File "${CMAKE_CURRENT_SOURCE_DIR}/scripts/run-probes.ps1"
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    )
    set_tests_properties(pulsar-offline-probes PROPERTIES
        # Must stay BELOW the CI wrapper's timeout_minutes (pipeline.yml,
        # nick-fields/retry around `ctest`, currently 300s). A passing run
        # takes ~122s (measured, run 31384751144); a real hang otherwise
        # gets externally SIGKILLed by the CI wrapper before CTest's own
        # timeout handler can fire, so `--output-on-failure` never prints
        # the buffered stdout capture -- a hang and a slow pass look
        # identical (zero output). 240s leaves CTest a ~60s margin to
        # win that race and report the exact phase it was in.
        TIMEOUT 240
        LABELS "integration"
    )

    # #167 -- DLL search-order gate for the NVIDIA SDK loads. Unlike the
    # probes above it needs no pulsar.exe, no GPU and no NVIDIA SDK: it
    # tests a loader property with its own fixture DLLs, which is what
    # lets criterion 3 (first level) gate the merge.
    add_subdirectory(tests/nv-probe)

    # #177 -- fail-closed regression for obs-websocket's IsDebugEnabled().
    # No pulsar.exe, no libobs/Qt: a boolean-predicate mirror is enough to
    # gate the merge (see tests/ws-debug-probe/ws-debug-probe-test.cpp).
    add_subdirectory(tests/ws-debug-probe)

    # #183 -- ADR-005 §3.1-§3.2 log handler: formatting + two-layer
    # redaction + rotation/ACL, built from the real
    # plugins/pulsar-headless/log-handler.cpp translation unit (see
    # tests/log-handler-probe/log-handler-probe-test.cpp).
    add_subdirectory(tests/log-handler-probe)

    # #201 -- ADR-005 §5 R10 (Amendment 1) directory/temp-file hardening:
    # atomic protected-DACL creation, reparse-point refusal, owner
    # verification, no-reopen temp file handle, orphaned .tmp sweep --
    # built from the real plugins/pulsar-headless/dir-hardening.cpp
    # translation unit (see tests/dir-hardening-probe/dir-hardening-probe-test.cpp).
    add_subdirectory(tests/dir-hardening-probe)

    # #243 -- runtime identity, crash-safe instance lock and the singleton
    # DirectShow legacy-alias lease. This links the production utility rather
    # than a test mirror and exercises four concurrent namespaces, canonical
    # cross-root/physical-directory authorities, alias hardening, and
    # acquire/refusal/release/abandoned-recovery of both lock classes.
    add_subdirectory(tests/runtime-isolation-probe)

    # #248 -- the producer (C) and DirectShow consumer (C++) must take the
    # same fail-closed namespace decision before either can touch a named
    # shared-memory queue.  This probe includes the production C-compatible
    # helper after the lexical upstream patch series has been applied.
    add_subdirectory(tests/directshow-namespace-probe)

    # #182 -- regression gate for the pulsar:OutputFailed reason_class
    # classifier (ADR-005 §3.4). No pulsar.exe, no libobs/Qt: a pure
    # function of (is_local_output, code, last_error).
    add_subdirectory(tests/output-classify-probe)

    # #186 -- regression gate for the pulsar:OutputAttemptSettled
    # outcome/reason_class-absence contract (ADR-005 §3.5). No pulsar.exe,
    # no libobs/Qt: the went-active state machine itself is exercised by
    # scripts/probe-multi-stream.py instead (see the probe's own header).
    add_subdirectory(tests/output-attempt-settled-probe)

    # #249 -- the rollback trigger is parsed by the same header-only helper
    # used by the frontend, so whitespace/sign/zero/overflow values cannot
    # silently arm or disarm a drill. This native truth table needs no OBS,
    # GPU, or running Pulsar process.
    add_subdirectory(tests/dual-lane-config)

    # #249 -- admission, in-flight callback accounting, and CEF close
    # finalization share one linearization point. This native test forces the
    # close/admission interleaving without requiring OBS or a running CEF.
    add_subdirectory(tests/pulsar-headless-shutdown)

    # #249 -- /SUBSYSTEM:WINDOWS Pulsar is spawned with stdout redirected to
    # a pipe by the real probe. Prove its anonymous inherited event control
    # path releases both runtime leases without falling back to kill().
    if(TARGET pulsar-headless)
        add_test(
            NAME pulsar-headless-graceful-shutdown
            # Resolve Python through the runner PATH at test execution time.
            # The CI job installs websockets after setup-python; baking the
            # configure-time interpreter here can select a different Python
            # minor with no probe dependencies.
            COMMAND python
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/pulsar-headless-shutdown/test_graceful_shutdown.py"
                    "$<TARGET_FILE:pulsar-headless>"
            WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
        )
        set_tests_properties(pulsar-headless-graceful-shutdown PROPERTIES
            TIMEOUT 120
            LABELS "integration;runtime"
        )
    endif()
endif()
