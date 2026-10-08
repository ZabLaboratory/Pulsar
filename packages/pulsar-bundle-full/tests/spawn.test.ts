import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { once } from "node:events";
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

  it.each([0, 7])("reports an unsolicited exit with code %i before the ready marker", async (exitCode) => {
    const events: Array<{ code: string; severity: string }> = [];
    // A node command that exits immediately won't print the marker.
    await expect(
      spawn({
        binariesPath: tmp,
        launchCommand: { exe: process.execPath, args: ["-e", `process.exit(${exitCode})`] },
        onPrismLog: (event) => events.push(event),
        readyTimeoutMs: 2_000,
      }),
    ).rejects.toThrow(/exited prematurely|did not signal ready/);
    expect(events.filter((event) => event.severity === "error").map((event) => event.code))
      .toEqual(["PULSAR_PROCESS_EXITED"]);
    expect(events.some((event) => event.code === "PULSAR_PROCESS_STOPPED")).toBe(false);
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

  it("redacts ready credentials from every user-facing log callback", async () => {
    const lines: string[] = [];
    const prismMessages: string[] = [];
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
      onLog: (_stream, line) => lines.push(line),
      onPrismLog: (event) => prismMessages.push(event.message),
    });

    expect(lines.some((line) => line.includes("PULSAR_READY"))).toBe(true);
    expect([...lines, ...prismMessages].some((line) => line.includes("fake-ready-password"))).toBe(false);
    expect([...lines, ...prismMessages].some((line) => line.includes("password=[redacted]"))).toBe(true);
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

  it("reports concurrent requested shutdown as one informational stop, not a crash", async () => {
    const events: Array<{ code: string; severity: string }> = [];
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
      onPrismLog: (event) => events.push(event),
    });
    await Promise.all([handle.shutdown(), handle.shutdown()]);
    expect(events.filter((event) => event.code === "PULSAR_PROCESS_STOPPED"))
      .toEqual([expect.objectContaining({ severity: "info", context: { action: "shutdown" },
        details: expect.objectContaining({ reason: "shutdown" }) })]);
    expect(events.filter((event) => event.severity === "error")).toEqual([]);
  });

  it("still reports an external SIGTERM after readiness as an unexpected exit", async () => {
    const events: Array<{ code: string; severity: string }> = [];
    const handle = await spawn({
      binariesPath: tmp,
      launchCommand: { exe: process.execPath, args: [FAKE_PULSAR] },
      onPrismLog: (event) => events.push(event),
    });
    try {
      const exited = once(handle.child, "exit");
      expect(handle.child.kill("SIGTERM")).toBe(true);
      await exited;
      expect(events.filter((event) => event.code === "PULSAR_PROCESS_EXITED"))
        .toEqual([expect.objectContaining({ severity: "error" })]);
      expect(events.some((event) => event.code === "PULSAR_PROCESS_STOPPED")).toBe(false);
    } finally {
      await handle.shutdown();
    }
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
    const events: Array<{ code: string; severity: string }> = [];
    await expect(
      spawn({
        binariesPath: tmp,
        env: { PULSAR_RUNTIME_ROOT: runtimeRoot },
        launchCommand: { exe: process.execPath, args: ["-e", "setInterval(() => {}, 1000)" ] },
        readyTimeoutMs: 100,
        onPrismLog: (event) => events.push(event),
      }),
    ).rejects.toMatchObject({
      name: "PulsarRuntimeError",
      prism: { code: "PULSAR_READY_TIMEOUT" },
    });
    expect(readdirSync(runtimeRoot)).toHaveLength(0);
    expect(events.filter((event) => event.severity === "error").map((event) => event.code))
      .toEqual(["PULSAR_READY_TIMEOUT"]);
    expect(events.filter((event) => event.code === "PULSAR_PROCESS_STOPPED"))
      .toEqual([expect.objectContaining({ severity: "info",
        details: expect.objectContaining({ reason: "startup-cleanup" }) })]);
  });
});
