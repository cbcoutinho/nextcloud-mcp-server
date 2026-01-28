# Changelog - MCP Server

All notable changes to the Nextcloud MCP Server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/).

## v0.63.0 (2026-01-28)

### Feat

- **astrolabe**: add background token refresh job

### Fix

- **astrolabe**: add pagination and psalm fixes for token refresh
- **astrolabe**: add locking to prevent token refresh race condition
- **astrolabe**: add issued_at to on-demand token refresh

## v0.62.0 (2026-01-26)

### Feat

- **scripts**: add database query helpers for development

### Fix

- **astrolabe**: resolve Psalm type errors in PDF preview code
- **astrolabe**: fix Psalm baseline and ESLint import order
- **astrolabe**: load pdfjs-dist externally to fix PDF viewer
- **astrolabe**: improve error messages for authorization issues
- **astrolabe**: rename OAuthController and fix app password check
- **tests**: improve Astrolabe integration test reliability
- **astrolabe**: update Plotly title attributes for v3 compatibility
- **deps**: update dependency plotly.js-dist-min to v3

### Refactor

- **api**: split management.py into domain-focused modules
- **astrolabe**: replace client-side PDF.js with server-side PyMuPDF rendering

## v0.61.5 (2026-01-17)

### Fix

- **astrolabe**: improve token refresh error handling and validation
- **astrolabe**: delete stale tokens when refresh fails
- **astrolabe**: resolve CI failures for code quality checks
- **astrolabe**: use internal URL for OAuth token refresh

### Refactor

- **astrolabe**: add PHP property types to fix Psalm errors
- **astrolabe**: upgrade to @nextcloud/vue 9.3.3 API

## v0.61.4 (2026-01-16)

### Fix

- **astrolabe**: Address reviewer feedback for hybrid mode
- **astrolabe**: Fix NcSelect options and CSS loading
- **astrolabe**: fix OAuth flow and settings UI for hybrid mode
- **api**: return OIDC config in hybrid mode for Astrolabe OAuth flow

## v0.61.3 (2026-01-15)

### Fix

- **astrolabe**: address review feedback for Vue 3 bindings
- **astrolabe**: update Vue component bindings for Vue 3 compatibility

## v0.61.2 (2026-01-15)

### Fix

- **ci**: bump helm chart version when MCP appVersion changes

## v0.61.1 (2026-01-15)

### Fix

- **astrolabe**: define appName and appVersion for @nextcloud/vue

## v0.61.0 (2026-01-14)

### Feat

- Add rate limiting and extract helpers for app password endpoints

### Fix

- Add missing annotations for deck remove/unassign operations
- **auth**: Store app passwords locally for multi-user BasicAuth background sync

### Refactor

- Use get_settings() for vector sync enabled check
- Extract storage helper and improve PHP error handling

## v0.60.4 (2026-01-12)

### Fix

- **deck**: use correct endpoint for reorder_card to fix cross-stack moves

## v0.60.3 (2025-12-31)

### Fix

- **deck**: Always preserve fields in update_card for partial updates
- **astrolabe**: Fix CSS loading for Nextcloud apps
- **astrolabe**: Fix revoke access button HTTP method mismatch

## v0.60.2 (2025-12-29)

### Fix

- **oauth**: Enable browser OAuth routes for Management API in hybrid mode

## v0.60.1 (2025-12-26)

### Fix

- **mcp**: Move all imports to the top of modules

## v0.60.0 (2025-12-26)

### Feat

- Remove URL rewriting in favor of proper nextcloud config
- **helm**: migrate to new environment variable naming convention
- Migrate to vue 3
- **astrolabe**: upgrade to Vue 3 and @nextcloud/vue 9

### Fix

- **tests**: Add singleton reset fixture to prevent anyio.WouldBlock errors
- **tests**: Fix integration test failures in qdrant, sampling, and rag tests
- **auth**: Skip issuer validation for management API tokens
- Use settings.enable_offline_access for env var consolidation
- Add required config.py attributes
- **docker**: remove overwritehost to fix container-to-container DCR
- **deps**: update dependency @nextcloud/vue to v9
- **deps**: update dependency vue to v3

### Refactor

- **auth**: Decouple BasicAuth and OAuth authentication strategies

## v0.59.1 (2025-12-22)

### Fix

- **helm**: set OIDC client env vars when using existingSecret
- **helm**: trigger chart release workflow on helm chart tags

## v0.59.0 (2025-12-22)

