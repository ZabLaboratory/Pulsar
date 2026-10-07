"""Exercise the real OBS dependency helper with a local hash-checked archive."""
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CefProvisioning(unittest.TestCase):
    def run_case(self, required, corrupt_hash=False):
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cef-proof-", dir=cache) as name:
            folder = pathlib.Path(name)
            source = folder / "source"
            source.mkdir()
            archive = source / "cef_binary_test_windows_x64.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Release/libcef.lib", "fixture import library")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata = json.dumps({"cef": {
                "version": "test", "hashes": {"windows-x64": "0" * 64 if corrupt_hash else digest},
                "baseUrl": source.as_uri(), "label": "CEF fixture",
            }})
            helper = (ROOT / "upstream/cmake/common/buildspec_common.cmake").as_posix()
            script = folder / "test.cmake"
            script.write_text(f'''
cmake_minimum_required(VERSION 3.28)
include("{helper}")
function(_get_dependency_data output)
 set(${{output}} [==[{metadata}]==] PARENT_SCOPE)
endfunction()
set(ENABLE_BROWSER OFF)
set(PULSAR_REQUIRE_CEF {"ON" if required else "OFF"})
set(arch x64)
set(platform windows-x64)
set(dependencies_dir "{folder.as_posix()}/deps")
file(MAKE_DIRECTORY "${{dependencies_dir}}")
set(cef_filename "cef_binary_VERSION_windows_ARCH_REVISION.zip")
set(cef_destination "cef_binary_VERSION_windows_ARCH")
_check_dependencies(cef)
if(PULSAR_REQUIRE_CEF AND NOT EXISTS "${{CEF_ROOT_DIR}}/Release/libcef.lib")
 message(FATAL_ERROR "Full runtime did not provision CEF")
endif()
if(NOT PULSAR_REQUIRE_CEF AND EXISTS "${{dependencies_dir}}/cef_binary_test_windows_x64")
 message(FATAL_ERROR "Light runtime provisioned unwanted CEF")
endif()
''', encoding="utf-8")
            result = subprocess.run(["cmake", "-P", str(script)], capture_output=True, text=True)
            return result

    def test_full_headless_provisions_cef(self):
        result = self.run_case(True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_light_headless_skips_cef(self):
        result = self.run_case(False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_archive_hash_is_rejected(self):
        self.assertNotEqual(self.run_case(True, corrupt_hash=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
