// @zablaboratory/pulsar-bundle
//
// Spawns the bundled pulsar.exe and returns a connected PulsarClient.
// Re-exports the client surface so callers don't need a second
// dependency line.

export { spawn } from "./spawn.js";
export type { SpawnOptions, SpawnedPulsar } from "./spawn.js";
export * from "@zablaboratory/pulsar-client";
