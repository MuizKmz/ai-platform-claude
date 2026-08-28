/**
 * @eaip/widget — a drop-in React chat surface for EAIP.
 *
 * `<EaipChat basePath="/api/eaip" />` is the whole API for most integrations.
 * `useEaipChat` is exported for building a custom UI on the same logic.
 *
 * See `sdk/SETUP.md` and `docs/CHAT_WIDGET_SDK.md`.
 */

export { EaipChat, type EaipChatProps } from "./EaipChat.tsx";
export {
  useEaipChat,
  type UseEaipChat,
  type UseEaipChatOptions,
  type EaipMessage,
  type EaipStatus,
} from "./useEaipChat.ts";
export { STYLESHEET, ensureStyles } from "./styles.ts";

// Re-exported so a consumer can catch these without a direct @eaip/client dep.
export {
  EaipError,
  EaipNotConfiguredError,
  EaipAuthError,
  EaipToolError,
} from "@eaip/client";
