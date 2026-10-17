import init, { VisionGpuEngine } from "/vision/pkg/lumencast_vision_web.js";
import { createMainThreadPresenter } from "/vision/ui/mainpresenter.mjs";

const encoder = new TextEncoder();
const canvas = document.querySelector("#vision");
const LEGACY_COMMIT = "85c49664a029744369b063ddffaa92415a59c8a8";

const canonical = (value) => {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
    );
  }
  return value;
};

async function pinSceneVersion(scene) {
  scene.scene_version = `sha256:${"0".repeat(64)}`;
  const digest = await crypto.subtle.digest(
    "SHA-256",
    encoder.encode(JSON.stringify(canonical(scene))),
  );
  scene.scene_version = `sha256:${[...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")}`;
  return scene;
}

function makeFrameMaskScene(source) {
  const capture = structuredClone(
    source.layout.children[0].children.find(
      (child) => child.kind === "x-zab.capture",
    ),
  );
  if (!capture) throw new Error("Vision fixture has no x-zab.capture node");
  capture.id = "frame-mask-browser-capture";
  capture.position = { x: 250, y: 20 };
  capture.size = { w: 32, h: 32 };
  return {
    lsml: "1.2",
    scene_id: "vision-frame-mask-browser-proof",
    scene_version: `sha256:${"0".repeat(64)}`,
    defaults: { "mask.opacity": 0.5 },
    layout: {
      kind: "frame",
      id: "frame-mask-browser-root",
      size: { w: 320, h: 180 },
      background: "#0c1119",
      clipsContent: false,
      children: [
        {
          kind: "stack",
          id: "frame-mask-browser-owner",
          direction: "horizontal",
          size: { w: 320, h: 180 },
          clipsContent: false,
          mask: {
            type: "alpha",
            op: "intersect",
            source: { kind: "group", ref: "frame-mask-browser-stencil" },
          },
          children: [
            {
              kind: "shape",
              id: "frame-mask-browser-paint",
              geometry: "rect",
              size: { w: 320, h: 180 },
              fill: "#ffffff",
            },
            capture,
          ],
        },
        {
          kind: "frame",
          id: "frame-mask-browser-stencil",
          visible: false,
          size: { w: 120, h: 124 },
          clipsContent: true,
          children: [
            {
              kind: "frame",
              id: "frame-mask-opacity-clipped",
              position: { x: 8, y: 16 },
              size: { w: 52, h: 92 },
              clipsContent: true,
              bind: { opacity: "mask.opacity" },
              children: [
                {
                  kind: "shape",
                  geometry: "rect",
                  size: { w: 36, h: 92 },
                  fill: "#ffffff",
                },
                {
                  kind: "shape",
                  geometry: "rect",
                  position: { x: 24, y: 0 },
                  size: { w: 44, h: 92 },
                  fill: "#ffffff",
                },
              ],
            },
            {
              kind: "frame",
              id: "frame-mask-opacity-unclipped",
              position: { x: 80, y: 16 },
              size: { w: 20, h: 92 },
              clipsContent: false,
              bind: { opacity: "mask.opacity" },
              children: [
                {
                  kind: "shape",
                  geometry: "rect",
                  size: { w: 64, h: 92 },
                  fill: "#ffffff",
                },
              ],
            },
          ],
        },
      ],
    },
  };
}

function makePresenter(target, loadWasm) {
  const messages = [];
  const presenter = createMainThreadPresenter(target, { loadWasm });
  presenter.addEventListener("message", (event) => messages.push(event.data));
  let sequence = 0;
  const request = async (message) => {
    const seq = sequence++;
    presenter.postMessage({ ...message, seq });
    await presenter.flush();
    const expectedType = {
      "init-empty": "engine-ready",
      "native-composition-mode": "native-composition-mode-set",
      load: "frame-submitted",
      patch: "patch-applied",
    }[message.type];
    const error = messages.find(
      (entry) => entry.type === "error" && entry.seq === seq,
    );
    if (error) throw new Error(`${message.type}: ${error.message}`);
    const response = messages.find(
      (entry) => entry.seq === seq && entry.type === expectedType,
    );
    if (!response) throw new Error(`${message.type}: no presenter reply`);
    return response;
  };
  return { presenter, messages, request };
}

