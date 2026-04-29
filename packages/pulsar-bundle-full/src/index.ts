// @clodocapeo/pulsar-bundle-full
//
// Spawns the full Pulsar bundle (with obs-browser / CEF + text + vlc)
// and returns a connected PulsarClient. Identical spawn() API to
// @clodocapeo/pulsar-bundle; the difference is purely the binary
// payload downloaded by postinstall. Use this package when consumers
// need browser sources (HTML overlays), native text sources, or
// VLC-backed media sources -- e.g. Prism's composed scenes.

export { spawn } from "./spawn.js";
export type { SpawnOptions, SpawnedPulsar } from "./spawn.js";
export * from "@clodocapeo/pulsar-client";
