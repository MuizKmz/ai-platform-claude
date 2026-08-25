# ADR 0009 — Keycloak as the identity provider

**Status:** Accepted
**Date:** 2026-08-25
**Phase:** 11 — production deployment

## Context

Tokens are minted by a CLI command against a symmetric secret. `issue_token`
performs **no credential check of any kind** — it signs whatever it is told. The
docstring has said "Phase 8 replaces it" since Phase 1; Phase 8 turned out to be
hardening, and identity is this phase.

That is correct for one developer and unacceptable for a second person. What is
missing is not encryption — the tokens are properly signed and verified — but
every process around them:

- **No credential check.** Possessing the CLI is possessing every identity.
- **No revocation.** A leaked token is valid until it expires. There is no way
  to disable a person who has left.
- **No lifecycle.** No password reset, no enrolment, no MFA, no session record.
- **One secret for every identity.** `JWT_SECRET` mints tokens for any tenant,
  any role, any label set. It is the single most dangerous value in `.env`, and
  it must be readable by the API in order to *verify*, not only to sign.

That last point is the structural flaw. HS256 is symmetric: the verifier holds
the same key the issuer holds. Anything that can check a token can forge one.

The pilot deployment (`infra/DEPLOY-aapanel.md`) names this as the blocker for
connecting MES and WMS, and it is the reason the pilot is one person on an SSH
tunnel rather than a team on a URL.

## Decision

**Keycloak**, self-hosted in `docker-compose`, as the OIDC provider. The API
verifies **RS256** tokens against Keycloak's **JWKS** endpoint.

Signing moves out of this codebase entirely. The API keeps a public key only,
so a compromised API host cannot mint an identity — it can only check one.

## Why this is a small change to the application

The Phase 1 design anticipated it, deliberately. `core/security.py` says:

> Claims are deliberately OIDC-shaped (sub, iss, aud, exp, iat) so a real
> identity provider can replace this issuer without the verification path or
> the Principal changing — only the key source moves to JWKS.

That prediction holds. The blast radius is:

| Changes | Does not change |
|---|---|
| `principal_from_token` — key source and algorithm | `Principal` — same shape, same fields |
| A new JWKS client with a cached key set | `get_principal` — still the only place a Principal is made |
| Claim mapping: Keycloak's names → ours | Every endpoint, every RLS policy, every tool authorization |
| `issue_token` becomes development-only | Security invariant #1 — tenancy still server-derived |

No route, no query, and no authorization check is touched. That is the whole
argument for having shaped the claims this way four phases before needing to.

## Alternatives considered

| Option | Why not |
|---|---|
| **Microsoft Entra ID** | The strongest option *if* the organisation is already on Microsoft 365 — staff sign in with existing accounts and there is no user directory to run. Rejected for now because it makes an on-prem platform depend on an external tenant we do not control, and the systems this serves (ERP, WMS, MES, IoT) are on-prem. Worth revisiting: the OIDC verification path built here works against Entra unchanged, which is the point of using standards. |
| **Auth0 / Okta** | Hosted, excellent, and priced per monthly active user. For an internal platform behind a company network the recurring cost buys convenience we can self-host, and it puts staff identity in a third party. |
| **Authentik / Zitadel** | Both good, both lighter than Keycloak. Rejected on operational maturity: Keycloak is Red Hat-backed, has a decade of production use, and its failure modes are searchable. For the component that gates *all* access, boring and well-documented beats modern. |
| **Roll our own login** | We already have. That is what this ADR replaces. Password storage, reset flows, MFA, lockout, and session management are a security speciality; writing them is how the CVE happens. |
| **Keep CLI tokens, add a password check** | Half a login system with none of the lifecycle. No revocation, no MFA, no audit — and the symmetric-key flaw remains untouched. |

## The security consequence, and it is the point of this ADR

**Verification stops being able to forge.**

Today `JWT_SECRET` both signs and verifies. An attacker reading it — from the
API host, a backup, a log, an image layer — can mint a token for any tenant with
the `admin` role and every label. There is no detection, because the token is
genuinely valid.

After this change the API holds a **public** key fetched from JWKS. It can
verify a signature and cannot produce one. Forging requires Keycloak's private
key, which never leaves Keycloak.

Three rules the implementation must hold, each an audited failure elsewhere:

1. **`algorithms=["RS256"]`, never the token's own `alg` header.** Accepting
   `alg` from the token is the classic JWT vulnerability — `alg: none` skips
   verification, and `alg: HS256` against an RSA public key lets an attacker
   sign with a value they can read. The current code already pins its algorithm
   for exactly this reason; the list changes, the pinning does not.
2. **Audience and issuer stay verified.** The MCP server's separate audience
   (RFC 8707, ADR 0007) is load-bearing: a console token must remain unusable
   as a machine credential. Keycloak issues both; the check is unchanged.
3. **JWKS is cached, with a bounded refresh.** An uncached fetch makes Keycloak
   a per-request dependency and a denial-of-service surface. An unrefreshed
   cache breaks every login the moment keys rotate. Cache with a TTL, and
   refresh on an unknown `kid`.

**Tenancy remains server-derived.** `tenant_id` moves from a claim we mint to a
claim Keycloak asserts, and it is still read only from a verified token —
never a body, header, or model output. The mapping is configured in Keycloak
and verified by the API; a token without it is refused rather than defaulted,
because "no tenant" must never come to mean "all tenants".

## Consequences

**Positive:**

- Real login: passwords, MFA, reset, enrolment, lockout — none of it our code.
- **Revocation exists.** Disabling a user in Keycloak ends their access.
- The API can no longer mint identities. Signing key compromise requires
  compromising Keycloak specifically.
- Standard OIDC, so replacing Keycloak with Entra later is configuration.
- Unblocks multi-user, and therefore MES, ERP, and WMS.

**Negative / accepted costs:**

- **A fourth container**, and a stateful one. Keycloak needs its own database
  and its own backup — a restore that recovers EAIP but not Keycloak recovers
  a platform nobody can log into.
- **It is now a hard dependency of every request.** JWKS caching keeps a brief
  Keycloak outage from becoming an EAIP outage; a long one still is.
- **Memory.** Keycloak wants ~512MB. The pilot host budget in
  `docker-compose.prod.yml` must be revisited before it is deployed there, not
  after — the deploy runbook's governing constraint is that nothing already on
  that host may be disturbed.
- **Configuration becomes security-critical.** A realm with the wrong claim
  mapper issues tokens missing `tenant_id`. Refusing them (not defaulting) is
  what keeps that a broken login rather than a tenancy breach.
- **Development gets a login step.** `issue_token` stays for tests, gated so it
  cannot be reached when `APP_ENV=production`.

## How we would remove it

Verification is one function and one key source. Point `OIDC_JWKS_URL` at a
different provider's endpoint and adjust the claim mapping; nothing else in the
application knows which IdP issued the token. Reverting to CLI tokens means
restoring the HS256 branch in `principal_from_token` — kept for the test suite,
so it does not need rewriting.

`Principal` is unchanged by this ADR, and that is the property worth preserving:
it is why an identity provider could arrive in Phase 11 instead of Phase 1.
