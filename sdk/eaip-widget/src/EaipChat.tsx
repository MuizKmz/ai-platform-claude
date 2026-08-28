/**
 * <EaipChat /> — the drop-in.
 *
 *   import { EaipChat } from "@eaip/widget";
 *   <EaipChat basePath="/api/eaip" />
 *
 * `basePath` is a path on the HOST APP's own origin where `@eaip/proxy-endpoint`
 * is mounted. The widget never talks to EAIP or Keycloak directly — see
 * `docs/CHAT_WIDGET_SDK.md`.
 *
 * Styling is self-contained (one injected `<style>`, all rules scoped to
 * `.eaip-widget`, all values themeable via CSS custom properties). It does not
 * pull in a design system and does not leak styles onto the host page.
 */

import { useEffect, useRef, type CSSProperties, type KeyboardEvent } from "react";

import { ensureStyles } from "./styles.ts";
import { useEaipChat, type UseEaipChatOptions } from "./useEaipChat.ts";

export interface EaipChatProps extends UseEaipChatOptions {
  /** Placeholder for the input. */
  placeholder?: string;
  /** Shown in the empty state before the first message. */
  greeting?: string;
  /** Extra class on the root, for layout (sizing/position) from the host. */
  className?: string;
  /** Inline style on the root — the usual place to set width/height and the
   *  `--eaip-color-*` theming variables. */
  style?: CSSProperties;
}

const DEFAULT_GREETING =
  "Ask a question and the answer will come from this workspace's connected knowledge.";

export function EaipChat({
  placeholder = "Ask a question…",
  greeting = DEFAULT_GREETING,
  className,
  style,
  ...chatOptions
}: EaipChatProps) {
  useEffect(() => {
    ensureStyles();
  }, []);

  const { messages, status, pending, error, send } = useEaipChat(chatOptions);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function submit() {
    const el = inputRef.current;
    if (!el) return;
    const text = el.value;
    el.value = "";
    void send(text);
  }

  const rootClass = className ? `eaip-widget ${className}` : "eaip-widget";

  if (status === "not-configured") {
    return (
      <div className={rootClass} style={style} data-eaip-status="not-configured">
        <p className="eaip-widget__notice">
          This chat is not connected yet. The app’s administrator needs to finish
          the EAIP setup on the backend.
        </p>
      </div>
    );
  }

  if (status === "unauthorized") {
    return (
      <div className={rootClass} style={style} data-eaip-status="unauthorized">
        <p className="eaip-widget__notice">
          This chat’s credential was refused. The app’s administrator needs to
          check the EAIP connection.
        </p>
      </div>
    );
  }

  return (
    <div className={rootClass} style={style} data-eaip-status={status}>
      <div className="eaip-widget__log" ref={logRef} aria-live="polite">
        {messages.length === 0 && status === "ready" ? (
          <p className="eaip-widget__empty">{greeting}</p>
        ) : null}
        {messages.length === 0 && status === "checking" ? (
          <p className="eaip-widget__empty">Connecting…</p>
        ) : null}

        {messages.map((message, i) => (
          <div
            key={i}
            className={
              message.role === "user"
                ? "eaip-widget__msg eaip-widget__msg--user"
                : message.isError
                  ? "eaip-widget__msg eaip-widget__msg--error"
                  : "eaip-widget__msg eaip-widget__msg--assistant"
            }
          >
            {message.content}
          </div>
        ))}

        {pending ? (
          <div className="eaip-widget__pending">
            <span className="eaip-widget__dot" />
            Thinking…
          </div>
        ) : null}
      </div>

      {error ? (
        <p className="eaip-widget__error-line" role="alert">
          {error}
        </p>
      ) : null}

      <div className="eaip-widget__composer">
        <textarea
          ref={inputRef}
          className="eaip-widget__input"
          placeholder={placeholder}
          aria-label="Message"
          onKeyDown={onKeyDown}
          disabled={status !== "ready" || pending}
          rows={1}
        />
        <button
          type="button"
          className="eaip-widget__send"
          onClick={submit}
          disabled={status !== "ready" || pending}
        >
          Send
        </button>
      </div>
    </div>
  );
}
