/**
 * Express adapter. The whole thing:
 *
 *   import express from "express";
 *   import { eaipProxyRouter } from "@eaip/proxy-endpoint/express";
 *
 *   const app = express();
 *   app.use("/api/eaip", eaipProxyRouter());   // reads config from process.env
 *
 * That mounts:
 *   POST /api/eaip/session
 *   POST /api/eaip/mcp
 *
 * which is exactly what `<EaipChat basePath="/api/eaip" />` calls.
 *
 * `express` is a peer dependency and imported lazily, so `@eaip/proxy-endpoint`
 * can be used without Express (via `createEaipProxy` directly) with no install.
 */

import { createRequire } from "node:module";

import { configFromEnv, type EaipProxyConfig } from "./config.ts";
import { createEaipProxy, type EaipProxy } from "./handler.ts";

const require = createRequire(import.meta.url);

// Minimal structural types so this file does not need @types/express to
// typecheck. The real objects Express passes satisfy these.
interface ExpressReq {
  body: unknown;
}
interface ExpressRes {
  status(code: number): ExpressRes;
  json(body: unknown): void;
}
type ExpressNext = (err?: unknown) => void;
interface ExpressRouter {
  post(path: string, handler: (req: ExpressReq, res: ExpressRes, next: ExpressNext) => void): void;
  use(handler: (req: ExpressReq, res: ExpressRes, next: ExpressNext) => void): void;
}

export interface EaipProxyRouterOptions {
  /** Provide config explicitly instead of reading `process.env`. */
  config?: EaipProxyConfig;
  /** Injected for testing. */
  fetch?: typeof fetch;
  /**
   * What to do when config is missing at mount time. `"mount-503"` (default)
   * mounts the routes anyway; they return 503 so the widget shows "not
   * configured". `"throw"` fails the server's startup instead.
   */
  onMissingConfig?: "mount-503" | "throw";
}

export function eaipProxyRouter(options: EaipProxyRouterOptions = {}): ExpressRouter {
  // Lazy require so the peer dep is only needed if this adapter is used.
  const express = loadExpress();
  const router: ExpressRouter = express.Router();

  let proxy: EaipProxy | null = null;
  let missing: string[] = [];

  if (options.config) {
    proxy = createEaipProxy(options.config, options.fetch);
  } else {
    const result = configFromEnv();
    if ("config" in result) {
      proxy = createEaipProxy(result.config, options.fetch);
    } else {
      missing = result.missing;
      if (options.onMissingConfig === "throw") {
        throw new Error(
          `@eaip/proxy-endpoint: missing required environment variables: ${missing.join(", ")}`,
        );
      }
      // eslint-disable-next-line no-console
      console.warn(
        `@eaip/proxy-endpoint: not configured (${missing.join(", ")}). ` +
          `Routes will return 503 until these are set.`,
      );
    }
  }

  const bodyJson = express.json();
  router.use(bodyJson as never);

  router.post("/session", (req, res, next) => {
    if (!proxy) {
      res.status(503).json({ error: `Not configured: ${missing.join(", ")}` });
      return;
    }
    proxy
      .session({ body: req.body })
      .then((out) => res.status(out.status).json(out.body))
      .catch(next);
  });

  router.post("/mcp", (req, res, next) => {
    if (!proxy) {
      res.status(503).json({ error: `Not configured: ${missing.join(", ")}` });
      return;
    }
    proxy
      .mcp({ body: req.body })
      .then((out) => res.status(out.status).json(out.body))
      .catch(next);
  });

  return router;
}

function loadExpress(): {
  Router: () => ExpressRouter;
  json: () => unknown;
} {
  try {
    const mod = require("express") as {
      Router: () => ExpressRouter;
      json: () => unknown;
    };
    return mod;
  } catch {
    throw new Error(
      "@eaip/proxy-endpoint/express requires `express` to be installed. " +
        "Run `npm install express`, or use `createEaipProxy` from the package root with your own framework.",
    );
  }
}