### Feat

- **helm**: add support for multi-user BasicAuth mode

### Fix

- **helm**: address PR #447 reviewer feedback
- **helm**: include MCP server version bumps in changelog pattern

## v0.58.0 (2025-12-22)

### Feat

- **config**: enable DCR for multi-user BasicAuth with offline access
- **astrolabe**: implement app password provisioning for multi-user background sync
- **config**: consolidate configuration with smart dependency resolution (ADR-021)

## v0.57.0 (2025-12-20)

### Feat

- **auth**: add multi-user BasicAuth pass-through mode
- **astrolabe**: add dynamic MCP server configuration for testing

### Fix

- **config**: address reviewer feedback

### Refactor

- **config**: centralize configuration validation and simplify startup

## v0.56.2 (2025-12-20)

### Fix

- **astrolabe**: screenshots in info.xml
- **astrolabe**: screenshots in info.xml

## v0.56.1 (2025-12-19)

### Fix

- **astrolabe**: Update screenshots
- **ci**: skip existing Helm chart releases to prevent duplicate release errors

## v0.56.0 (2025-12-19)

### Feat

- **ci**: add --increment flag to bump scripts for manual version control

### Fix

- **astrolabe**: add contents:write permission to appstore workflow
- **astrolabe**: update commitizen pattern to properly update info.xml version
- **astrolabe**: prevent workflow failure when only helm/astrolabe commits exist
- **astrolabe**: info.xml

## v0.55.1 (2025-12-19)

### Fix

- **ci**: push all tags explicitly in bump workflow

## v0.55.0 (2025-12-19)

### BREAKING CHANGE

- MCP server now bumps for ANY conventional commit except
those explicitly scoped to helm or astrolabe.

### Feat

- **ci**: implement monorepo-aware version bumping workflow

### Fix

- **ci**: make MCP server default bump target for all non-scoped commits
- **ci**: restrict docker build to MCP server tags only
- **ci**: correct appstore-push-action version to v1.0.4

## v0.54.0 (2025-12-19)

### Feat

- **astrolabe**: add Nextcloud App Store deployment automation
- configure commitizen monorepo with independent versioning

### Fix

- **ci**: improve versioning and error handling
- **ci**: address critical workflow and validation issues
- **astrolabe**: address code review feedback

## v0.53.0 (2025-12-19)

### Feat

- add Alembic database migration system
- make chunk modal title clickable link to documents
- add native Plotly hover styling for clickable points
- add click interactivity to Plotly 3D scatter chart
- improve chunk viewer with fixed navigation and markdown rendering
- **astrolabe**: enable multi-select for document types and refactor PDF viewer
- **auth**: implement refresh token rotation for Nextcloud OIDC
- **astrolabe**: enhance unified search and add webhook management
- **astrolabe**: add webhook management UI to admin settings
- **astrolabe**: add OAuth token refresh and webhook presets
- **search**: add file_path metadata and chunk offsets to search results
- **astrolabe**: use proper icons and thumbnails in unified search
- **astrolabe**: add admin search settings and enhanced UI
- **astrolabe**: add unified search provider with clickable file links
- **astrolabe**: add 3D PCA visualization for semantic search
- **astrolabe**: add Nextcloud PHP app for MCP server management
- **vector-sync**: enable background sync in OAuth mode

### Fix

- **security**: address critical security issues from PR #401 code review
- **oauth**: enable PKCE for all clients and add token_broker to oauth_context
- **astrolabe**: revert invalid files_pdfviewer URL for file links
- resolve type checking warnings for CI
- move Alembic to package submodule for Docker compatibility
- update unified search results to match chunk viz display
- **astrolabe**: handle OAuth refresh token rotation
- address critical code review issues (4 fixes)
- resolve CI linting issues for Astroglobe

### Refactor

- **astrolabe**: extract PDF viewer to dedicated component
- **astrolabe**: reframe UI as semantic search service

## v0.52.1 (2025-12-13)

### Perf

- **deck**: optimize card lookup by storing board_id/stack_id in metadata

## v0.52.0 (2025-12-13)

### Feat

- **vector**: add Deck card vector search with visualization support

## v0.51.0 (2025-12-13)

### Feat

- **vector-viz**: add news_item support for links and chunk expansion

## v0.50.2 (2025-12-13)

### Fix

- **news**: revert get_item() to use get_items() + filter

## v0.50.1 (2025-12-12)

### Fix

