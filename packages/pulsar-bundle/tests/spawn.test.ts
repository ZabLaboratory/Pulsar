import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { spawn } from "../src/spawn.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const FAKE_PULSAR = resolve(__dirname, "fake-pulsar.mjs");

describe("spawn()", () => {
  let tmp: string;

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "pulsar-bundle-"));
    // The spawn() helper expects <binariesPath>/bin/64bit/ to exist as
    // the cwd. Create it but leave pulsar.exe absent -- launchCommand
    // overrides the executable lookup.
    mkdirSync(join(tmp, "bin", "64bit"), { recursive: true });
  });

  afterEach(() => {
    try {
      rmSync(tmp, { recursive: true, force: true });
    } catch {
      // tmp already gone or held by a stray child -- ignore.
    }
  });

  it("connects after the fake pulsar prints the ready marker", async () => {
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
    });

    expect(handle.libobsVersion).toBe("32.1.2-fake");
    expect(handle.port).toBeGreaterThan(0);
    expect(handle.client.isConnected()).toBe(true);

    await handle.shutdown();
    expect(handle.client.isConnected()).toBe(false);
  });

  it("rejects when the executable does not exist (no launchCommand)", async () => {
    await expect(spawn({ binariesPath: tmp })).rejects.toThrow(/pulsar\.exe not found/);
  });

  it("rejects when the executable exits before the ready marker", async () => {
    // A node command that exits immediately won't print the marker.
    await expect(
      spawn({
        binariesPath: tmp,
        launchCommand: { exe: process.execPath, args: ["-e", "process.exit(0)"] },
        readyTimeoutMs: 2_000,
      }),
    ).rejects.toThrow(/exited prematurely|did not signal ready/);
  });

  it("forwards stdout/stderr lines via onLog", async () => {
    const lines: string[] = [];
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
      onLog: (_stream, line) => lines.push(line),
    });
    expect(lines.some((l) => l.includes("ready, idling"))).toBe(true);
    await handle.shutdown();
  });

  it("shutdown is idempotent", async () => {
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
    });
    await handle.shutdown();
    await handle.shutdown();
    expect(handle.client.isConnected()).toBe(false);
  });
});
