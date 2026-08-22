/**
 * Regression: a partial install (e.g. app.get throws while wiring
 * /metrics) must NOT leave the sentinel `simsysMetricsInstalled`
 * set to true. Otherwise a retry no-ops via the idempotent guard
 * and /metrics + the HTTP middleware are never wired.
 *
 * Pre-fix the sentinel was set BEFORE the framework-wiring calls;
 * v0.3.8 sets it LAST inside a try/catch that rolls back state on
 * partial failure.
 */

import { describe, it, expect, beforeEach } from "vitest";
import express from "express";
import { Hono } from "hono";
import { install, registry } from "../src/index.js";
import { _resetForTests as _resetProc } from "../src/process.js";
import { _resetForTests as _resetBase } from "../src/baseline.js";

describe("partial install rollback", () => {
  beforeEach(() => {
    _resetProc();
    _resetBase();
    registry.resetMetrics();
  });

  it("express: failure during app.get rolls back the sentinel", async () => {
    const app = express();
    const realGet = app.get.bind(app);
    let calls = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    app.get = ((...args: any[]) => {
      calls += 1;
      if (calls === 1) throw new Error("forced get failure");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return (realGet as any)(...args);
    }) as typeof app.get;

    expect(() =>
      install(app, { service: "express-partial", version: "0.0.1" }),
    ).toThrow(/forced get failure/);

    // Sentinel must NOT be set after a failed install — otherwise
    // retry no-ops on the idempotent guard.
    expect(app.locals.simsysMetricsInstalled).toBeUndefined();
    expect(app.locals.simsysService).toBeUndefined();

    // Retry succeeds and wires everything.
    install(app, { service: "express-partial", version: "0.0.1" });
    expect(app.locals.simsysMetricsInstalled).toBe(true);

    // The /metrics route is actually mounted now.
    await new Promise<void>((resolve, reject) => {
      const server = app.listen(0, async () => {
        try {
          const addr = server.address();
          if (!addr || typeof addr === "string") {
            server.close();
            resolve();
            return;
          }
          const res = await fetch(`http://127.0.0.1:${addr.port}/metrics`);
          expect(res.status).toBe(200);
          const body = await res.text();
          expect(body).toContain('service="express-partial"');
          server.close();
          resolve();
        } catch (e) {
          server.close();
          reject(e);
        }
      });
    });
  });

  it("hono: failure during app.get rolls back the sentinel", async () => {
    const app = new Hono();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const props = app as any;
    const realGet = app.get.bind(app);
    let calls = 0;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    app.get = ((...args: any[]) => {
      calls += 1;
      if (calls === 1) throw new Error("forced hono get failure");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return (realGet as any)(...args);
    }) as typeof app.get;

    expect(() =>
      install(app, { service: "hono-partial", version: "0.0.1" }),
    ).toThrow(/forced hono get failure/);

    expect(props.simsysMetricsInstalled).toBeUndefined();
    expect(props.simsysService).toBeUndefined();

    // Retry succeeds.
    install(app, { service: "hono-partial", version: "0.0.1" });
    expect(props.simsysMetricsInstalled).toBe(true);

    const res = await app.fetch(new Request("http://l/metrics"));
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain('service="hono-partial"');
  });
});
