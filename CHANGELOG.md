## v0.40.0 (2025-11-16)

### Feat

- add unified provider architecture with Amazon Bedrock support

### Fix

- suppress Starlette middleware type warnings in ty checker

## v0.39.0 (2025-11-16)

### Feat

- Implement BM25 hybrid search with native Qdrant RRF fusion

### Fix

- Handle named vectors in visualization and semantic search
- Update vizApp to use bm25_hybrid algorithm and remove deprecated weights
- Update viz routes to use BM25 hybrid search after refactor

## v0.38.0 (2025-11-16)

### Feat

- add concurrent uploads and --force flag to upload command
- implement RAG evaluation framework with CLI tooling

### Fix

- download qrels from BEIR ZIP instead of HuggingFace

### Refactor

- migrate asyncio to anyio for consistent structured concurrency
- replace httpx client with NextcloudClient in upload command

### Perf

- Eliminate double-fetching in semantic search sampling
- fix vector viz search performance and visual encoding
- make note deletion concurrent in upload --force

## v0.37.0 (2025-11-16)

### Feat

- Add OpenTelemetry tracing to @instrument_tool decorator

## v0.36.0 (2025-11-15)

### BREAKING CHANGE

- Search algorithms now require Qdrant to be populated.
Vector sync must be enabled and documents indexed for search to work.

### Feat

- Normalize hybrid search RRF scores to 0-1 range
- Enhance vector visualization UI and parallelize search verification
- Add Vector Viz tab to app home page
- Add vector visualization pane with multi-select document types
- Implement custom PCA to remove sklearn dependency
- Add multi-document Protocol with cross-app search support
- Update nc_semantic_search tool with algorithm selection
- Implement unified search algorithm module

### Fix

- Reorder tabs and fix viz pane session access

### Refactor

- Optimize Nextcloud access verification with centralized filtering
- Make all search algorithms query Qdrant payload, not Nextcloud

### Perf

- Exclude vector-sync status polling from distributed tracing

## v0.35.0 (2025-11-15)

### Feat

- Enable SSE transport for mcp service and update test fixtures

## v0.34.2 (2025-11-13)

### Fix

- Use NEXTCLOUD_OIDC_CLIENT_ID/SECRET env vars consistently

## v0.34.1 (2025-11-13)

### Fix

- return all notes when search query is empty

## v0.34.0 (2025-11-13)

### Feat

- Complete Phase 5 - Instrument all 93 MCP tools
- Add instrumentation decorator and apply to notes tools (Phase 5)
- Add OAuth token and database metrics (Phases 3-4)
- Add metrics instrumentation for queue, health, and database operations

## v0.33.1 (2025-11-13)

### Fix

- Move grafana_folder from labels to annotations

## v0.33.0 (2025-11-13)

### Feat

- Add Grafana dashboard and vector sync metric instrumentation

## v0.32.1 (2025-11-12)

### Fix

- add dynamic dimension detection for Ollama embedding models

## v0.32.0 (2025-11-11)

### Feat

- **ollama**: Pull model on startup if not available in ollama
- add dynamic vector sync status updates with htmx polling
- add webhook management UI and BeforeNodeDeletedEvent support
- validate Nextcloud webhook schemas and document findings

### Fix

- improve webapp tab UI with CSS Grid and viewport-filling container

### Refactor

- move webapp from /user/page to /app
- consolidate database storage for webhooks and OAuth tokens

## v0.31.1 (2025-11-10)

### Refactor

- simplify OpenTelemetry tracing configuration

## v0.31.0 (2025-11-10)

### Feat

- skip tracing for health and metrics endpoints

### Fix

- add retry logic for ETag conflicts in category change test
- optimize Notes API pagination with pruneBefore parameter

## v0.30.0 (2025-11-10)

### Feat

- **helm**: Add document chunking configuration
- **vector**: Add configurable chunk size and overlap for document embedding
- **vector**: Support multiple embedding models with auto-generated collection names

### Fix

- Support in-memory Qdrant for CI testing

## v0.29.2 (2025-11-09)

### Fix

- **helm**: Set default strategy to Recreate

## v0.29.1 (2025-11-09)

### Fix

- **observability**: isolate metrics endpoint to dedicated port

## v0.29.0 (2025-11-09)

### Feat

- **helm**: Add observability support with ServiceMonitor and Grafana dashboard

