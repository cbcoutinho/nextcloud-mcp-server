# OAuth Architecture Comparison: MCP Server Authentication Patterns

This document compares three authentication architectures for the MCP server, explaining the evolution from pass-through authentication to true offline access capabilities.

## Pattern 1: Pass-Through Authentication (Current Implementation)

### Architecture
```
┌─────────────┐     OAuth Flow    ┌─────────────┐
│  MCP Client │◄──────────────────│   OAuth     │
│   (Claude)  │                   │  Provider   │
└──────┬──────┘                   └─────────────┘
       │
       │ Access Token
       │ (per request)
       ▼
┌─────────────┐                   ┌─────────────┐
│ MCP Server  │───────────────────►│  Nextcloud  │
│(Pass-through)                   │    APIs     │
└─────────────┘                   └─────────────┘
```

### Characteristics
| Aspect | Description |
|--------|-------------|
| **Token Flow** | MCP Client → MCP Server → Nextcloud |
| **Token Storage** | None (tokens exist only during request) |
| **Offline Access** | ❌ Impossible |
| **Background Workers** | ❌ Not supported |
| **User Consent** | Single OAuth flow (client-managed) |
| **Complexity** | Low |
| **Security** | High (no token persistence) |

### How It Works
1. MCP Client performs OAuth with provider
2. Client includes access token in each MCP request
3. MCP Server validates token and forwards to Nextcloud
4. Token discarded after request completes

### Limitations
- No operations possible without active MCP session
- Background sync/indexing impossible
- Cannot refresh tokens independently

---

## Pattern 2: Token Exchange Delegation (ADR-002 - Flawed)

### Architecture
```
┌─────────────┐                    ┌─────────────┐
│ MCP Client  │────────────────────│   OAuth     │
│  (Claude)   │                    │  Provider   │
└──────┬──────┘                    └──────┬──────┘
       │                                   │
       │ Access Token                      │ Service Account Token
       ▼                                   ▼
┌─────────────────────────────────────────────┐
│            MCP Server                        │
│  ┌────────────────────────────────────┐     │
│  │ Token Exchange (RFC 8693)          │     │
│  │ Subject: Service Account           │     │
│  │ Target: User                       │     │
│  └────────────────────────────────────┘     │
└───────────────┬─────────────────────────────┘
                │ Exchanged Token
                ▼
         ┌─────────────┐
         │  Nextcloud  │
         │    APIs     │
         └─────────────┘
```

### Characteristics
| Aspect | Description |
|--------|-------------|
| **Token Flow** | Service Account → Exchange → User Token |
| **Token Storage** | None (MCP server still stateless) |
| **Offline Access** | ❌ Still impossible (circular dependency) |
| **Background Workers** | ❌ Requires service account (rejected) |
| **User Consent** | Implicit through service account |
| **Complexity** | High |
| **Security** | ⚠️ Service accounts violate OAuth principles |

### Why It Fails
1. **Circular Dependency**: To exchange tokens, you need a token to exchange
2. **Service Account Problem**: Creates Nextcloud user identity for service
3. **OAuth Violation**: Service acts as itself, not on behalf of users
4. **No Bootstrap**: Still can't obtain initial tokens offline

### The Fatal Flaw
```
Q: How does background worker get tokens?
A: Use token exchange with service account

Q: How does service account get authorized?
A: Client credentials grant creates user account (violates OAuth)

Q: Can we use user's refresh token?
A: MCP server never sees refresh tokens (by design)
```

---

## Pattern 3: Sign-in with Nextcloud (Previous ADR-004 Draft)

### Architecture
```
┌─────────────┐                      ┌─────────────────┐                     ┌────────────┐
│  MCP Client ├───────────────────>  │   MCP Server    ├────────────────────>│ Nextcloud  │
│  (Claude)   │  (MCP Protocol)      │  (OAuth Client) │   (OIDC + APIs)     │   (IdP)    │
└─────────────┘                      └─────────────────┘                     └────────────┘
                                             │
                                      ┌──────▼────────┐
                                      │ Token Storage │
                                      │ (NC Tokens)   │
                                      └───────────────┘
```

