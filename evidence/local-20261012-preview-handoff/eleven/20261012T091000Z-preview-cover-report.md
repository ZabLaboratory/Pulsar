# Preview media after physical lane promotion

Continuation in Pulsar/.worktrees/eleven-local-20261008-webm-transition, branch eleven/local-20261008-webm-transition, previous HEAD 8d7eaed0e47241bc66780de683b069b303de85b3. The common Prism first Take revealed BeginPreview requiring the literal ZabPreviewComposite name; the second physical producer therefore could not prepare its media cover.

BeginPreview now validates the actual scene bound to PreviewView, refusing physical lane roots and Program/currentScene aliases before capturing or changing the view. EndPreview already holds and restores previewMediaBase by source reference; this ownership path is retained, without introducing scene lookup, cloning or document reload. README and the existing private native probe describe/test the change.

New RelWithDebInfo pulsar.exe SHA256 f911e5114bf821d11a54789bd8a1b6344a3ff3248a6122261c0d837c55301b63 under build-upstream/rundir/RelWithDebInfo/bin/64bit. Existing paired websocket DLL remains SHA256 6ce95e738caf84b7830efe7b64a156a15e62536304c53600aecc1dde13d28421. Only own build artifacts changed; the upstream gitlink is not staged.

Private native WebM and MP4 probes passed A/B/A three Takes. After the first Take, the named second composite completed BeginPreview/covered/EndPreview/playback/restoration with nonzero frame provenance. After the second Take, the original composite completed cover/abort/restoration. Each restored frame follows its starting frame, Program stayed unchanged through both Preview covers, both browser UUIDs and exactly two document acquisitions stayed unchanged, and stinger configuration remained ready. MP4 also verifies a physical lane root is refused before covering. Processes exited0 with no forced termination. No stream, recording, physical microphone or capture device was used.

Evidence: 20261012T091000Z-preview-cover-{webm,mp4}.json and preview-cover-build.log. The existing synthetic fixture remains a source/ownership regression, not Solar pixel proof or an end-to-end production claim. Coordinator Main1 owns common installation/restart and subsequent Prism/Solar qualification.

Focused regression command: python -m pytest tests/pulsar-media-transition tests/pulsar-transition-controller tests/dual-lane-hot-swap tests/probe-dual-lane tests/probe-take-latency tests/trace-integrity -q. Result196 passed,1 skipped in3.75s; output is preview-cover-regression.log. The skip is not counted as validated. git diff --check passes.