### Fix

- **readiness**: Only check external Qdrant in network mode

## v0.28.0 (2025-11-09)

### Feat

- **observability**: Add comprehensive monitoring with Prometheus and OpenTelemetry

### Fix

- **vector**: Handle missing 'modified' field in notes gracefully

## v0.27.3 (2025-11-09)

### Fix

- **ci**: Use helm dependency build instead of update to use Chart.lock

## v0.27.2 (2025-11-09)

### Fix

- **helm**: update Qdrant dependency condition to match new mode structure

## v0.27.1 (2025-11-09)

### Fix

- **ci**: add Helm repository setup to chart release workflow

## v0.27.0 (2025-11-09)

### Feat

- **helm**: add Qdrant local mode support with three deployment options [skip ci]
- add Qdrant local mode support with in-memory and persistent storage
- implement ADR-009 - refactor semantic search to use generic semantic:read scope
- implement MCP sampling for semantic search RAG (ADR-008)
- add optional vector database and semantic search to helm chart
- add vector sync processing status to /app endpoint
- implement semantic search tool and fix vector sync issues (ADR-007 Phase 3)
- implement vector sync scanner and processor (ADR-007 Phase 2)

### Fix

- implement deletion grace period and vector sync status tool
- remove unnecessary urllib3<2.0 constraint
- integrate vector sync tasks with Starlette lifespan for streamable-http

### Refactor

- migrate vector sync from asyncio.Queue to anyio memory object streams
- update to Qdrant query_points API and fix Playwright Keycloak login

## v0.26.1 (2025-11-08)

### Fix

- **deps**: update dependency mcp to >=1.21,<1.22

## v0.26.0 (2025-11-08)

### Feat

- add real elicitation integration test with python-sdk MCP client
- unify session architecture and enhance login status visibility

### Fix

- Consolidate OAuth callbacks and implement PKCE for all flows

## v0.25.0 (2025-11-05)

### BREAKING CHANGE

- All OAuth deployments must be reconfigured to specify
resource URIs (NEXTCLOUD_MCP_SERVER_URL and NEXTCLOUD_RESOURCE_URI) and
choose between multi-audience or token exchange mode.

### Feat

- Implement ADR-005 unified token verifier to eliminate token passthrough vulnerability

### Fix

- Implement proper OAuth resource parameters and PRM-based discovery
- Simplify token verifier to be RFC 7519 compliant
- Use Keycloak client ID for NEXTCLOUD_RESOURCE_URI in token exchange
- Correct OAuth token audience validation for multi-audience mode

### Refactor

- Eliminate duplicate validation logic in UnifiedTokenVerifier

## v0.24.1 (2025-11-04)

### Fix

- **deps**: update dependency mcp to >=1.20,<1.21

## v0.24.0 (2025-11-04)

### Feat

- add scope protection to OAuth provisioning tools
- enable authorization services for token exchange in Keycloak
- implement scope-based audience mapping and RFC 9728 support
- integrate token exchange into MCP server application
- implement RFC 8693 Standard Token Exchange for Keycloak
- Add userinfo route/page
- add browser-based user info page with separate OAuth flow
- Implement ADR-004 Progressive Consent foundation (partial)
- Complete ADR-004 Progressive Consent OAuth flows implementation
- Implement ADR-004 Progressive Consent foundation components
- Implement ADR-004 Hybrid Flow with comprehensive integration tests

### Fix

- add missing await for get_nextcloud_client in capabilities resource
- use valid Fernet encryption keys in token exchange tests
- accept resource URL in token audience for Nextcloud JWT tokens
- remove token-exchange-nextcloud scope and accept tokens without audience
- move audience mapper from scope to nextcloud-mcp-server client
- move token-exchange-nextcloud from default to optional scopes
- restructure routes to prevent SessionAuthBackend from interfering with FastMCP OAuth
- allow OAuth Bearer tokens on /mcp endpoint by excluding from session auth
- correct OAuth token audience validation using RFC 8707 resource parameter
- remove remaining references to deleted oauth_callback and oauth_token
- remove Hybrid Flow, make Progressive Consent default (ADR-004)
- browser OAuth userinfo endpoint and refresh token rotation
- make ENABLE_PROGRESSIVE_CONSENT consistently opt-in (default false)
- make provisioning checks opt-in (default false)
- Disable Progressive Consent for mcp-oauth to enable Hybrid Flow tests

