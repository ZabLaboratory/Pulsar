// @zablaboratory/pulsar-bundle postinstall.
//
// Downloads the matching pulsar-windows-x64-v<VERSION>.zip from the
// Pulsar GitHub Releases and extracts it into ./binaries/. Idempotent:
// a stamp file under binaries/.version records what's already on disk
// so re-running npm install on an unchanged version is a no-op.
//
// Skip with `PULSAR_BUNDLE_SKIP_POSTINSTALL=1` (useful for CI matrix
// runs that don't actually need the binary, or for offline builds with
// a vendored copy).
//
// Override the download URL with `PULSAR_BUNDLE_DOWNLOAD_URL=<url>` if
// the binary is hosted somewhere else (private mirror, internal CDN).

import { execFileSync } from "node:child_process";
import { createWriteStream } from "node:fs";
import { mkdir, readFile, rm, writeFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pkgRoot = resolve(__dirname, "..");

if (process.env.PULSAR_BUNDLE_SKIP_POSTINSTALL === "1") {
  console.log("[pulsar-bundle] PULSAR_BUNDLE_SKIP_POSTINSTALL=1 -- skipping binary download");
  process.exit(0);
}

if (process.platform !== "win32" || process.arch !== "x64") {
  // package.json's "os" / "cpu" guards prevent this on a normal install,
  // but a cross-platform `npm install --force` could end up here.
  console.log(`[pulsar-bundle] platform=${process.platform} arch=${process.arch} unsupported; skipping`);
  process.exit(0);
}

const pkg = JSON.parse(await readFile(join(pkgRoot, "package.json"), "utf8"));
const version = pkg.version;
const zipName = `pulsar-windows-x64-v${version}.zip`;
const url =
  process.env.PULSAR_BUNDLE_DOWNLOAD_URL ??
  `https://github.com/ZabLaboratory/Pulsar/releases/download/v${version}/${zipName}`;

const binariesDir = join(pkgRoot, "binaries");
const stampFile = join(binariesDir, ".version-stamp");

// Cache hit?
try {
  const stamp = await readFile(stampFile, "utf8");
  if (stamp.trim() === version) {
    console.log(`[pulsar-bundle] binaries/ already at v${version}, skipping download`);
    process.exit(0);
  }
} catch {
  // Missing stamp -> proceed
}

await rm(binariesDir, { recursive: true, force: true });
await mkdir(binariesDir, { recursive: true });

console.log(`[pulsar-bundle] downloading ${url}`);
let response;
try {
  response = await fetch(url, { redirect: "follow" });
} catch (err) {
  console.warn(`[pulsar-bundle] WARN: network error fetching binary: ${err.message}`);
  console.warn("[pulsar-bundle] WARN: pulsar.exe will be unavailable until you re-install with network access");
  process.exit(0);
}
if (!response.ok || !response.body) {
  console.warn(`[pulsar-bundle] WARN: download failed: ${response.status} ${response.statusText}`);
  console.warn(`[pulsar-bundle] WARN: is the v${version} release published?`);
  console.warn("[pulsar-bundle]       https://github.com/ZabLaboratory/Pulsar/releases");
  console.warn("[pulsar-bundle] WARN: pulsar.exe will be unavailable; spawn() will throw a clear error");
  // Soft-fail so npm install completes -- the bundle is still usable in
  // monorepo dev (where binariesPath is overridden) and the failure is
  // surfaced loudly enough that production callers will fix it.
  process.exit(0);
}

const zipPath = join(binariesDir, zipName);
await pipeline(Readable.fromWeb(response.body), createWriteStream(zipPath));
const sizeMb = ((await stat(zipPath)).size / 1024 / 1024).toFixed(1);
console.log(`[pulsar-bundle] downloaded ${sizeMb} MB`);

console.log("[pulsar-bundle] extracting...");
// Expand-Archive is built into PowerShell since Windows 10. Avoids
// adding a native unzip dependency to the package.
execFileSync(
  "powershell.exe",
  [
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    `Expand-Archive -Path "${zipPath}" -DestinationPath "${binariesDir}" -Force`,
  ],
  { stdio: "inherit" },
);
await rm(zipPath, { force: true });

await writeFile(stampFile, `${version}\n`, "utf8");
console.log(`[pulsar-bundle] binaries ready at ${binariesDir}`);
