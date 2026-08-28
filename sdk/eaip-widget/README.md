# @eaip/widget

A drop-in React chat surface for EAIP.

```tsx
import { EaipChat } from "@eaip/widget";

<div style={{ height: 480, width: 380 }}>
  <EaipChat basePath="/api/eaip" />
</div>
```

`basePath` is a path on your own origin where [`@eaip/proxy-endpoint`](../eaip-proxy-endpoint)
is mounted. The widget talks only to that — never to EAIP or Keycloak
directly. See [../../docs/CHAT_WIDGET_SDK.md](../../docs/CHAT_WIDGET_SDK.md).

Full install: [../SETUP.md](../SETUP.md).

## Props

All of `useEaipChat`'s options, plus:

| Prop | Default | |
|---|---|---|
| `basePath` | `/api/eaip` | where the proxy is mounted, on your origin |
| `greeting` | a generic line | shown before the first message |
| `placeholder` | `"Ask a question…"` | input placeholder |
| `tool` / `argument` | `search_knowledge` / `query` | which EAIP tool a message calls |
| `onExchange` | — | `({ question, ok }) => void`, for your analytics |
| `className` / `style` | — | layout and theming |

## Theming

One injected `<style>`, every rule scoped to `.eaip-widget`, every colour a
CSS custom property with a light/dark default. Override in `style`:

`--eaip-color-bg`, `--eaip-color-fg`, `--eaip-color-muted`,
`--eaip-color-accent`, `--eaip-color-user-bg`, `--eaip-color-border`,
`--eaip-color-error`, `--eaip-radius-base`, `--eaip-font-family`.

No CSS framework, no style leakage onto the host page.

## Building your own UI

```ts
import { useEaipChat } from "@eaip/widget";

const { messages, status, pending, error, send, reset } = useEaipChat({
  basePath: "/api/eaip",
});
```

`status` is `"checking" | "ready" | "not-configured" | "unauthorized"`. A
failed tool arrives as a `messages` entry with `isError: true` (shown, not
hidden — same as the EAIP console treats a refusal). A rate-limit / budget
error arrives in `error` with `status` staying `"ready"`.

## Peer dependencies

`react` and `react-dom` (>=18).
