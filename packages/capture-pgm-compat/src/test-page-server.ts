import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

/**
 * A tiny local-only HTTP server (loopback, no external network) serving the
 * two real pages this unit's negative controls need. Everything else 404s
 * on purpose -- the third scenario ("black") is a request to a path this
 * server does NOT serve, which @clodocapeo/pulsar-bundle-full's own README
 * documents as CEF rendering blank/black ("CEF won't render a 404 page by
 * default"), not a fixture I invented.
 */
export interface TestPageServer {
  /** e.g. "http://127.0.0.1:51234" */
  readonly baseUrl: string;
  urlFor(path: string): string;
  close(): Promise<void>;
}

const HEALTHY_PAGE = `<!doctype html>
<html><head><meta charset="utf-8"><style>html,body{margin:0;background:#000}</style></head>
<body>
<canvas id="c" width="320" height="240"></canvas>
<script>
  // Real, continuously-varying content: a moving field of coloured
  // rectangles driven by requestAnimationFrame. Genuine spatial detail
  // (many distinct colours per frame) AND genuine temporal change
  // (every frame differs from the last) -- both axes this unit measures.
  var ctx = document.getElementById('c').getContext('2d');
  var w = 320, h = 240, n = 0;
  function draw() {
    n++;
    for (var y = 0; y < h; y += 20) {
      for (var x = 0; x < w; x += 20) {
        var r = (x + n * 3) % 255;
        var g = (y + n * 5) % 255;
        var b = (n * 7) % 255;
        ctx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
        ctx.fillRect(x, y, 20, 20);
      }
    }
    requestAnimationFrame(draw);
  }
  draw();
</script>
</body></html>`;

const FROZEN_PAGE = `<!doctype html>
<html><head><meta charset="utf-8"><style>html,body{margin:0;background:#000}</style></head>
<body>
<canvas id="c" width="320" height="240"></canvas>
<script>
  // Deliberately NOT an animation: one real draw call on load, painting
  // real spatial detail (a checkerboard of distinct colours), then nothing
  // ever runs again -- no requestAnimationFrame, no interval, no further
  // DOM mutation. This is the "CEF rendered fine once, then JS execution
  // never updates the page again" failure mode named in #231: a source
  // that is NOT black (real detail, real non-zero spatialStddev/meanLuma)
  // but IS temporally dead (temporalDiff == 0). Spatial-only measures
  // cannot tell this apart from healthy.
  var ctx = document.getElementById('c').getContext('2d');
  var w = 320, h = 240;
  for (var y = 0; y < h; y += 20) {
    for (var x = 0; x < w; x += 20) {
      var r = (x * 3) % 255, g = (y * 5) % 255, b = ((x + y) * 7) % 255;
      ctx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
      ctx.fillRect(x, y, 20, 20);
    }
  }
</script>
</body></html>`;

export async function startTestPageServer(port = 0): Promise<TestPageServer> {
  const server: Server = createServer((req, res) => {
    const url = req.url ?? "/";
    if (url === "/healthy") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(HEALTHY_PAGE);
      return;
    }
    if (url === "/frozen") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(FROZEN_PAGE);
      return;
    }
    // Every other path, including /missing (the "black" scenario's URL) --
    // a real 404, not a stub. CEF's documented behaviour on a 404 is what
    // this unit's negative control relies on, not a mocked response.
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve());
  });

  const addr = server.address() as AddressInfo;
  const baseUrl = `http://127.0.0.1:${addr.port}`;

  return {
    baseUrl,
    urlFor: (path: string) => `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((err) => (err ? reject(err) : resolve()));
      }),
  };
}