### Refactor

- integrate token exchange into unified get_client() pattern

## v0.23.0 (2025-11-03)

### Feat

- Auto-configure impersonation role in Keycloak realm import
- Implement dual-tier token exchange (Standard V2 + Legacy V1 impersonation)
- Add Keycloak external IdP integration with custom scopes
- Implement RFC 8693 token exchange for Keycloak (ADR-002 Tier 2)
- Add Keycloak OAuth provider support with refresh token storage

### Fix

- Complete Keycloak external IdP integration with all tests passing
- Complete Keycloak external IdP integration with all tests passing
- Update DCR token_type tests for OIDC app changes

### Refactor

- Remove NEXTCLOUD_OIDC_CLIENT_STORAGE environment variable
- Remove unnecessary user_oidc patch - CORSMiddleware patch is sufficient
- Unify OAuth configuration to be provider-agnostic

## v0.22.7 (2025-10-29)

### Fix

- **helm**: Remove image tag overide

## v0.22.6 (2025-10-29)

### Fix

- **helm**: Update helm chart with extraArgs

## v0.22.5 (2025-10-29)

### Fix

- Update helm chart variables

## v0.22.4 (2025-10-29)

### Fix

- **helm**: Update helm version with release
- **helm**: Update helm version with release

## v0.22.3 (2025-10-29)

### Fix

- **helm**: Update helm version with release

## v0.22.2 (2025-10-29)

### Fix

- **helm**: Update helm version with release

## v0.22.1 (2025-10-29)

### Fix

- Trigger release

## v0.22.0 (2025-10-29)

### Feat

- **server**: Add /live & /health endpoints
- Initialize helm chart

## v0.21.0 (2025-10-25)

### Feat

- Add text processing background worker for telling client about progress

### Refactor

- Transform document parsing into pluggable processor architecture

## v0.20.0 (2025-10-24)

### Feat

- **auth**: Add support for client registration deletion
- Split read/write scopes into app:read/write scopes

### Fix

- Add support for RFC 7592 client registration and deletion
- Update webdav models for proper serialization

## v0.19.1 (2025-10-24)

### Fix

- **deps**: update dependency mcp to >=1.19,<1.20

## v0.19.0 (2025-10-23)

### Feat

- Enable token introspection for opaque tokens

### Fix

- Add CORS middleware to allow browser-based clients like MCP Inspector

## v0.18.0 (2025-10-23)

### Feat

- **server**: Add support for custom OIDC scopes and permissions via JWTs
- Initialize JWT-scoped tools

### Fix

- Use occ-created OAuth clients with allowed_scopes for all tests
- Separate OAuth fixtures for opaque vs JWT tokens

### Refactor

- Update JWT client to use DCR, re-enable tool filtering

## v0.17.1 (2025-10-20)

### Fix

- **caldav**: Fix caldav search() due to missing todos

## v0.17.0 (2025-10-19)

### Feat

- **caldav**: Add support for tasks

### Fix

- **caldav**: Check that calendar exists after creation to avoid race condition
- **caldav**: Properly parse datetimes as vDDDTypes

### Refactor

- Migrate from internal CalendarClient to caldav library

## v0.16.0 (2025-10-19)

### Feat

- **webdav**: Add search and list favorite response tools

### Perf

- **notes**: Improve notes search performance using async iterators

## v0.15.2 (2025-10-17)

### Refactor

- Unify logging & remove factory deployment

## v0.15.1 (2025-10-17)

### Fix

- Increase HTTP client timeout to 30s
- Handle RequestError in mcp tools

## v0.15.0 (2025-10-17)

### Feat

- **cookbook**: Add full Cookbook app support with 13 tools and 2 resources

## v0.14.3 (2025-10-17)

### Fix

- **deps**: update dependency mcp to >=1.18,<1.19

## v0.14.2 (2025-10-16)

### Fix

- **deps**: update dependency pillow to v12

## v0.14.1 (2025-10-15)

### Fix

- **oauth**: Remove the option to force_register new clients

## v0.14.0 (2025-10-15)

### Feat

- Add Groups API client
- add sharing API client and server tools
- **users**: Initialize user API client

### Fix

- Update user/groups API to OCS v2

## v0.13.0 (2025-10-13)