### Characteristics
| Aspect | Description |
|--------|-------------|
| **Token Flow** | MCP Server uses Nextcloud as identity provider |
| **Token Storage** | ✅ Encrypted Nextcloud refresh tokens |
| **Offline Access** | ✅ Full support |
| **Background Workers** | ✅ Use stored refresh tokens |
| **User Consent** | Single OAuth flow (Nextcloud only) |
| **Complexity** | Medium |
| **Security** | High (with token rotation) |

### How It Works
1. **Initial Setup**:
   - User tries to use MCP tool
   - MCP server returns auth required
   - User authenticates with Nextcloud's OIDC endpoint
   - Nextcloud may use user_oidc to delegate to external IdP (Keycloak, etc.)
   - MCP server stores Nextcloud-issued refresh token (encrypted)

2. **Subsequent Requests**:
   - MCP server uses stored Nextcloud tokens
   - Refreshes automatically when expired
   - No client involvement needed

3. **Background Operations**:
   - Worker retrieves stored refresh token
   - Refreshes with Nextcloud directly
   - Performs operations independently

### Advantages
- ✅ Single sign-on with Nextcloud
- ✅ True offline access capability
- ✅ OAuth-compliant with proper consent
- ✅ Supports external IdPs via user_oidc
- ✅ Simpler integration - only one OAuth endpoint

### Trade-offs
- Authentication flows through Nextcloud
- Nextcloud manages IdP relationships (via user_oidc)
- MCP server only knows about Nextcloud, not the underlying IdP

---

## Pattern 4: Federated Authentication Architecture (ADR-004 - Solution)

### Architecture
```
┌─────────────┐                ┌─────────────────┐                ┌──────────────┐              ┌────────────┐
│  MCP Client │◄──────401──────│   MCP Server    │◄────OAuth──────│  Shared IdP  │──Validates──►│ Nextcloud  │
│  (Claude)   │                │  (OAuth Client) │   (On-Behalf)  │  (Keycloak)  │   Tokens     │(Resource)  │
└─────────────┘                └─────────────────┘                └──────────────┘              └────────────┘
                                        │
                                ┌───────▼────────┐
                                │ Token Storage  │
                                │ (IdP Tokens)   │
                                └────────────────┘
```

### Characteristics
| Aspect | Description |
|--------|-------------|
| **Token Flow** | Shared IdP issues tokens for Nextcloud access |
| **Token Storage** | ✅ Encrypted IdP refresh tokens |
| **Offline Access** | ✅ Full support |
| **Background Workers** | ✅ Use stored IdP refresh tokens |
| **User Consent** | Single OAuth flow (IdP manages consent) |
| **Complexity** | Medium-High |
| **Security** | Highest (enterprise-grade IdP) |

### How It Works
1. **Initial Setup**:
   - MCP client connects, receives 401
   - Browser opens MCP server OAuth URL
   - MCP server redirects to shared IdP
   - User authenticates once to IdP
   - IdP shows consent for both identity and Nextcloud access
   - MCP server stores IdP refresh token (encrypted)
   - MCP server issues session token to client

2. **Subsequent Requests**:
   - MCP server validates session token
   - Uses stored IdP token for Nextcloud
   - Refreshes with IdP when expired
   - No client involvement needed

3. **Background Operations**:
   - Worker retrieves stored IdP refresh token
   - Gets new access token from IdP
   - Uses token to access Nextcloud
   - Performs operations independently

### Advantages
- ✅ True single sign-on (SSO)
- ✅ Enterprise-ready with SAML/LDAP support
- ✅ OAuth-compliant with proper delegation
- ✅ Direct IdP relationship - no intermediary
- ✅ Flexible - can swap resource servers
- ✅ Industry-standard federated pattern

### Trade-offs
- Requires shared IdP infrastructure
- More complex initial setup
- Token validation overhead

---

## Comparison Matrix

