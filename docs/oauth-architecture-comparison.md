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

## Pattern 3: MCP Server as OAuth Client (ADR-004 - Solution)

### Architecture
```
        Layer 1: MCP Authentication          Layer 2: Nextcloud Authorization
┌─────────────┐                      ┌─────────────────┐                ┌─────────────┐
│ MCP Client  │                      │   MCP Server    │                │  Nextcloud  │
│  (Claude)   │                      │  (OAuth Client) │                │OAuth Provider
└──────┬──────┘                      └────────┬────────┘                └──────┬──────┘
       │                                      │                                │
       │ 1. MCP Request                       │ 2. Check stored tokens         │
       ├─────────────────────────────────────►│                                │
       │                                      │                                │
       │ 3. "Need Nextcloud Auth"             │                                │
       │◄─────────────────────────────────────┤                                │
       │                                      │                                │
       │ 4. User initiates OAuth              │ 5. OAuth Authorization         │
       ├─────────────────────────────────────►├───────────────────────────────►│
       │                                      │                                │
       │                                      │ 6. Access + Refresh Tokens     │
       │                                      │◄───────────────────────────────┤
       │                                      │                                │
       │                                      │ 7. Store encrypted tokens      │
       │                                      ├────────┐                       │
       │                                      │        ▼                       │
       │                                      │ ┌─────────────┐               │
       │                                      │ │Token Storage│               │
       │                                      │ └─────────────┘               │
       │ 8. "Auth Complete"                   │                                │
       │◄─────────────────────────────────────┤                                │
       │                                      │                                │
       │ 9. Subsequent requests               │ 10. Use stored tokens         │
       ├─────────────────────────────────────►├───────────────────────────────►│
       │                                      │                         Nextcloud APIs
       │                                      │                                │
       │                           Background │ 11. Refresh when expired       │
       │                              Worker──►├───────────────────────────────►│
       │                           (No client needed!)                         │
```

### Characteristics
| Aspect | Description |
|--------|-------------|
| **Token Flow** | MCP Server owns Nextcloud tokens |
| **Token Storage** | ✅ Encrypted refresh tokens |
| **Offline Access** | ✅ Full support |
| **Background Workers** | ✅ Use stored refresh tokens |
| **User Consent** | Two OAuth flows (app + Nextcloud) |
| **Complexity** | Medium-High |
| **Security** | High (proper OAuth compliance) |

### How It Works
1. **Initial Setup**:
   - User connects to MCP server (Layer 1 auth)
   - MCP server checks for stored Nextcloud tokens
   - If missing, triggers OAuth flow with Nextcloud
   - User authorizes MCP server to access Nextcloud
   - MCP server stores refresh token (encrypted)

2. **Subsequent Requests**:
   - MCP server uses stored access token
   - Refreshes automatically when expired
   - No client involvement needed

3. **Background Operations**:
   - Worker retrieves stored refresh token
   - Gets new access token from Nextcloud
   - Performs operations independently

### Advantages
- ✅ True offline access capability
- ✅ OAuth-compliant with proper consent
- ✅ Background workers can operate independently
- ✅ Tokens persist across MCP sessions
- ✅ Users can revoke access anytime

### Trade-offs
- Users must authorize twice (MCP + Nextcloud)
- More complex token management
- Requires secure token storage

---

## Comparison Matrix

| Feature | Pass-Through | Token Exchange | MCP as OAuth Client |
|---------|--------------|----------------|-------------------|
| **Offline Access** | ❌ No | ❌ No | ✅ Yes |
| **Background Workers** | ❌ No | ❌ No* | ✅ Yes |
| **Token Storage** | None | None | Refresh tokens |
| **OAuth Compliance** | ✅ Full | ⚠️ Violates | ✅ Full |
| **User Consent** | Once | Implicit | Twice |
| **Implementation Complexity** | Low | High | Medium |
| **Security** | High | Medium | High |
| **Suitable For** | Interactive only | N/A (flawed) | Full platform |

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

### Stage 3: Application Pattern ✅
- **Goal**: True offline access with OAuth compliance
- **Result**: MCP server as independent OAuth client
- **Trade-off**: Additional complexity justified by requirements

---

## Key Insights

1. **The MCP Protocol Boundary**: The MCP protocol creates a fundamental boundary between client and server token management. Attempting to breach this boundary (ADR-002) leads to architectural contradictions.

2. **Service Accounts Don't Solve User Problems**: Using service accounts for user operations violates OAuth's core principle of acting on behalf of users, not as a service identity.

3. **Double OAuth is Industry Standard**: Major platforms (Zapier, IFTTT, Microsoft Power Automate) use this pattern - the integration platform is an OAuth client that maintains its own relationships with upstream services.

4. **Refresh Tokens Are The Solution**: The OAuth spec designed refresh tokens specifically for offline access. Rejecting them (as ADR-002 did) means rejecting the standard solution.

5. **Complexity is Justified**: The additional complexity of managing two OAuth flows is acceptable when offline access is a requirement. The alternative is no offline access at all.

---

## Recommendations

### For Simple Deployments
Use **Pattern 1 (Pass-Through)** if:
- Offline access not needed
- Only interactive operations required
- Simplicity is priority

### For Platform Deployments
Use **Pattern 3 (MCP as OAuth Client)** if:
- Background sync/indexing required
- Multiple users need service
- Building integration platform
- Offline operations critical

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