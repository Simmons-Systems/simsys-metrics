/**
 * Regression test for the Phase 3 Next.js empty-body bug
 * (https://github.com/Simmons-Systems/simsys-metrics — node v0.4.1 → v0.4.2).
 *
 * Webpack standalone bundling can inline this package's modules into
 * multiple server chunks (e.g. instrumentation.ts and
 * app/api/metrics/route.ts each get their own copy of registry.ts).
 * Without globalThis-pinned singletons, each chunk constructs its own
 * `new Registry()` and its own metric instances at module top-level —
 * `installNext()` writes samples to one registry, the GET handler
 * reads from the other, and `/metrics` returns HELP/TYPE only.
 *
 * The fix (commit-message-search the v0.4.2 changelog) pins every
 * stateful singleton to globalThis. This test simulates the
 * chunk-split scenario by clearing Vitest's module cache between
 * imports and verifies that BOTH "module instances" hand out the same
 * Registry, the same metric instances, and the same buildinfo
 * ownership Set.
 *
 * If this test starts failing, the package has regressed to per-module
 * singletons and the Next.js empty-body bug is back.
 */

import { describe, it, expect, vi } from "vitest";

describe("registry singletons survive bundler chunk-splitting", () => {
  it("re-importing the package after vi.resetModules yields the same registry instance", async () => {
    const m1 = await import("../src/registry.js");
    const reg1 = m1.registry;
    const buildInfo1 = m1.buildInfo;
    const httpReq1 = m1.httpRequestsTotal;

    vi.resetModules();

    const m2 = await import("../src/registry.js");
    expect(m2.registry).toBe(reg1);
    expect(m2.buildInfo).toBe(buildInfo1);
    expect(m2.httpRequestsTotal).toBe(httpReq1);
  });

  it("baseline state survives across module reloads", async () => {
    const b1 = await import("../src/baseline.js");
    b1.setService("chunk-split-test");

    vi.resetModules();

    const b2 = await import("../src/baseline.js");
    expect(b2._peekService()).toBe("chunk-split-test");

    // Cleanup so we don't leak into other suites.
    b2._resetForTests();
  });

  it("buildinfo ownership Set survives across module reloads", async () => {
    const bi1 = await import("../src/buildinfo.js");
    const labels = {
      service: "chunk-split-test",
      version: "0.0.0",
      commit: "deadbeef",
      started_at: "2026-04-27T00:00:00Z",
    };

    const r1 = bi1.registerBuildInfo(labels);
    expect(r1.wasNew).toBe(true);

    vi.resetModules();

    const bi2 = await import("../src/buildinfo.js");
    // Second registration on the second "module instance" with the same
    // labelset must see the prior ownership entry — wasNew=false. Without
    // globalThis pinning, bi2 would have a fresh Set and treat this as a
    // new sample, then on rollback silently delete bi1's still-live
    // sample.
    const r2 = bi2.registerBuildInfo(labels);
    expect(r2.wasNew).toBe(false);

    // Cleanup.
    bi2.unregisterBuildInfoIfOwned(labels, true);
    bi2._resetBuildInfoOwnershipForTests();
  });

  it("registry.metrics() output from the second instance includes samples written via the first", async () => {
    const r1 = await import("../src/registry.js");
    r1.buildInfo
      .labels({
        service: "chunk-split-svc",
        version: "1.2.3",
        commit: "abc",
        started_at: "2026-04-27T00:00:00Z",
      })
      .set(1);

    vi.resetModules();

    const r2 = await import("../src/registry.js");
    const body = await r2.registry.metrics();
    expect(body).toContain('simsys_build_info{service="chunk-split-svc"');
    expect(body).toContain('version="1.2.3"');

    // Cleanup so we don't leak the labelset into other suites.
    r2.buildInfo.remove({
      service: "chunk-split-svc",
      version: "1.2.3",
      commit: "abc",
      started_at: "2026-04-27T00:00:00Z",
    });
  });
});