| Feature | Pass-Through | Token Exchange | Sign-in with NC | Federated Auth |
|---------|--------------|----------------|-----------------|----------------|
| **Offline Access** | ❌ No | ❌ No | ✅ Yes | ✅ Yes |
| **Background Workers** | ❌ No | ❌ No* | ✅ Yes | ✅ Yes |
| **Token Storage** | None | None | NC refresh tokens | IdP refresh tokens |
| **OAuth Compliance** | ✅ Full | ⚠️ Violates | ✅ Full | ✅ Full |
| **User Consent** | Once | Implicit | Once (NC) | Once (IdP) |
| **Implementation Complexity** | Low | High | Medium | Medium-High |
| **Security** | High | Medium | High | Highest |
| **Enterprise Ready** | ❌ No | ❌ No | ⚠️ Indirect | ✅ Yes |
| **Identity Provider** | Client-managed | N/A | Nextcloud (+user_oidc) | Shared IdP |
| **Suitable For** | Interactive only | N/A (flawed) | Small teams | Enterprise |

\* *Requires service accounts that violate OAuth principles*

---

## Evolution Summary

### Stage 1: Simple Pass-Through ✅
- **Goal**: Basic MCP functionality
- **Result**: Works well for interactive use
- **Limitation**: No offline capabilities

### Stage 2: Attempted Delegation ❌
- **Goal**: Enable offline access without changing architecture
- **Result**: Circular dependencies, OAuth violations
- **Learning**: MCP protocol constraints are fundamental

### Stage 3: Sign-in with Nextcloud ⚠️
- **Goal**: True offline access with OAuth compliance
- **Result**: MCP server uses Nextcloud as identity provider
- **Limitation**: Tight coupling to Nextcloud, no enterprise IdP

### Stage 4: Federated Pattern ✅
- **Goal**: Enterprise-ready offline access
- **Result**: Shared IdP for both MCP server and Nextcloud
- **Trade-off**: Additional infrastructure justified by enterprise needs

---

## Key Insights

1. **Pattern 3 vs Pattern 4**: Both support external IdPs, but differ in integration approach:
   - Pattern 3: MCP → Nextcloud OIDC → (user_oidc) → External IdP
   - Pattern 4: MCP → External IdP directly (Nextcloud also uses same IdP)
   - Choose Pattern 3 for Nextcloud-centric deployments, Pattern 4 for IdP-centric enterprises

2. **The MCP Protocol Boundary**: The MCP protocol creates a fundamental boundary between client and server token management. Attempting to breach this boundary (ADR-002) leads to architectural contradictions.

3. **Service Accounts Don't Solve User Problems**: Using service accounts for user operations violates OAuth's core principle of acting on behalf of users, not as a service identity.

4. **Double OAuth is Industry Standard**: Major platforms (Zapier, IFTTT, Microsoft Power Automate) use this pattern - the integration platform is an OAuth client that maintains its own relationships with upstream services.

5. **Refresh Tokens Are The Solution**: The OAuth spec designed refresh tokens specifically for offline access. Rejecting them (as ADR-002 did) means rejecting the standard solution.

6. **Complexity is Justified**: The additional complexity of managing OAuth flows is acceptable when offline access is a requirement. The alternative is no offline access at all.

---

## Recommendations

### For Simple Deployments
Use **Pattern 1 (Pass-Through)** if:
- Offline access not needed
- Only interactive operations required
- Simplicity is priority

### For Teams Using Nextcloud
Use **Pattern 3 (Sign-in with Nextcloud)** if:
- Background sync/indexing required
- Nextcloud manages your authentication
- Can use external IdPs via user_oidc
- Prefer single integration point through Nextcloud

### For Enterprise Deployments
Use **Pattern 4 (Federated Authentication)** if:
- Enterprise IdP already exists (Keycloak, Okta, Azure AD)
- Multiple resource servers beyond Nextcloud
- Compliance requirements for centralized auth
- Building platform for multiple organizations

### Never Use Pattern 2
Token Exchange with service accounts should not be used as it:
- Doesn't enable true offline access
- Violates OAuth principles
- Adds complexity without solving the problem

---

## References

- [ADR-002: Vector Database Background Sync Authentication (Deprecated)](./ADR-002-vector-sync-authentication.md)
- [ADR-004: MCP Server as OAuth Client for Offline Access](./ADR-004-mcp-application-oauth.md)
- [RFC 6749: OAuth 2.0 Framework](https://datatracker.ietf.org/doc/html/rfc6749)
- [RFC 8693: OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693)