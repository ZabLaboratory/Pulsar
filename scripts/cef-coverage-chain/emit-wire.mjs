import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function requiredId(value, label) {
  if (!Number.isSafeInteger(value) || value < 1)
    throw new Error(`${label} must be a positive safe integer`);
  return value;
}

function findCaptureNodes(value, result = []) {
  if (Array.isArray(value)) {
    for (const child of value) findCaptureNodes(child, result);
    return result;
  }
  if (!value || typeof value !== "object") return result;
  if (
    value.kind === "x-zab.capture" &&
    typeof value.id === "string" &&
    typeof value["x-zab.sourceKind"] === "string" &&
    typeof value["x-zab.deviceRef"] === "string"
  ) {
    result.push(value);
  }
  if (Array.isArray(value.children)) findCaptureNodes(value.children, result);
  if (value.template !== undefined) findCaptureNodes(value.template, result);
  return result;
}

async function viteAt(root, cacheDir) {
  const entry = pathToFileURL(join(root, "node_modules/vite/dist/node/index.js"));
  const { createServer: createViteServer } = await import(entry.href);
  return createViteServer({
    root,
    configFile: false,
    cacheDir,
    optimizeDeps: { noDiscovery: true, include: [] },
    server: { middlewareMode: true, hmr: false, ws: false },
    appType: "custom",
  });
}

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath)
  throw new Error("usage: node emit-wire.mjs <input.json> <output.json>");
const inputBytes = await readFile(inputPath);
const input = JSON.parse(inputBytes.toString("utf8"));
const document = input.document;
const receipt = input.message?.composition;
if (
  !document ||
  !receipt ||
  receipt.schema !== "vision.native-composition-frame/2" ||
  receipt.sceneId !== document.scene_id ||
  receipt.sceneVersion !== document.scene_version
)
  throw new Error("actual Vision /2 receipt and submitted LSML identity must match");

