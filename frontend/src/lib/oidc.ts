/**
 * Signing in against the identity provider (ADR 0009).
 *
 * Authorization Code flow with PKCE. A browser application cannot keep a
 * secret — anything shipped here is readable by whoever is using it — so there
 * is no client secret. PKCE is what replaces it: the browser generates a random
 * verifier, sends only its SHA-256 hash to start the flow, and proves it holds
 * the original when redeeming the code. An intercepted code is useless without
 * the verifier, which never travels.
 *
 * Written directly rather than with a library. The flow is ~100 lines against
 * the Web Crypto API, every dependency in this project needs an ADR justifying
 * it, and a login library is a large amount of code in the most
 * security-sensitive path the console has.
 *
 * The tokens this obtains are verified by the API against the provider's public
 * keys. Nothing here decides who the user is; it only obtains the assertion and
 * hands it over.
 */

const ISSUER = process.env.NEXT_PUBLIC_OIDC_ISSUER ?? "";
const CLIENT_ID = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID ?? "eaip-console";

/** Whether a provider is configured. Empty issuer means the paste-a-token path. */
export function oidcConfigured(): boolean {
  return ISSUER.length > 0;
}

/**
 * The PKCE verifier and the state, between the redirect out and the return.
 *
 * sessionStorage, not localStorage: scoped to this tab and cleared when it
 * closes. These are single-use values that live for the seconds between leaving
 * for the provider and coming back.
 */
const VERIFIER_KEY = "eaip.pkce.verifier";
const STATE_KEY = "eaip.pkce.state";
const RETURN_KEY = "eaip.pkce.return";

/** Where the provider sends the browser back to. Must match the realm exactly. */
export function redirectUri(): string {
  return `${window.location.origin}/callback`;
}

function randomString(bytes = 32): string {
  const buffer = new Uint8Array(bytes);
  crypto.getRandomValues(buffer);
  return base64Url(buffer);
}

function base64Url(bytes: Uint8Array | ArrayBuffer): string {
  const view = bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : bytes;
  let binary = "";
  for (const byte of view) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(digest);
}

/**
 * Leave for the provider's login page.
 *
 * `state` is generated here and checked on return: it is what stops a third
 * party from feeding this tab an authorization code it did not ask for (CSRF
 * against the callback).
 */
export async function beginLogin(returnTo = "/agent"): Promise<void> {
  const verifier = randomString();
  const state = randomString(16);

  window.sessionStorage.setItem(VERIFIER_KEY, verifier);
  window.sessionStorage.setItem(STATE_KEY, state);
  window.sessionStorage.setItem(RETURN_KEY, returnTo);

  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    response_type: "code",
    scope: "openid",
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
  });

  // A full-page navigation to the identity provider, which is a different
  // origin. router.push() is for internal routes and cannot leave the app;
  // the lint rule assumes an internal destination and does not apply here.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`${ISSUER}/protocol/openid-connect/auth?${params}`);
}

export interface TokenSet {
  access_token: string;
  refresh_token?: string;
  expires_in: number;
  token_type: string;
}

/**
 * Redeem the code the provider sent back.
 *
 * Refuses if `state` does not match what we stored, which means this tab did
 * not start the flow. The verifier is deleted either way: it is single-use, and
 * a leftover one is a value an attacker would like to find.
 */
export async function completeLogin(params: URLSearchParams): Promise<TokenSet> {
  const verifier = window.sessionStorage.getItem(VERIFIER_KEY);
  const expectedState = window.sessionStorage.getItem(STATE_KEY);
  window.sessionStorage.removeItem(VERIFIER_KEY);
  window.sessionStorage.removeItem(STATE_KEY);

  const error = params.get("error");
  if (error) {
    throw new Error(params.get("error_description") ?? `Sign-in failed (${error}).`);
  }

  const code = params.get("code");
  const state = params.get("state");
  if (!code) throw new Error("The provider returned no authorization code.");
  if (!verifier) throw new Error("This sign-in was not started in this tab. Try again.");
  if (!state || state !== expectedState) {
    // Not a user-facing subtlety: a mismatched state means the code did not
    // come from a flow this tab began.
    throw new Error("Sign-in state did not match. Start again.");
  }

  const response = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      redirect_uri: redirectUri(),
      code,
      code_verifier: verifier,
    }),
  });

  if (!response.ok) {
    throw new Error("The provider refused to issue a token.");
  }
  return (await response.json()) as TokenSet;
}

/** Where the user was heading before being sent to log in. */
export function consumeReturnTo(): string {
  const target = window.sessionStorage.getItem(RETURN_KEY) ?? "/agent";
  window.sessionStorage.removeItem(RETURN_KEY);
  // Only same-site paths. An absolute URL here would make the callback an open
  // redirect, which is a phishing primitive.
  return target.startsWith("/") && !target.startsWith("//") ? target : "/agent";
}

/**
 * Exchange a refresh token for a new access token.
 *
 * Access tokens live five minutes so that disabling a user in the provider ends
 * their access promptly. That is only usable if the console renews quietly
 * rather than interrupting someone mid-sentence.
 */
export async function refresh(refreshToken: string): Promise<TokenSet> {
  const response = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      client_id: CLIENT_ID,
      refresh_token: refreshToken,
    }),
  });
  if (!response.ok) throw new Error("Session expired.");
  return (await response.json()) as TokenSet;
}

/**
 * End the session at the provider, not only in this tab.
 *
 * Clearing local storage alone leaves the provider's session cookie intact, so
 * the next sign-in would skip the password prompt — which looks like the logout
 * did not work, and on a shared machine means it did not.
 */
export function logoutUrl(idTokenHint?: string): string {
  const params = new URLSearchParams({
    post_logout_redirect_uri: `${window.location.origin}/login`,
    client_id: CLIENT_ID,
  });
  if (idTokenHint) params.set("id_token_hint", idTokenHint);
  return `${ISSUER}/protocol/openid-connect/logout?${params}`;
}
