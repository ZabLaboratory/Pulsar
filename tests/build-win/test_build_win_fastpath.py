"""Contract checks for the global incremental Windows build fastpath."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "build-win.ps1"


def test_patched_checkout_reuse_is_exact_and_fail_closed() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "Get-PatchSetFingerprint" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "pinned_revision = $PinnedRevision" in script
    assert "applied_head = $head" in script
    assert "$head -ne $state.applied_head" in script
    assert "git -C $Repository diff --quiet HEAD" in script
    assert "git -C $Repository diff --cached --quiet HEAD" in script
    assert "reusing exact patched upstream HEAD" in script
    assert "reusing exact patched obs-browser HEAD" in script
    assert "Get-TextFingerprint" in script
    assert ".pulsar-config-state.json" in script
    assert "Reusing exact Pulsar CMake configuration" in script
    assert '"upstream_head=' not in script


def test_refresh_and_full_build_semantics_are_preserved() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "[switch] $RefreshPatches" in script
    assert "if ($RefreshPatches" in script
    assert "git reset --hard $recordedSha" in script
    assert "git am --keep-non-patch" in script
    assert "cmake --build --preset $preset --config RelWithDebInfo --parallel" in script
    assert "cmake --build $pulsarBuild --config RelWithDebInfo --parallel" in script
    assert "PULSAR_BUILD_BROWSER=$(if ($Full) { 'ON' } else { 'OFF' })" in script


def test_stripped_windows_environment_keeps_full_qt_graph() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "-not $env:PROCESSOR_ARCHITECTURE -and ($Full -or $GuiBuild)" in script
    assert "CMakePresets.json" in script
    assert "-DQT_HOST_PATH=$qtHostPath" in script
