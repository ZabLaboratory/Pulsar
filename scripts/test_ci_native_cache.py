"""Hardware-free invalidation and payload-integrity tests for CI runtime reuse."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("ci_native_cache", Path(__file__).with_name("ci-native-cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class NativeCacheTests(unittest.TestCase):
    def test_every_native_or_unknown_path_invalidates(self):
        for path in (
            "upstream", "patches/new.patch", "VERSION", "CMakeLists.txt",
            "plugins/pulsar-headless/main.cpp", "plugins/new/resource.json",
            "tests/new/CMakeLists.txt", "tests/new/probe.cpp", "scripts/build-win.ps1",
            "scripts/ci-native-cache.py", ".github/workflows/pipeline.yml", "new-input.dat",
        ):
            with self.subTest(path=path):
                before = cache.fingerprint([("blob", "old", path)], {})
                after = cache.fingerprint([("blob", "new", path)], {})
                self.assertNotEqual(before, after)
                self.assertNotEqual(before, cache.fingerprint([], {}))

    def test_reviewed_runtime_and_docs_changes_reuse_native_outputs(self):
        for path in (*cache.RUNTIME_ONLY, "docs/releases/3.0.0.md", "evidence/build/report.md", "README.md"):
            with self.subTest(path=path):
                self.assertEqual(cache.fingerprint([("blob", "old", path)], {}),
                                 cache.fingerprint([("blob", "new", path)], {}))

    def test_toolchain_image_workspace_and_profile_invalidate(self):
        for field in ("ImageOS", "ImageVersion", "VCToolsVersion", "WindowsSDKVersion", "workspace", "profile"):
            with self.subTest(field=field):
                self.assertNotEqual(cache.fingerprint([], {field: "old"}),
                                    cache.fingerprint([], {field: "new"}))

    def test_gitlink_mode_and_order(self):
        entries = [("160000 commit", "abc", "upstream"), ("100644 blob", "def", "VERSION")]
        self.assertEqual(cache.fingerprint(entries, {}), cache.fingerprint(entries[::-1], {}))
        self.assertNotEqual(cache.fingerprint(entries, {}),
                            cache.fingerprint([("100644 blob", "abc", "upstream"), entries[1]], {}))

    def make_payload(self, root):
        for name in (*cache.REQUIRED, "build/tests/nv-probe/Release/probe.exe",
                     "build/tests/data/libobs/default.effect", "build/tests/new/CTestTestfile.cmake"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"native output")

    def test_seal_verify_and_integrity_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_payload(root)
            with patch.object(cache, "git", return_value=b"producer-sha\n"):
                cache.seal(root, "key-a")
            cache.verify(root, "key-a")
            with self.assertRaisesRegex(ValueError, "key mismatch"):
                cache.verify(root, "key-b")
            executable = root / cache.REQUIRED[0]
            executable.write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                cache.verify(root, "key-a")
            executable.unlink()
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                cache.verify(root, "key-a")

    def test_missing_outputs_cannot_be_sealed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Missing required"):
                cache.seal(Path(temporary), "key-a")

    def test_no_runtime_scripts_or_probe_state_in_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_payload(root)
            (root / "scripts").mkdir()
            (root / "scripts/probe-twitch-live.py").write_text("current probe")
            self.assertFalse(any(path.startswith("scripts/") for path in cache.payload_files(root)))


if __name__ == "__main__":
    unittest.main()