### Feat

- **server**: Experimental support for OAuth2/OIDC authentication

## v0.12.6 (2025-10-11)

### Fix

- **deps**: update dependency mcp to >=1.17,<1.18

## v0.12.5 (2025-10-03)

### Fix

- **deps**: update dependency mcp to >=1.16,<1.17

## v0.12.4 (2025-09-25)

### Fix

- **deps**: update dependency mcp to >=1.15,<1.16

## v0.12.3 (2025-09-23)

### Refactor

- Add tools for all resources to enable tool-only workflows

## v0.12.2 (2025-09-20)

### Refactor

- Add `http` to --transport option

## v0.12.1 (2025-09-11)

### Fix

- **docker**: Provide --host 0.0.0.0 in default docker image

## v0.12.0 (2025-09-11)

### Feat

- **server**: Add support for `streamable-http` transport type

## v0.11.1 (2025-09-11)

### Fix

- **deps**: update dependency mcp to >=1.13,<1.14

## v0.11.0 (2025-09-11)

### Feat

- **deck**: Add support for stack, cards, labels
- **deck**: Initialize Deck app client/server

## v0.10.0 (2025-09-10)

### Feat

- Add WebDAV resource copy functionality
- Add WebDAV resource move/rename functionality

## v0.9.0 (2025-09-10)

### BREAKING CHANGE

- FASTMCP_-prefixed env vars have been replaced by CLI
arguments. Refer to the README for updated usage.

### Feat

- **cli**: Replace `mcp run` with click CLI and runtime options

## v0.8.3 (2025-08-31)

### Fix

- **server**: Replace ErrorResponses with standard McpErrors
- **notes**: Include ETags in responses to avoid accidently updates

## v0.8.2 (2025-08-31)

### Fix

- **notes**: Remove note contents from responses to reduce token usage

## v0.8.1 (2025-08-30)

### Fix

- **model**: Serialize timestamps in RFC3339 format

## v0.8.0 (2025-08-30)

### Feat

- **client**: Preserve fields when modifying contacts/calendar resources
- **server**: Add structured output to all tool/resource output

### Refactor

- Use _make_request where available

## v0.7.2 (2025-08-30)

### Fix

- **client**: Use paging to fetch all notes

## v0.7.1 (2025-08-08)

### Fix

- **client**: Strip cookies from responses to avoid falsely raising CSRF errors

## v0.7.0 (2025-08-03)

### Feat

- **contacts**: Initialize Contacts App

## v0.6.1 (2025-08-01)

### Fix

- **calendar**: Fix iCalendar date vs datetime format
- **calendar**: Remove try/except in calendar API

## v0.6.0 (2025-07-29)

### Feat

- **calendar**: add comprehensive Calendar app support via CalDAV protocol

### Fix

- apply ruff formatting to pass CI checks
- **calendar**: address PR feedback from maintainer

### Refactor

- **calendar**: optimize logging for production readiness

## v0.5.0 (2025-07-26)

### Feat

- Update webdav client create_directory method to handle recursive directories
- **webdav**: add complete file system support

### Fix

- apply ruff formatting to test_webdav_operations.py

## v0.4.1 (2025-07-10)

### Fix

- **deps**: update dependency mcp to >=1.10,<1.11

## v0.4.0 (2025-07-06)

### Feat

- Add TablesClient and associated tools

### Fix

- update tests

### Refactor

- Modularize NC and Notes app client

## v0.3.0 (2025-06-06)

### Feat

- Switch to using async client

## v0.2.5 (2025-05-25)

### Fix

- Commitizen release process

## v0.2.4 (2025-05-25)

### Fix

- Do not update dependencies when running in Dockerfile
- Configure logging

## v0.2.3 (2025-05-25)

### Fix

- Limit search results to notes with score > 0.5

## v0.2.2 (2025-05-24)

### Fix

- Install deps before checking service

## v0.2.1 (2025-05-24)

### Fix

- Install deps before checking service

## v0.2.1 (2025-05-24)

## v0.2.0 (2025-05-24)

### Feat

- **notes**: Add append to note functionality

### Fix

- **deps**: update dependency mcp to >=1.9,<1.10

## v0.1.3 (2025-05-16)

## v0.1.2 (2025-05-05)

## v0.1.1 (2025-05-05)

## v0.1.0 (2025-05-05)
