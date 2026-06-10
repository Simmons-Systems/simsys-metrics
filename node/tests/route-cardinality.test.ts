/**
 * Route-label cardinality hardening (infra#37576).
 *
 * SAFE_TEXT_SEGMENT_RE keeps lowercase-wordish segments verbatim, and
 * internet scanner wordlists are exactly that shape — on the public BFR
 * vhosts this minted ~8k route labels (14 MB scrape body) in 10 days.
 * Two defenses under test:
 *
 *   1. 404 responses collapse to `/__unmatched__` (mirrors the Express
 *      adapter's `__unmatched__` router-miss label).
 *   2. Distinct route labels are capped per process (`maxRoutes`,
 *      default 300); overflow records as `/__overflow__`.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import http from "node:http";
import { registry } from "../src/index.js";
import { _resetRouteCapForTests } from "../src/adapters/next.js";
import { _resetForTests as _resetProc } from "../src/process.js";
import { _resetForTests as _resetBase } from "../src/baseline.js";
import { _resetBuildInfoOwnershipForTests } from "../src/buildinfo.js";

const ORIG_HTTP_EMIT = http.Server.prototype.emit;

function resetAll() {
  registry.resetMetrics();
  _resetProc();
  _resetBase();
  _resetBuildInfoOwnershipForTests();
  _resetRouteCapForTests();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).__simsysNextInstalled = false;
  http.Server.prototype.emit = ORIG_HTTP_EMIT;
}

async function driveRequest(
  server: http.Server,
  path: string,
): Promise<number> {
  const addr = server.address();
  if (!addr || typeof addr === "string") {
    throw new Error("server has no address");
  }
  const res = await fetch(`http://127.0.0.1:${addr.port}${path}`);
  await res.text();
  return res.status;
}

function startServer(handler: http.RequestListener): Promise<http.Server> {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, () => resolve(server));
  });
}

function closeServer(server: http.Server): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

describe("404 collapse to /__unmatched__", () => {
  beforeEach(() => resetAll());
  afterEach(() => resetAll());

  it("scanner wordlist paths returning 404 share one route label", async () => {
    const { installNext } = await import("../src/index.js");
    installNext({ service: "scan-test", version: "0.0.0" });

    const server = await startServer((_req, res) => {
      res.writeHead(404);
      res.end("not found");
    });

    const SCANNER_PATHS = [
      "/wp-login.php",
      "/aaf.php",
      "/aardvarktopsites/index.php",
      "/abantecart/index.php",
      "/vendor/phpunit/whatever.php",
    ];
    try {
      for (const p of SCANNER_PATHS) {
        expect(await driveRequest(server, p)).toBe(404);
      }
    } finally {
      await closeServer(server);
    }

    const body = await registry.metrics();
    expect(body).toContain('route="/__unmatched__"');
    expect(body).not.toContain("wp-login");
    expect(body).not.toContain("abantecart");
    expect(body).not.toContain("phpunit");
  });

  it("non-404 statuses keep their bucketed route label (401 auth denial)", async () => {
    const { installNext } = await import("../src/index.js");
    installNext({ service: "auth-test", version: "0.0.0" });

    const server = await startServer((_req, res) => {
      res.writeHead(401);
      res.end("denied");
    });

    try {
      expect(await driveRequest(server, "/api/admin")).toBe(401);
    } finally {
      await closeServer(server);
    }

    const body = await registry.metrics();
    expect(body).toContain('route="/api/admin"');
    expect(body).not.toContain('route="/__unmatched__"');
  });
});

describe("distinct-route cap → /__overflow__", () => {
  beforeEach(() => resetAll());
  afterEach(() => resetAll());

  it("routes beyond maxRoutes record as /__overflow__", async () => {
    const { installNext } = await import("../src/index.js");
    installNext({ service: "cap-test", version: "0.0.0", maxRoutes: 3 });

    const server = await startServer((_req, res) => {
      res.writeHead(200);
      res.end("ok");
    });

    try {
      for (const p of ["/alpha", "/beta", "/gamma", "/delta", "/epsilon"]) {
        expect(await driveRequest(server, p)).toBe(200);
      }
    } finally {
      await closeServer(server);
    }

    const body = await registry.metrics();
    expect(body).toContain('route="/alpha"');
    expect(body).toContain('route="/beta"');
    expect(body).toContain('route="/gamma"');
    expect(body).toContain('route="/__overflow__"');
    expect(body).not.toContain('route="/delta"');
    expect(body).not.toContain('route="/epsilon"');
  });

  it("already-seen routes keep recording after the cap is hit", async () => {
    const { installNext } = await import("../src/index.js");
    installNext({ service: "cap-seen", version: "0.0.0", maxRoutes: 2 });

    const server = await startServer((_req, res) => {
      res.writeHead(200);
      res.end("ok");
    });

    try {
      await driveRequest(server, "/alpha");
      await driveRequest(server, "/beta");
      await driveRequest(server, "/gamma"); // over cap → overflow
      await driveRequest(server, "/alpha"); // seen → still /alpha
    } finally {
      await closeServer(server);
    }

    const body = await registry.metrics();
    const alphaLine = body
      .split("\n")
      .find(
        (l) =>
          l.startsWith("simsys_http_requests_total") && l.includes('route="/alpha"'),
      );
    expect(alphaLine).toBeDefined();
    expect(alphaLine).toContain(" 2");
    expect(body).toContain('route="/__overflow__"');
  });

  it("attacker-supplied /__overflow__ path cannot impersonate the sentinel route", async () => {
    const { installNext } = await import("../src/index.js");
    installNext({ service: "sentinel-test", version: "0.0.0" });

    const server = await startServer((_req, res) => {
      res.writeHead(200);
      res.end("ok");
    });

    try {
      await driveRequest(server, "/__overflow__");
    } finally {
      await closeServer(server);
    }

    // `__` fails SAFE_TEXT_SEGMENT_RE → buckets to /:str, not the sentinel.
    const body = await registry.metrics();
    expect(body).toContain('route="/:str"');
    expect(body).not.toContain('route="/__overflow__"');
  });
});
