"""Exact CI runtime reuse, never a cache of test results or checked-out sources.

The default is conservative: every tracked path affects the key except reviewed
documentation and three interpreted probe files. In particular new build inputs,
the upstream gitlink, workflow, native tests and this helper invalidate reuse.
"""

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import subprocess


SCHEMA = 1
MANIFEST = "build/native-cache-manifest.json"
# Keep identical to the pipeline's pulsar-rundir upload and cache paths.
PAYLOAD = (
    "upstream/build_x64/rundir/RelWithDebInfo/**/*",
    "build/**/CTestTestfile.cmake",
    "build/**/*.vcxproj",
    "build/tests/nv-probe/**/*",
    "build/tests/data/libobs/**/*",
)
REQUIRED = (
    "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/pulsar.exe",
    "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit/obs.dll",
    "build/CTestTestfile.cmake",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/pulsar-browser.dll",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/pulsar-browser-page.exe",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/libcef.dll",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/chrome_elf.dll",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/libEGL.dll",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/libGLESv2.dll",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/v8_context_snapshot.bin",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/chrome_100_percent.pak",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/chrome_200_percent.pak",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/icudtl.dat",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/resources.pak",
    "upstream/build_x64/rundir/RelWithDebInfo/obs-plugins/64bit/locales/en-US.pak",
)
RUNTIME_ONLY = frozenset((
    "scripts/probe-twitch-live.py",
    "scripts/test_probe_twitch_live.py",
    "scripts/run-probes.ps1",
))


def is_build_input(path):
    return not (
        path in RUNTIME_ONLY
        or path in {"README.md", "CHANGELOG.md", "CONSUMER-AUDIT.md"}
        or path.startswith(("docs/", "evidence/"))
    )


def fingerprint(entries, environment):
    inputs = sorted(entry for entry in entries if is_build_input(entry[2]))
    document = {"schema": SCHEMA, "inputs": inputs, "environment": environment}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def tree_entries(root):
    entries = []
    for record in git(root, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if record:
            metadata, path = record.decode("utf-8").split("\t", 1)
            mode, kind, oid = metadata.split()
            entries.append((mode + " " + kind, oid, path))
    return entries


def payload_files(root):
    files = set()
    for pattern in PAYLOAD:
        for name in glob.glob(str(root / pattern), recursive=True):
            path = Path(name)
            if path.is_file():
                files.add(path.relative_to(root).as_posix())
    return sorted(files)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def seal(root, key):
    for path in REQUIRED:
        if not (root / path).is_file():
            raise ValueError(f"Missing required native output: {path}")
    manifest = {
        "schema": SCHEMA,
        "key": key,
        "producer_sha": git(root, "rev-parse", "HEAD").decode().strip(),
        "producer_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "files": {path: digest(root / path) for path in payload_files(root)},
    }
    (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify(root, key):
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if manifest["schema"] != SCHEMA or manifest["key"] != key:
        raise ValueError("Native cache schema/key mismatch")
    actual = set(payload_files(root))
    if set(manifest["files"]) != actual or not set(REQUIRED) <= actual:
        raise ValueError("Native cache file inventory mismatch")
    for path, expected in manifest["files"].items():
        if digest(root / path) != expected:
            raise ValueError(f"Native cache digest mismatch: {path}")
    print(f"Verified {len(actual)} native outputs; producer SHA={manifest['producer_sha']} "
          f"run={manifest['producer_run']}; tests must run against the current checkout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("key", "seal", "verify"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--key")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "key":
        # Called before any build/patch mutations. Never fingerprint HEAD while
        # consuming uncommitted build inputs from disk.
        if git(root, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=none"):
            raise ValueError("Fingerprint requires a clean tracked checkout")
        names = ("ImageOS", "ImageVersion", "VCToolsVersion", "WindowsSDKVersion")
        environment = {name: os.environ.get(name, "") for name in names}
        if os.environ.get("GITHUB_ACTIONS") and not all(environment.values()):
            raise ValueError("Missing runner/toolchain identity; refusing ambiguous cache key")
        environment.update(workspace=str(root), profile="Full-RelWithDebInfo-x64")
        key = "pulsar-native-v1-" + fingerprint(tree_entries(root), environment)
        print(key)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"key={key}\n")
    elif not args.key:
        parser.error("--key is required for seal/verify")
    elif args.command == "seal":
        seal(root, args.key)
    else:
        verify(root, args.key)


if __name__ == "__main__":
    main()
