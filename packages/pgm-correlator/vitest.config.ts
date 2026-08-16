import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    // The pgm-extractor tests shell out to a real ffmpeg to build and
    // analyse a real fixture video; generous but bounded.
    testTimeout: 30_000,
  },
});
