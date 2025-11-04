# Manual OAuth Flow Testing

This directory contains manual test scripts for OAuth flows that require browser interaction.

## ADR-004 OAuth Hybrid Flow Test

The `test_adr004_oauth_flow.py` script tests the complete OAuth flow described in ADR-004.

### Prerequisites

1. **Install Playwright browsers:**
   ```bash
   uv run playwright install firefox
   ```

2. **Start MCP server with OAuth enabled:**

   For Nextcloud OIDC:
   ```bash
   export ENABLE_OFFLINE_ACCESS=true
   export TOKEN_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   docker-compose up --build -d mcp-oauth
   ```

   For Keycloak:
   ```bash
   export ENABLE_OFFLINE_ACCESS=true
   export TOKEN_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
   docker-compose up --build -d mcp-keycloak
   ```

### Running the Test

**Test with Nextcloud OIDC:**
```bash
uv run python tests/manual/test_adr004_oauth_flow.py --provider nextcloud
```

**Test with Keycloak:**
```bash
uv run python tests/manual/test_adr004_oauth_flow.py --provider keycloak
```

**Headless mode:**
```bash
uv run python tests/manual/test_adr004_oauth_flow.py --provider nextcloud --headless
```
