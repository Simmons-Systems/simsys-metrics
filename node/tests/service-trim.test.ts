/**
 * `service` is trimmed, so it stays a usable join key with simsys-logevent.
 *
 * An operator pivots from simsys_http_requests_total{service="portal"} in
 * Prometheus to {service="portal"} | json in Loki using the same label
 * value, so both packages must agree on what that value IS. simsys-logevent
 * trims its own copy; before this, metrics did not, so "  portal  " here and
 * "portal" there were two identities and the pivot returned nothing --
 * silently, because an empty Loki result looks exactly like a quiet service.
 *
 * Trimming was not deferred to a major because it is a measured no-op: all
 * 36 services reporting simsys_build_info were queried from live Prometheus
 * on 2026-08-22 and none carried surrounding whitespace.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setService, _peekService, _resetForTests } from "../src/baseline.js";

describe("setService trims the service label", () => {
  let warn: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    _resetForTests();
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    warn.mockRestore();
    _resetForTests();
  });

  it("strips surrounding whitespace", () => {
    setService("  portal  ");
    expect(_peekService()).toBe("portal");
  });

  it("resolves padded and unpadded spellings to one identity", () => {
    // The actual join-breaking scenario, asserted directly.
    setService("  portal  ");
    const padded = _peekService();
    _resetForTests();
    setService("portal");
    expect(padded).toBe(_peekService());
  });

  it("preserves internal whitespace", () => {
    // Negative control: passing this while ALSO stripping inner spaces would
    // mean the trim is rewriting names rather than normalizing their edges.
    setService("  my service  ");
    expect(_peekService()).toBe("my service");
  });

  it("leaves an ordinary service name untouched", () => {
    // The no-op case -- what all 36 live services look like.
    setService("portal");
    expect(_peekService()).toBe("portal");
    expect(warn).not.toHaveBeenCalled();
  });

  it("warns when it had to trim", () => {
    setService("  portal  ");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("whitespace"));
  });

  it("warns on an all-whitespace service but still sets it", () => {
    // Warn-now, throw-in-the-next-major. Asserting the value is STILL SET
    // pins today's lenient behavior, so promoting this to a throw later is a
    // deliberate edit to this test rather than an accident.
    setService("   ");
    expect(_peekService()).toBe("");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("empty after"));
  });

  it("stays silent when clearing with null", () => {
    // install() rollback passes null; it must not warn or throw.
    setService("portal");
    warn.mockClear();
    setService(null);
    expect(_peekService()).toBeNull();
    expect(warn).not.toHaveBeenCalled();
  });
});
