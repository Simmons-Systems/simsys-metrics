/**
 * @simsys/metrics — Drop-in Prometheus /metrics for Node.js web apps.
 *
 * Public API:
 *
 *   install(app, { service, version })     // auto-detects Express or Hono
 *   trackQueue(name, { depthFn })
 *   trackJob(name)(fn)                     // function wrapper
 *   trackJob(name).run(() => ...)          // ad-hoc async span
 *   safeLabel(value, allowedSet)
 *
 * See https://github.com/Avicennasis/simsys-metrics (Python reference) for
 * the canonical metric catalogue and cardinality rules.
 */

export {
  trackQueue,
  trackPool,
  trackJob,
  safeLabel,
  setService,
  type TrackQueueOpts,
  type TrackPoolOpts,
  type JobTracker,
} from "./baseline.js";

export {
  trackProgress,
  type ProgressOpts,
  type ProgressTracker,
} from "./progress.js";

export { registry, PREFIX } from "./registry.js";

export { detectCommit } from "./buildinfo.js";

import {
  installExpress,
  isExpressApp,
  type ExpressInstallOpts,
  EXEMPT_PATHS as EXPRESS_EXEMPT,
} from "./adapters/express.js";
import {
  installHono,
  isHonoApp,
  type HonoInstallOpts,
  EXEMPT_PATHS as HONO_EXEMPT,
} from "./adapters/hono.js";

export {
  installNext,
  bucketRoute,
  type NextInstallOpts,
  type RouteTemplate,
} from "./adapters/next.js";

// Re-export for advanced consumers who want to pin to a specific adapter.
export {
  installExpress,
  installHono,
  isExpressApp,
  isHonoApp,
};

export const EXEMPT_PATHS = EXPRESS_EXEMPT; // identical to HONO_EXEMPT

export interface InstallOpts {
  service: string;
  version: string;
  commit?: string;
  metricsPath?: string;
}

/**
 * Install simsys baseline metrics on ``app``. Framework is auto-detected
 * between Express 5 and Hono 4. Throws if neither duck-type matches.
 *
 * Returns the same app for chaining.
 */
export function install<T>(app: T, opts: InstallOpts): T {
  if (!opts || typeof opts !== "object") {
    throw new Error("install(): opts { service, version } required.");
  }
  if (!opts.service || typeof opts.service !== "string") {
    throw new Error("install(): opts.service must be a non-empty string.");
  }
  if (!opts.version || typeof opts.version !== "string") {
    throw new Error("install(): opts.version must be a non-empty string.");
  }

  if (isExpressApp(app)) {
    return installExpress(app, opts as ExpressInstallOpts) as T;
  }
  if (isHonoApp(app)) {
    return installHono(app, opts as HonoInstallOpts) as T;
  }
  throw new TypeError(
    "simsys-metrics install(): unrecognised app object. " +
      "Expected an Express 5 application (with .use / .get / .handle / .listen) " +
      "or a Hono 4 application (with .route / .fetch / .get). " +
      "If you're using a different framework, file an issue or use the " +
      "adapter-specific installHono() / installExpress() directly.",
  );
}

// HONO_EXEMPT is the same set; re-export under its own name for symmetry.
export { HONO_EXEMPT, EXPRESS_EXEMPT };
