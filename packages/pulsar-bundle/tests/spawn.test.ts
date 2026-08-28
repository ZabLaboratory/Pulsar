import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
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

  // RC4 (ADR-005 §3.3): the fixture now emits "PULSAR_SESSION <id>" as an
  // intercalary line right before the ready marker (matching main.cpp's
  // real ordering). spawn() must reach ready without touching the watchdog
  // -- it only ever inspects the idle/ready line, never this one.
  it("reaches ready despite the PULSAR_SESSION line preceding the sentinel", async () => {
    const lines: string[] = [];
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
      readyTimeoutMs: 5_000,
      onLog: (_stream, line) => lines.push(line),
    });

    const sessionIdx = lines.findIndex((l) => l.startsWith("PULSAR_SESSION "));
    const readyIdx = lines.findIndex((l) => l.includes("ready, idling"));
    expect(sessionIdx).toBeGreaterThanOrEqual(0);
    expect(readyIdx).toBeGreaterThan(sessionIdx);
    expect(handle.client.isConnected()).toBe(true);

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

  it("gives concurrent children distinct runtime identities and config namespaces", async () => {
    const handles = await Promise.all(
      Array.from({ length: 4 }, () =>
        spawn({
          binariesPath: tmp,
          launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
        }),
      ),
    );

    expect(new Set(handles.map((handle) => handle.runtimeInstanceId)).size).toBe(4);
    expect(new Set(handles.map((handle) => handle.runtimeDir)).size).toBe(4);
    expect(handles.every((handle) => handle.port > 0)).toBe(true);

    const generatedRuntimeDirs = handles.map((handle) => handle.runtimeDir);
    await Promise.all(handles.map((handle) => handle.shutdown()));
    expect(generatedRuntimeDirs.every((dir) => !existsSync(dir))).toBe(true);
  });

  it("rejects an invalid runtime identity before starting a child", async () => {
    const lines: string[] = [];
    await expect(
      spawn({
        binariesPath: tmp,
        env: { PULSAR_RUNTIME_INSTANCE_ID: "../escape" },
        launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
        onLog: (_stream, line) => lines.push(line),
      }),
    ).rejects.toMatchObject({
      name: "PulsarRuntimeError",
      prism: { code: "PULSAR_RUNTIME_ID_INVALID" },
    });
    expect(lines).toHaveLength(0);
  });

  it("does not remove a caller-owned runtime directory", async () => {
    const runtimeDir = join(tmp, "caller-runtime");
    const handle = await spawn({
      binariesPath: tmp,
      env: { PULSAR_RUNTIME_DIR: runtimeDir },
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
    });

    expect(handle.runtimeDir).toBe(resolve(runtimeDir));
    await handle.shutdown();
    expect(existsSync(runtimeDir)).toBe(true);
  });

  it("cleans a generated namespace when boot times out", async () => {
    const runtimeRoot = join(tmp, "runtime-root");
    await expect(
      spawn({
        binariesPath: tmp,
        env: { PULSAR_RUNTIME_ROOT: runtimeRoot },
        launchCommand: { exe: process.execPath, args: ["-e", "setInterval(() => {}, 1000)" ] },
        readyTimeoutMs: 100,
      }),
    ).rejects.toMatchObject({
      name: "PulsarRuntimeError",
      prism: { code: "PULSAR_READY_TIMEOUT" },
    });
    expect(readdirSync(runtimeRoot)).toHaveLength(0);
  });
});
