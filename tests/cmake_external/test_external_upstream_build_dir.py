"""Static contract for the external upstream build-directory seam."""

from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest


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


@pytest.mark.parametrize("browser", ["ON", "OFF"])
@pytest.mark.parametrize("frontend", ["ON", "OFF"])
def test_fast_cache_gate_executes_with_preserved_browser_capability(tmp_path, browser, frontend):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell required for the actual Windows build-cache gate")
    script = (ROOT / "scripts" / "build-win.ps1").read_text(encoding="utf-8")
    gate = script[script.index("$upstreamCache =") : script.index("if ($Stage -in @('configure', 'all') -and -not $reuseFastUpstreamConfigure)")]
    (tmp_path / "CMakeCache.txt").write_text(
        f"ENABLE_FRONTEND:BOOL={frontend}\nENABLE_UI:BOOL=OFF\n"
        f"ENABLE_BROWSER:BOOL={browser}\nENABLE_WEBSOCKET:BOOL=OFF\n", encoding="utf-8"
    )
    command = (
        "$ErrorActionPreference='Stop'; $Fast=$true; [switch]$Full=$false; "
        "$upstreamBuildDir=$env:PULSAR_TEST_CACHE; " + gate +
        "\n@{full=[bool]$Full;reused=$reuseFastUpstreamConfigure} | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", command], capture_output=True, text=True,
        env={**os.environ, "PULSAR_TEST_CACHE": str(tmp_path)}, timeout=30,
    )
    if frontend == "ON":
        assert result.returncode != 0
        assert "compatible headless" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"full": browser == "ON", "reused": True}