async function postFrame(variant, document, message, target, mutation = null) {
  const gl = target.getContext("webgl2");
  if (!gl || gl.isContextLost()) throw new Error("Vision WebGL2 context unavailable");
  const receipt = message.composition;
  if (
    message.type !== "frame-submitted" ||
    receipt?.schema !== "vision.native-composition-frame/2" ||
    receipt.sceneId !== document.scene_id ||
    receipt.sceneVersion !== document.scene_version ||
    receipt.canvasSize?.[0] !== target.width ||
    receipt.canvasSize?.[1] !== target.height
  ) {
    throw new Error("actual presenter did not submit the matching /2 receipt");
  }
  const payload = {
    variant,
    document,
    message,
    canvasSize: [target.width, target.height],
    maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
  };
  if (mutation !== null) payload.mutation = mutation;
  const response = await fetch("/frame", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`frame receiver returned HTTP ${response.status}`);
  return payload;
}

async function proveLegacyRejectsExactReceipt(sceneDocument, actualReceipt, currentPresenter) {
  const { createMainThreadPresenter: createLegacy } = await import(
    "/baseline/mainpresenter.mjs"
  );
  currentPresenter.presenter.terminate();
  await currentPresenter.presenter.flush();
  const legacyCanvas = globalThis.document.querySelector("#legacy");
  const legacyMessages = [];
  let suppliedExactReceipt = false;
  const legacyEngine = {
    create_html_canvas: VisionGpuEngine.create_html_canvas.bind(VisionGpuEngine),
    async create_empty_html_canvas(target, width, height) {
      const engine = await VisionGpuEngine.create_empty_html_canvas(
        target,
        width,
        height,
      );
      return new Proxy(engine, {
        get(inner, property) {
          if (property === "render_contribution_atlas_to_canvas") {
            return async (...dimensions) => {
              const produced = await inner.render_contribution_atlas_to_canvas(
                ...dimensions,
              );
              if (
                produced.schema !== actualReceipt.schema ||
                produced.sceneId !== actualReceipt.sceneId ||
                produced.sceneVersion !== actualReceipt.sceneVersion ||
                produced.coverageSurfaces?.length !==
                  actualReceipt.coverageSurfaces?.length
              ) {
                throw new Error("legacy negative did not render the same scene /2");
              }
              suppliedExactReceipt = true;
              return actualReceipt;
            };
          }
          const value = Reflect.get(inner, property, inner);
          return typeof value === "function" ? value.bind(inner) : value;
        },
      });
    },
  };
  const presenter = createLegacy(legacyCanvas, { loadWasm: async () => ({
    default: init,
    VisionGpuEngine: legacyEngine,
  }) });
  presenter.addEventListener("message", (event) => legacyMessages.push(event.data));
  const baselineRequest = async (message) => {
    presenter.postMessage(message);
    await presenter.flush();
    return legacyMessages.filter((entry) => entry.seq === message.seq);
  };
  try {
    await baselineRequest({
      type: "init-empty",
      seq: 0,
      canvas: legacyCanvas,
      width: 320,
      height: 180,
    });
    await baselineRequest({ type: "native-composition-mode", seq: 1, enabled: true });
    await baselineRequest({
      type: "load",
      seq: 2,
      packageBytes: encoder.encode(JSON.stringify(sceneDocument)),
      fonts: [],
    });
    const error = legacyMessages.find(
      (entry) => entry.type === "error" && entry.seq === 2,
    );
    const submitted = legacyMessages.some(
      (entry) => entry.type === "frame-submitted" && entry.seq === 2,
    );
    if (
      !suppliedExactReceipt ||
      !error ||
      submitted ||
      !/invalid native composition frame receipt/i.test(error.message)
    ) {
      throw new Error(`pinned legacy presenter negative failed: ${JSON.stringify({
        suppliedExactReceipt,
        actualReceiptSchema: actualReceipt?.schema,
        error: error ? { message: error.message, phase: error.phase, seq: error.seq } : null,
        submitted,
        seq2Messages: legacyMessages.filter((entry) => entry.seq === 2),
      })}`);
    }
    return {
      baselineCommit: LEGACY_COMMIT,
      exactReceiptSupplied: suppliedExactReceipt,
      emittedFrameSubmitted: submitted,
      errorPhase: error.phase,
      errorMessage: error.message,
    };
  } finally {
    presenter.terminate();
    await presenter.flush();
  }
}

