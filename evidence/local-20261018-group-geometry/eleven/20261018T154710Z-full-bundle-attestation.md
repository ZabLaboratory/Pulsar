# Full Windows bundle attestation

The isolated Windows x64 `full` distribution was built and packaged from this
worktree. It carries version stamp `3.0.1` and is available locally at
`D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261018-group-geometry\dist\pulsar-windows-x64-full-v3.0.1`.
The package contains 1,256 files totaling 397,821,398 bytes (379.4 MiB). The
distribution itself is a local build artifact and is not committed. Its sorted
per-file inventory, byte lengths, and SHA-256 values are in
[`20261018T154600Z-full-bundle-files.sha256`](20261018T154600Z-full-bundle-files.sha256);
the inventory file SHA-256 is
`b26a8f9228c23ac2fed408d80ce46e1a16dc664f1deeb288559d983cdc9cf46d`.

The top-level source was Pulsar commit
`25b0c606082abe8177aa47251ea79ab7a5d31ea4` on
`eleven/local-20261018-group-geometry`. `scripts/build-win.ps1 -Full`
completed with exit code 0, then
`scripts/package-win.ps1 -SkipBuild -Variant full` completed with exit code 0.
The packaging validator reported `coherent: true` for
`pulsar.source-telemetry-bundle.v1`, checking all five required OBS source
telemetry exports across seven binaries. The packaged
`pulsar-capture-effects.dll` has the same SHA-256 as the DLL emitted by the
full build directory, confirming that the package contains that build output.

The `upstream` gitlink in the top-level commit resolves to pinned OBS source
`25ed780b7f392a1690dfca929cd57679959cf3ad`. To keep the canonical submodule
checkout untouched, the full build used a standalone clone under this
worktree. The build script applied Pulsar's OBS patch stack there; the patched
checkout ended at `1dd2e8d6d149871e481e1402c46261e94b0ebb93` (parent
`12dd86b82eeacc944978eea5149c0fd604c055cf`) and its final reported status was
` M plugins/obs-browser`. The package source was therefore the patched local
build tree, not the dirty canonical checkout. The standalone clone and build
cache remain local and were not staged.

Key artifact digests:

| File | SHA-256 |
| --- | --- |
| `bin/64bit/pulsar.exe` | `b29d2192465e5c9bf25007caa18cf4713f014b154ddc8f21fd48d79f33c021b6` |
| `obs-plugins/64bit/pulsar-capture-effects.dll` | `dd52e4e06b33567c7816b0524a4a0949ce12252a12a84a74f83e5f743a0a02ae` |
| `.version-stamp` | `3.0.1` |

The native geometry behavior was separately exercised using a trace-enabled
build of this worktree's plugin source in an isolated runtime. Its pixel/model
equality, geometry timings, repeated translations, same-object capture setting
change, structural-filter fallback, and reset evidence are recorded in
[`20261018T154032Z-translation-only-geometry.md`](20261018T154032Z-translation-only-geometry.md)
and the linked JSON receipt. That focused runtime was not the packaged full
distribution. No Live or recording session was started, no shared Prism dev
runtime was modified, and no packaged-runtime end-to-end run was performed.
Frame-skip measurements came from a synthetic GPU scene with noisy idle
counters; see the focused report for the idle-relative comparison and limits.
