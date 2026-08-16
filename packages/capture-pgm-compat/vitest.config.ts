import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    // frame-health.test.ts builds real ffmpeg fixtures; live-capture-compat
    // spawns a real full Pulsar + CEF and records real video -- generous
    // but bounded. See README.md for the PULSAR_LIVE_CAPTURE_COMPAT env
    // gate that keeps the latter opt-in.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