const solarServer = await viteAt(input.solarRoot, input.solarCacheDir);
const prismServer = await viteAt(input.prismRoot, input.prismCacheDir);
try {
  const solarComposition = await solarServer.ssrLoadModule(
    "/src/scenes/native-composition.ts",
  );
  const solarTree = await solarServer.ssrLoadModule(
    "/src/internal/native-tree.ts",
  );
  const prismConsumer = await prismServer.ssrLoadModule(
    "/src/main/broadcast/engine/capture/native-composition-consumer.ts",
  );

  const viewport = {
    width: document.layout?.size?.w,
    height: document.layout?.size?.h,
  };
  const projection = solarComposition.projectNativeCompositionFrame(receipt, {
    sceneId: document.scene_id,
    sceneVersion: document.scene_version,
    width: viewport.width,
    height: viewport.height,
    maxTextureSize: input.maxTextureSize,
  });
  const atlasSize = prismConsumer.nativeCompositionAtlasSize({
    schema: projection.coverageSurfaces
      ? "solar.native-composition.plan/2"
      : "solar.native-composition.plan/1",
    coordinateSpace: projection.coordinateSpace,
    atlasSize: projection.atlasSize,
    viewport: projection.viewport,
    surfaces: projection.surfaces,
  });
  if (
    atlasSize.width !== input.canvasSize?.[0] ||
    atlasSize.height !== input.canvasSize?.[1]
  )
    throw new Error("Solar atlas dimensions differ from the same CEF canvas");
  if (!projection.coverageSurfaces?.length)
    throw new Error("the actual Vision /2 frame has no Solar coverage surfaces");

  const stateHash = solarTree.nativeTreeHash(document);
  const plan = solarComposition.withNativeCompositionSource(
    projection,
    input.producerId,
    {
      resource: "solar/lane-a",
      scene_id: document.scene_id,
      scene_version: document.scene_version,
      stateHash,
    },
  );
  const captures = findCaptureNodes(document.layout);
  if (captures.length !== 1)
    throw new Error(`expected exactly one LSML capture, received ${captures.length}`);
  const captureRecord = captures[0];
  const captureChild = plan.groups
    .flatMap((group) => group.children)
    .find((child) => child.kind === "capture");
  if (!captureChild) throw new Error("Solar projection omitted the capture occurrence");
  const sourceComponentId = captureChild.sourceComponentId ?? captureChild.componentId;
  if (captureRecord.id !== sourceComponentId)
    throw new Error("LSML capture and Vision effective source identity differ");

  const captureBindings = new Map([
    [sourceComponentId, {
      sceneItemId: requiredId(input.captureBindingSceneItemId, "captureBindingSceneItemId"),
      sourceKind: captureRecord["x-zab.sourceKind"],
      deviceRef: captureRecord["x-zab.deviceRef"],
    }],
  ]);
  const validation = prismConsumer.validateSolarNativeCompositionPlan({
    lane: "A",
    producerId: input.producerId,
    plan,
    document,
    documentStateHash: stateHash,
    captureBindings,
  });
  const cefSceneItemId = requiredId(input.cefSceneItemId, "cefSceneItemId");
  const contributionSceneItemId = requiredId(
    input.captureContributionSceneItemId,
    "captureContributionSceneItemId",
  );
  const composed = prismConsumer.buildNativeCompositionProjection({
    plan: validation.plan,
    captureBindings,
    captureContributionItems: new Map([[captureChild.componentId, contributionSceneItemId]]),
    cef: {
      sceneItemId: cefSceneItemId,
      sourceWidth: input.canvasSize[0],
      sourceHeight: input.canvasSize[1],
      replacedSceneItems: [cefSceneItemId],
    },
  });
  const generation = requiredId(input.generation, "generation");
  const wire = {
    protocol: composed.protocol ?? "pulsar.group-composition/1",
    ...(composed.coordinateSpace !== undefined
      ? { coordinateSpace: composed.coordinateSpace }
      : {}),
    sceneName: input.sceneName,
    runtimeInstanceId: input.runtimeInstanceId,
    generation,
    planHash: prismConsumer.nativeCompositionWireHash(composed),
    groups: composed.groups,
    replacedSceneItems: composed.replacedSceneItems,
  };
  if (
    wire.protocol !== "pulsar.group-composition/2" ||
    wire.coordinateSpace !== "global-scene-texels"
  )
    throw new Error("Prism did not produce the required native /2 wire");

  // A structural mask-negative is prepared from the same product projection.
  // Changing the crop and recomputing Prism's hash makes this a real plugin
  // mask-dimension refusal rather than a stale-hash refusal.
  const negativeProjection = structuredClone(composed);
  const maskedGroup = negativeProjection.groups.find((group) => group.mask);
  if (!maskedGroup) throw new Error("Prism projection omitted the masked group");
  maskedGroup.mask.crop.left += 1;
  const negativeWire = {
    ...wire,
    planHash: prismConsumer.nativeCompositionWireHash(negativeProjection),
    groups: negativeProjection.groups,
  };

  const result = {
    passed: true,
    provenance: {
      solarRoot: input.solarRoot,
      prismRoot: input.prismRoot,
      solarProjectionModule: "/src/scenes/native-composition.ts",
      solarSourceModule: "/src/scenes/native-composition-plan.ts",
      solarStateHashModule: "/src/internal/native-tree.ts",
      prismConsumerModule:
        "/src/main/broadcast/engine/capture/native-composition-consumer.ts",
      receiptSchema: receipt.schema,
      receiptSha256: sha256(Buffer.from(JSON.stringify(receipt))),
      documentSha256: sha256(Buffer.from(JSON.stringify(document))),
      documentStateHash: stateHash,
      planHash: plan.planHash,
      atlasSize,
      captureComponentId: captureChild.componentId,
      captureSourceComponentId: sourceComponentId,
      cefSceneItemId,
      captureBindingSceneItemId: input.captureBindingSceneItemId,
      captureContributionSceneItemId: contributionSceneItemId,
    },
    receipt,
    document,
    solarProjection: projection,
    solarPlan: plan,
    prismProjection: composed,
    wire,
    maskDimensionNegativeWire: negativeWire,
    inputSha256: sha256(inputBytes),
  };
  await writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
} finally {
  await Promise.all([solarServer.close(), prismServer.close()]);
}
