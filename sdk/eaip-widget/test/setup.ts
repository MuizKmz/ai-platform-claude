/**
 * A DOM for the component tests. Loaded via `--import` before the test files.
 *
 * jsdom is a dev-only dependency of this package: the widget ships no test
 * runtime, and the framework-agnostic conversation logic in `@eaip/client` is
 * tested there without a DOM at all. This is only so `<EaipChat />` can be
 * mounted and asserted on.
 */

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://host-app.test/",
  pretendToBeVisual: true,
});

const g = globalThis as unknown as Record<string, unknown>;
g.window = dom.window;
g.document = dom.window.document;
g.HTMLElement = dom.window.HTMLElement;
g.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
g.Node = dom.window.Node;
g.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
// `navigator` is a read-only global in Node 24+; Node provides its own and
// React 19 is happy with it. jsdom's window keeps its own `navigator` too.

// React 19's scheduler checks for this.
g.IS_REACT_ACT_ENVIRONMENT = true;
