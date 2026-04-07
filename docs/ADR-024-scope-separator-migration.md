# ADR-024: OAuth Scope Separator Migration (colon to dot)

**Status:** Accepted
**Date:** 2026-04-07
**Supersedes:** Scope naming conventions in ADR-004, ADR-009, ADR-011

## Context

The MCP server defines application-level OAuth scopes using the `resource:action`
pattern (e.g., `notes:read`, `calendar:write`). While the colon separator is
visually intuitive and used by some OAuth implementations, it causes
interoperability problems with many identity providers.

### IDP Compatibility Issues

Several widely-deployed identity providers reject or mishandle colons in OAuth
scope names:

- **Keycloak**: Accepts colons but requires special configuration for scope
  mappers; colons can conflict with realm-qualified scope names
- **Auth0**: Permits colons but treats them as namespace delimiters in their
  Resource Server API, leading to unexpected scope resolution behavior
- **Azure AD / Entra ID**: Uses colons internally for delegated permissions
  (e.g., `User.Read`) and may reject custom scopes containing colons
- **AWS Cognito**: Restricts scope names to alphanumeric characters, hyphens,
  periods, and underscores — colons are not allowed
- **Okta**: Custom scopes are restricted to `[a-zA-Z0-9._-]`; colons are
  explicitly rejected

### RFC References

The OAuth 2.0 framework (RFC 6749, Section 3.3) defines scope values as:

> scope-token = 1*( %x21 / %x23-5B / %x5D-7E )

This technically permits the colon character (`%x3A`), so colons are
spec-compliant. However, the specification also notes:

> The authorization server MAY fully or partially ignore the scope requested by
> the client, based on the authorization server policy or the resource owner's
> instructions.

In practice, many authorization servers impose stricter character restrictions
than the RFC minimum. The dot separator (`.`) is universally accepted across all
major OAuth/OIDC implementations and is the de facto convention used by:

- Microsoft Identity Platform (`User.Read`, `Mail.Send`)
- Google OAuth (`https://www.googleapis.com/auth/calendar.readonly`)
- MCP specification examples in RFC 9728 (OAuth Protected Resource Metadata)

### RFC 9728 (OAuth Protected Resource Metadata)

RFC 9728 defines the Protected Resource Metadata endpoint used by this server
(`/.well-known/oauth-protected-resource`). While the RFC does not mandate a
specific scope naming convention, its examples and the broader OAuth ecosystem
favor dot-separated scopes for maximum interoperability.

## Decision

Replace the colon (`:`) separator with a dot (`.`) in all application-level
OAuth scope names:

| Before | After |
|--------|-------|
| `notes:read` | `notes.read` |
| `notes:write` | `notes.write` |
| `calendar:read` | `calendar.read` |
| `calendar:write` | `calendar.write` |
| `todo:read` | `todo.read` |
| `todo:write` | `todo.write` |
| `contacts:read` | `contacts.read` |
| `contacts:write` | `contacts.write` |
| `files:read` | `files.read` |
| `files:write` | `files.write` |
| `tables:read` | `tables.read` |
| `tables:write` | `tables.write` |
| `deck:read` | `deck.read` |
| `deck:write` | `deck.write` |
| `cookbook:read` | `cookbook.read` |
| `cookbook:write` | `cookbook.write` |
| `sharing:read` | `sharing.read` |
| `sharing:write` | `sharing.write` |
| `news:read` | `news.read` |
| `news:write` | `news.write` |
| `collectives:read` | `collectives.read` |
| `collectives:write` | `collectives.write` |
| `semantic:read` | `semantic.read` |

Standard OIDC scopes (`openid`, `profile`, `email`, `offline_access`) are
unchanged — they are defined by OIDC Core and do not use separators.

## Consequences

### Positive

- **Universal IDP compatibility**: Dot-separated scopes work with every major
  identity provider without special configuration
- **Industry alignment**: Matches the naming convention used by Microsoft,
  Google, and other major OAuth implementations
- **No logic changes**: The authorization system uses string comparison and
  `startswith()` prefix matching — changing the separator character requires no
  algorithmic changes

### Negative

- **Breaking change**: Existing OAuth clients, stored tokens, and IDP
  configurations must be updated to use the new scope names
- **Migration required**: An Alembic database migration updates stored scope
  strings in `app_passwords` and `login_flow_sessions` tables

### Migration

- **Database**: Alembic migration `004` handles `REPLACE(scopes, ':', '.')`
  on stored scope JSON
- **Keycloak**: The realm export (`keycloak/realm-export.json`) has been updated;
  existing Keycloak deployments must re-import or manually update scope
  definitions
- **Nextcloud OIDC app**: The `astrolabe` OAuth client hook
  (`26-configure-astrolabe-oauth.sh`) has been updated with new scope names
- **Existing MCP clients**: Must update their scope requests to use dot
  separators; old colon-separated scope requests will be rejected

## Alternatives Considered

### Hyphen separator (`notes-read`)

Rejected: While universally compatible, hyphens are commonly used within scope
component names (e.g., hypothetical `file-share.read`), creating ambiguity about
which hyphen is the separator.

### Underscore separator (`notes_read`)

Rejected: Also universally compatible but less conventional in the OAuth
ecosystem. Dot is the dominant separator in industry practice.

### Keep colons with IDP-specific workarounds

Rejected: Requires per-IDP configuration, documentation, and ongoing maintenance.
The root cause is a poor separator choice, not an IDP deficiency.