- Disable DNS rebinding protection for containerized deployments
- **deps**: update dependency mcp to >=1.23,<1.24

## v0.50.0 (2025-12-11)

### Feat

- add MCP tool annotations for enhanced UX

### Fix

- address PR review feedback

## v0.49.2 (2025-12-09)

### Fix

- Update lockfile

## v0.49.1 (2025-12-09)

### Fix

- Revert mcp version <1.23

## v0.49.0 (2025-12-08)

### Feat

- **news**: add Nextcloud News app integration

### Fix

- resolve all type checking errors (8 errors fixed)

### Refactor

- **news**: simplify vector sync to fetch all items

### Perf

- **news**: use direct API endpoint for get_item()

## v0.48.6 (2025-12-03)

### Fix

- **deps**: update dependency mcp to >=1.23,<1.24

## v0.48.5 (2025-11-28)

### Fix

- **deps**: update dependency pillow to v12

## v0.48.4 (2025-11-23)

### Fix

- Add rate limit retry logic to OpenAI provider

## v0.48.3 (2025-11-23)

### Fix

- Increase MCP sampling timeout to 5 minutes for slower LLMs

## v0.48.2 (2025-11-23)

### Fix

- Share vector sync state with FastMCP session lifespan via module singleton
- Share vector sync state with FastMCP session lifespan via module singleton

## v0.48.1 (2025-11-23)

### Fix

- Use WebDAV for tag creation and add LLM-as-a-judge for RAG tests

### Refactor

- Move background tasks to server lifespan and deprecate SSE transport

## v0.48.0 (2025-11-23)

### Feat

- Add tag management methods to WebDAV client

## v0.47.0 (2025-11-23)

### Feat

- Add OpenAI provider support for embeddings and generation

## v0.46.2 (2025-11-22)

### Fix

- **smithery**: Enable JSON response format for scanner compatibility

## v0.46.1 (2025-11-22)

### Perf

- Optimize vector viz search performance

## v0.46.0 (2025-11-22)

### Feat

- Add Smithery CLI deployment support
- Implement ADR-016 Smithery stateless deployment mode

### Fix

- **smithery**: Add JSON Schema metadata to mcp-config endpoint
- **smithery**: Use container runtime pattern for config discovery
- Add Smithery lifespan and auth mode detection

## v0.45.0 (2025-11-22)

### Feat

- Add context expansion to semantic search with chunk overlap removal
- Use Ollama native batch API in embed_batch()
- Implement Qdrant placeholder state management
- Switch files to use numeric IDs with file_path resolution
- Implement per-chunk vector visualization with context expansion

### Fix

- Use alpha_composite for proper RGBA highlight blending
- Remove pymupdf.layout.activate() to fix page_chunks behavior
- Centralize PDF processing and generate separate images per chunk
- Set is_placeholder=False in processor to fix search filtering
- Increase placeholder staleness threshold to 5x scan interval
- Add placeholder staleness check to prevent duplicate processing
- Use empty SparseVector instead of None for placeholders
- Return empty array instead of null for query_coords when no results
- Align PDF text extraction between indexing and context expansion
- Update models and viz to use int-only doc_id
- Reconstruct full content for notes to match indexed offsets
- Add async/await, PDF metadata, and type safety fixes

### Refactor

- Simplify PDF text extraction with single to_markdown call

### Perf

- Optimize PDF processing with parallel extraction and single-render highlights

## v0.44.1 (2025-11-21)

### Fix

- **deps**: update dependency mcp to >=1.22,<1.23

## v0.44.0 (2025-11-19)

### Feat

- Improve vector visualization with static assets and fixes
- Redesign UI to match Nextcloud ecosystem aesthetic

### Fix

- Improve 3D plot rendering with explicit dimensions and window resize support
- Preserve 3D plot camera and improve documentation
- Preserve 3D plot camera position and fix CSS loading

## v0.43.0 (2025-11-18)

### Feat

- Replace custom document chunker with LangChain MarkdownTextSplitter

## v0.42.0 (2025-11-17)

### Feat

- **viz**: Add dual-score display and improve UI controls

## v0.41.0 (2025-11-17)

### Feat

- add configurable fusion algorithms for BM25 hybrid search
- add chunk position tracking to vector indexing and search
- add vector viz template and chunk context endpoint

### Fix

- prevent infinite loop in DocumentChunker with position tracking
- Relax SearchResult validation to support DBSF fusion scores > 1.0

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
