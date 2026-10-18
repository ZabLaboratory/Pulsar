# Native camera effects

`camera-effects.cpp` registers `pulsar_vignette_filter`: a GPU vignette applied to
the native source, shared by Preview and Program. It darkens RGB towards the
edges and preserves source alpha. This is a graphic effect, without an AI model.

Settings: `amount` 0–1, `radius` 0.1–1.5, `softness` 0.01–1. Defaults are
0.35, 0.65 and 0.5. Amount zero and native disable bypass the effect. Parameters
are finite/clamped in native code; Prism validates before sending them.
The shader is embedded. No file/shader path, CPU readback or additional capture
producer is introduced. Effect allocation/destruction stays on the graphics thread.

The top-level CMake build includes this module in both distribution variants.
Standalone builds accept libobs source-header and build-directory paths. Packaging
uses the existing plugin-directory staging. The capability inventory reports the
registration; hosts must gate the controls on that inventory.

Regression: `scripts/probe-camera-effects.py` exercises actual native pixels,
transparency, parameter updates, stack/bypass/removal and graceful shutdown without
physical camera, broadcasting or recording.