async function loadInitialScene(source, presenter) {
  const document = await pinSceneVersion(makeFrameMaskScene(source));
  const message = await presenter.request({
    type: "load",
    packageBytes: encoder.encode(JSON.stringify(document)),
    fonts: [],
  });
  const payload = await postFrame("initial", document, message, canvas);
  return { document, receipt: message.composition, payload };
}

try {
  const fixtureResponse = await fetch("/vision-fixture/contribution-scene.lsml");
  if (!fixtureResponse.ok)
    throw new Error(`Vision fixture route returned HTTP ${fixtureResponse.status}`);
  const fixture = await fixtureResponse.json();
  const presenter = makePresenter(canvas, async () => ({
    default: init,
    VisionGpuEngine,
  }));
  await presenter.request({
    type: "init-empty",
    canvas,
    width: 320,
    height: 180,
  });
  await presenter.request({ type: "native-composition-mode", enabled: true });

  const initial = await loadInitialScene(fixture, presenter);
  const owner = initial.receipt.owners.find((entry) => entry.maskPresent);
  if (!owner || owner.maskSurfaceIndex == null)
    throw new Error("frame-masked owner was absent from the actual presenter receipt");

  let lastOpacity = 0.5;
  let latest = initial;
  let legacyStarted = false;
  window.__visionCoverageChainReady = true;
  setInterval(async () => {
    if (window.__visionCoverageChainBusy) return;
    try {
      const controlResponse = await fetch("/control", { cache: "no-store" });
      if (!controlResponse.ok) throw new Error("control endpoint unavailable");
      const control = await controlResponse.json();
      if (control.runLegacy && !legacyStarted) {
        legacyStarted = true;
        window.__visionCoverageChainBusy = true;
        const negative = await proveLegacyRejectsExactReceipt(
          latest.document,
          latest.receipt,
          presenter,
        );
        await fetch("/negative", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(negative),
        });
        window.__visionCoverageChainLegacyNegative = negative;
        window.__visionCoverageChainBusy = false;
        return;
      }
      if (control.opacity === lastOpacity) return;
      window.__visionCoverageChainBusy = true;
      const patchApplied = await presenter.request({
        type: "patch",
        patchJson: JSON.stringify({ "mask.opacity": control.opacity }),
      });
      const changedNodes = patchApplied.metadata?.changed_nodes;
      if (!Array.isArray(changedNodes) || changedNodes.length === 0) {
        throw new Error("mask opacity state patch did not change a bound Vision node");
      }
      const matchingFrames = presenter.messages.filter(
        message => message.type === "frame-submitted" && message.seq === patchApplied.seq,
      );
      const frame = matchingFrames[matchingFrames.length - 1];
      if (!frame) throw new Error("mask opacity patch did not submit a new Vision frame");
      const mutation = {
        type: "patch",
        path: "mask.opacity",
        value: control.opacity,
        seq: patchApplied.seq,
        changedNodes,
      };
      const payload = await postFrame("updated", latest.document, frame, canvas, mutation);
      const updated = {
        document: latest.document,
        receipt: frame.composition,
        payload,
        message: frame,
        mutation,
      };
      lastOpacity = control.opacity;
      latest = updated;
      window.__visionCoverageChainUpdated = {
        sceneId: updated.document.scene_id,
        sceneVersion: updated.document.scene_version,
        canvasSize: updated.payload.canvasSize,
        pixelRevision: updated.receipt.pixelRevision,
        layoutProjectionRevision: updated.receipt.layoutProjectionRevision,
        mutation,
      };
      window.__visionCoverageChainBusy = false;
    } catch (error) {
      window.__visionCoverageChainError = String(error?.stack ?? error);
      window.__visionCoverageChainBusy = false;
      await fetch("/negative", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ error: window.__visionCoverageChainError }),
      }).catch(() => {});
    }
  }, 250);
  window.__visionCoverageChainInitial = {
    sceneId: initial.document.scene_id,
    sceneVersion: initial.document.scene_version,
    canvasSize: initial.payload.canvasSize,
    pixelRevision: initial.receipt.pixelRevision,
  };
} catch (error) {
  window.__visionCoverageChainError = String(error?.stack ?? error);
  await fetch("/negative", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ error: window.__visionCoverageChainError }),
  }).catch(() => {});
}
