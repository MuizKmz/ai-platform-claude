/**
 * The widget's styling, as one scoped stylesheet injected once.
 *
 * Why a `<style>` string and not a CSS file or a CSS-in-JS library:
 *
 *   - The widget lands in someone else's app. It cannot assume a bundler that
 *     handles CSS imports, and it must not pull in a styling runtime.
 *   - Every rule is under `.eaip-widget`, and every value is a CSS custom
 *     property with a fallback, so a host can retheme it with four variables
 *     and nothing here leaks out to their page.
 *   - `color-scheme` + the `prefers-color-scheme` block mean it follows the
 *     host's light/dark without configuration.
 */

export const STYLE_ELEMENT_ID = "eaip-widget-styles";

export const STYLESHEET = `
.eaip-widget {
  --eaip-bg: var(--eaip-color-bg, #ffffff);
  --eaip-fg: var(--eaip-color-fg, #1a1a1a);
  --eaip-muted: var(--eaip-color-muted, #6b7280);
  --eaip-accent: var(--eaip-color-accent, #2563eb);
  --eaip-user-bg: var(--eaip-color-user-bg, #eef2ff);
  --eaip-border: var(--eaip-color-border, rgba(0, 0, 0, 0.1));
  --eaip-error: var(--eaip-color-error, #b91c1c);
  --eaip-radius: var(--eaip-radius-base, 10px);
  --eaip-font: var(--eaip-font-family, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);

  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--eaip-bg);
  color: var(--eaip-fg);
  font-family: var(--eaip-font);
  font-size: 14px;
  line-height: 1.5;
  border: 1px solid var(--eaip-border);
  border-radius: var(--eaip-radius);
  overflow: hidden;
}

.eaip-widget * { box-sizing: border-box; }

.eaip-widget__log {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.eaip-widget__empty {
  color: var(--eaip-muted);
  font-size: 13px;
  margin: auto 0;
  text-align: center;
  padding: 0 8px;
}

.eaip-widget__msg {
  max-width: 85%;
  padding: 8px 12px;
  border-radius: var(--eaip-radius);
  white-space: pre-wrap;
  word-wrap: break-word;
}

.eaip-widget__msg--user {
  align-self: flex-end;
  background: var(--eaip-user-bg);
}

.eaip-widget__msg--assistant {
  align-self: flex-start;
  background: transparent;
  border: 1px solid var(--eaip-border);
}

.eaip-widget__msg--error {
  align-self: flex-start;
  background: transparent;
  border: 1px solid var(--eaip-error);
  color: var(--eaip-error);
}

.eaip-widget__pending {
  align-self: flex-start;
  color: var(--eaip-muted);
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.eaip-widget__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: eaip-pulse 1.2s ease-in-out infinite;
}

@keyframes eaip-pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

.eaip-widget__notice {
  margin: auto;
  color: var(--eaip-muted);
  font-size: 13px;
  text-align: center;
  padding: 24px 16px;
}

.eaip-widget__error-line {
  color: var(--eaip-error);
  font-size: 13px;
  padding: 0 16px 8px;
}

.eaip-widget__composer {
  border-top: 1px solid var(--eaip-border);
  padding: 8px;
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.eaip-widget__input {
  flex: 1;
  resize: none;
  border: 1px solid var(--eaip-border);
  border-radius: calc(var(--eaip-radius) - 2px);
  padding: 8px 10px;
  font: inherit;
  color: inherit;
  background: var(--eaip-bg);
  max-height: 120px;
  min-height: 38px;
}

.eaip-widget__input:focus {
  outline: 2px solid var(--eaip-accent);
  outline-offset: -1px;
}

.eaip-widget__send {
  border: none;
  border-radius: calc(var(--eaip-radius) - 2px);
  background: var(--eaip-accent);
  color: #ffffff;
  font: inherit;
  font-weight: 500;
  padding: 8px 14px;
  cursor: pointer;
  height: 38px;
}

.eaip-widget__send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (prefers-color-scheme: dark) {
  .eaip-widget {
    --eaip-bg: var(--eaip-color-bg, #1a1a1a);
    --eaip-fg: var(--eaip-color-fg, #f3f4f6);
    --eaip-muted: var(--eaip-color-muted, #9ca3af);
    --eaip-user-bg: var(--eaip-color-user-bg, #1e293b);
    --eaip-border: var(--eaip-color-border, rgba(255, 255, 255, 0.14));
    --eaip-error: var(--eaip-color-error, #f87171);
  }
}
`.trim();

/** Inject the stylesheet once per document. No-op if already present or if
 *  there is no document (SSR). */
export function ensureStyles(): void {
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ELEMENT_ID)) return;
  const el = document.createElement("style");
  el.id = STYLE_ELEMENT_ID;
  el.textContent = STYLESHEET;
  document.head.appendChild(el);
}
