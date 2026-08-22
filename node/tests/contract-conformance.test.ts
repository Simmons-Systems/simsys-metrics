/**
 * Node-lane conformance against spec/metrics-contract.json.
 *
 * Reads the expectation from the shared contract and compares it against a
 * LIVE registry — never against node/src/registry.ts, which would be a
 * tautology: the test would agree with the implementation by construction and
 * could never catch a metric that fails to register.
 *
 * Three directions:
 *   1. every metric the contract says node emits is registered, with the
 *      declared type and label names
 *   2. every core metric is present (a core metric missing here is a broken
 *      cross-runtime promise, not a node quirk)
 *   3. no simsys_-prefixed metric is emitted that the contract does not
 *      declare — the additive-drift direction that was missing entirely, and
 *      what would have caught simsys_pool_* shipping undocumented in all
 *      three lanes
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
// eslint-disable-next-line
import { readFileSync } from "node:fs";
// eslint-disable-next-line
import { fileURLToPath } from "node:url";
import { registry } from "../src/registry.js";
import {
  setService,
  trackQueue,
  trackPool,
  trackJob,
  _resetForTests,
} from "../src/baseline.js";
import { trackProgress } from "../src/progress.js";
import { registerProcessCollector, _resetForTests as _resetProcess } from "../src/process.js";
import { registerBuildInfo } from "../src/buildinfo.js";

const CONTRACT_PATH = fileURLToPath(
  new URL("../../spec/metrics-contract.json", import.meta.url),
);
const contract = JSON.parse(readFileSync(CONTRACT_PATH, "utf8"));

type MetricSpec = {
  type: string;
  tier: string;
  runtimes: string[];
  labels?: string[];
  status?: string;
  current?: Record<string, { labels: string[] }>;
};

const metrics: Record<string, MetricSpec> = contract.metrics;

/** Declared labels, honouring a tracked per-runtime divergence. */
function labelsFor(m: MetricSpec): string[] {
  if (m.status === "divergent") {
    const cur = m.current?.node;
    if (!cur) throw new Error("divergent metric with no `current.node` entry");
    return cur.labels;
  }
  return m.labels ?? [];
}

const nodeMetrics = Object.entries(metrics).filter(([, m]) =>
  m.runtimes.includes("node"),
);

let emitted: Map<string, { type: string; labels: Set<string> }>;

describe("metric contract conformance (node)", () => {
  beforeAll(async () => {
    _resetForTests();
    registry.resetMetrics();
    setService("contract-conformance");

    // Registration is split: queue/job/pool/progress collectors exist from
    // module import, but build_info and the process collector only appear once
    // install() runs. Registering them directly rather than standing up a whole
    // Express app keeps the fixture to the thing under test -- but they MUST be
    // registered, or half the catalogue reads as missing and the failure looks
    // like a contract error rather than a fixture error.
    registerProcessCollector("contract-conformance");
    registerBuildInfo({
      service: "contract-conformance",
      version: "0.0.0",
      commit: "test",
      started_at: "1970-01-01T00:00:00Z",
    });

    // Drive every opt-in helper so its labels are observable. A collector with
    // no samples exposes no label names, and a label comparison against zero
    // series passes without testing anything.
    // clearInterval rather than .stop(): the PollerHandle with stop() lands in
    // the Phase-B poller-lifecycle change (#50318), and this conformance test
    // must run on the branch that introduces the contract, not depend on it.
    clearInterval(trackQueue("cq", { depthFn: () => 1, intervalMs: 3_600_000 }));
    clearInterval(trackPool("cp", { activeFn: () => 1, idleFn: () => 1, intervalMs: 3_600_000 }));
    await trackJob("cj").run(async () => undefined);
    trackProgress({ operation: "cop", total: 1, windowMs: 60_000, intervalMs: 3_600_000 }).stop();

    emitted = new Map();
    for (const m of await registry.getMetricsAsJSON()) {
      if (!m.name.startsWith("simsys_")) continue;
      const labels = new Set<string>();
      for (const v of (m as { values?: { labels?: Record<string, unknown> }[] }).values ?? []) {
        for (const k of Object.keys(v.labels ?? {})) {
          // `le` is added by the client to histogram bucket rows; it is not a
          // declared label and no lane chooses it.
          if (k !== "le" && k !== "quantile") labels.add(k);
        }
      }
      emitted.set(m.name, { type: m.type as string, labels });
    }
  });

  afterAll(() => {
    _resetProcess();
    _resetForTests();
  });

  it.each(nodeMetrics.map(([name]) => name))(
    "%s is registered with the declared type",
    (name) => {
      const spec = metrics[name];
      const got = emitted.get(name);
      expect(got, `contract declares ${name} for node but not emitted`).toBeDefined();
      expect(got!.type).toBe(spec.type);
    },
  );

  it.each(nodeMetrics.map(([name]) => name))("%s has the declared labels", (name) => {
    const spec = metrics[name];
    const got = emitted.get(name);
    if (!got || got.labels.size === 0) return; // no series yet; covered above
    expect([...got.labels].sort()).toEqual([...labelsFor(spec)].sort());
  });

  it("every core metric is present", () => {
    const missing = Object.entries(metrics)
      .filter(([n, m]) => m.tier === "core" && !emitted.has(n))
      .map(([n]) => n);
    expect(
      missing,
      "core metrics are the cross-runtime guarantee a shared dashboard relies on",
    ).toEqual([]);
  });

  it("emits no simsys_ metric the contract does not declare", () => {
    const undeclared = [...emitted.keys()].filter((n) => !(n in metrics)).sort();
    expect(
      undeclared,
      "add these to spec/metrics-contract.json and the README catalogues, or stop emitting them",
    ).toEqual([]);
  });

  it("NEGATIVE CONTROL: does not claim metrics it cannot emit", () => {
    // Without this, a contract listing every runtime on every metric would
    // satisfy the checks above and look complete while meaning nothing.
    for (const n of ["simsys_process_threads", "simsys_runtime_goroutines"]) {
      expect(metrics[n].runtimes).not.toContain("node");
      expect(emitted.has(n)).toBe(false);
    }
  });
});
