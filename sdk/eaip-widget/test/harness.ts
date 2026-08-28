/**
 * Mount a hook and drive it, without JSX (Node's native TS support does not
 * cover `.tsx`). A probe component built with `createElement` calls the hook
 * and copies its return value to a mutable ref the test reads.
 *
 * The widget's own presentational shell (`EaipChat.tsx`) is exercised by the
 * SETUP.md manual smoke and, later, a Playwright E2E — consistent with ADR 0006
 * and `docs/CHAT_WIDGET_SDK.md`'s testing note. Everything with real logic is
 * in this hook and is covered here.
 */

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

export interface HookHarness<T> {
  /** The hook's latest return value. */
  current: T;
  /** Run a callback (usually one that calls a returned function) inside act(). */
  act: (fn: () => void | Promise<void>) => Promise<void>;
  unmount: () => void;
}

export async function mountHook<T>(useHook: () => T): Promise<HookHarness<T>> {
  const box: { current: T } = { current: undefined as T };

  function Probe() {
    box.current = useHook();
    return null;
  }

  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root;

  await act(async () => {
    root = createRoot(container);
    root.render(createElement(Probe));
  });

  return {
    get current() {
      return box.current;
    },
    async act(fn) {
      await act(async () => {
        await fn();
      });
    },
    unmount() {
      act(() => root.unmount());
      container.remove();
    },
  };
}

export async function waitFor(
  predicate: () => boolean,
  { timeout = 1000, interval = 10 }: { timeout?: number; interval?: number } = {},
): Promise<void> {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeout) {
      throw new Error("waitFor: condition not met within timeout");
    }
    await act(async () => {
      await new Promise((r) => setTimeout(r, interval));
    });
  }
}
