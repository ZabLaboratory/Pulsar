"""Static contract for the external upstream build-directory seam."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_BUILD_DEFAULT = '${PULSAR_UPSTREAM_DIR}/build_x64'


def test_root_exposes_external_upstream_build_cache_with_legacy_default():
    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")

    assert 'set(PULSAR_UPSTREAM_BUILD_DIR "${PULSAR_UPSTREAM_DIR}/build_x64" CACHE PATH' in cmake
    assert 'message(STATUS "Pulsar upstream build directory: ${PULSAR_UPSTREAM_BUILD_DIR}")' in cmake


def test_plugins_consume_the_shared_build_directory():
    plugin_files = (
        "pulsar-browser",
        "pulsar-frontend-stub",
        "pulsar-headless",
        "pulsar-multi-stream",
        "pulsar-scene-source",
        "pulsar-websocket",
    )
    for plugin in plugin_files:
        cmake = (ROOT / "plugins" / plugin / "CMakeLists.txt").read_text(encoding="utf-8")
        assert "PULSAR_UPSTREAM_BUILD_DIR" in cmake
        assert UPSTREAM_BUILD_DEFAULT in cmake
        assert '"${PULSAR_UPSTREAM_DIR}/build_x64/' not in cmake


def test_windows_build_script_propagates_override_and_keeps_default():
    script = (ROOT / "scripts" / "build-win.ps1").read_text(encoding="utf-8")

    assert "[string] $UpstreamBuildDir = ''" in script
    assert "$requestedUpstreamBuildDir = $env:PULSAR_UPSTREAM_BUILD_DIR" in script
    assert "$defaultUpstreamBuildDir = Join-Path $upstream 'build_x64'" in script
    assert '"-DPULSAR_UPSTREAM_BUILD_DIR=$upstreamBuildDir"' in script
    assert "cmake --build $upstreamBuildDir" in script
    assert "cmake --build build_x64" not in script
    assert "if (-not $env:PROCESSOR_ARCHITECTURE) {" in script


def test_headless_output_is_resolved_from_external_build_directory():
    script = (ROOT / "scripts" / "build-win.ps1").read_text(encoding="utf-8")

    assert "$pulsarExe = Join-Path $upstreamBuildDir" in script
    assert "rundir\\RelWithDebInfo\\bin\\64bit\\pulsar.exe" in script
