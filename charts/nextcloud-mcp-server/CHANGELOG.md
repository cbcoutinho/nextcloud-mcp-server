# Changelog - Helm Chart

All notable changes to the Helm chart will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


### Added
- Initial independent versioning release
- Support for Nextcloud MCP server deployment
- Qdrant subchart integration
- Ollama subchart integration
- Configurable resource limits
- Grafana dashboard annotations

## nextcloud-mcp-server-0.57.26 (2026-01-31)

## nextcloud-mcp-server-0.57.25 (2026-01-31)

## nextcloud-mcp-server-0.57.24 (2026-01-31)

## nextcloud-mcp-server-0.57.23 (2026-01-30)

## nextcloud-mcp-server-0.57.22 (2026-01-30)

## nextcloud-mcp-server-0.57.21 (2026-01-30)

## nextcloud-mcp-server-0.57.20 (2026-01-29)

## nextcloud-mcp-server-0.57.19 (2026-01-28)

## nextcloud-mcp-server-0.57.18 (2026-01-28)

## nextcloud-mcp-server-0.57.17 (2026-01-28)

## nextcloud-mcp-server-0.57.16 (2026-01-28)

### Feat

- **astrolabe**: add background token refresh job

### Fix

- **astrolabe**: add pagination and psalm fixes for token refresh
- **astrolabe**: add locking to prevent token refresh race condition
- **astrolabe**: add issued_at to on-demand token refresh

## nextcloud-mcp-server-0.57.15 (2026-01-26)

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

## nextcloud-mcp-server-0.57.14 (2026-01-26)

## nextcloud-mcp-server-0.57.13 (2026-01-24)

## nextcloud-mcp-server-0.57.12 (2026-01-20)

## nextcloud-mcp-server-0.57.11 (2026-01-20)

## nextcloud-mcp-server-0.57.10 (2026-01-19)

## nextcloud-mcp-server-0.57.9 (2026-01-19)

## nextcloud-mcp-server-0.57.8 (2026-01-18)

## nextcloud-mcp-server-0.57.7 (2026-01-17)

### Fix

- **astrolabe**: improve token refresh error handling and validation
- **astrolabe**: delete stale tokens when refresh fails
- **astrolabe**: resolve CI failures for code quality checks
- **astrolabe**: use internal URL for OAuth token refresh

### Refactor

- **astrolabe**: add PHP property types to fix Psalm errors
- **astrolabe**: upgrade to @nextcloud/vue 9.3.3 API

## nextcloud-mcp-server-0.57.6 (2026-01-16)

## nextcloud-mcp-server-0.57.5 (2026-01-16)

## nextcloud-mcp-server-0.57.4 (2026-01-16)

### Fix

- **astrolabe**: Address reviewer feedback for hybrid mode
- **astrolabe**: Fix NcSelect options and CSS loading
- **astrolabe**: fix OAuth flow and settings UI for hybrid mode
- **api**: return OIDC config in hybrid mode for Astrolabe OAuth flow

## nextcloud-mcp-server-0.57.3 (2026-01-15)

## nextcloud-mcp-server-0.57.2 (2026-01-15)

### Fix

- **astrolabe**: address review feedback for Vue 3 bindings
- **astrolabe**: update Vue component bindings for Vue 3 compatibility

## nextcloud-mcp-server-0.57.1 (2026-01-15)

### Fix

- **ci**: bump helm chart version when MCP appVersion changes
- **astrolabe**: define appName and appVersion for @nextcloud/vue

## nextcloud-mcp-server-0.57.0 (2026-01-15)

### Feat

- Add rate limiting and extract helpers for app password endpoints

### Fix

- Add missing annotations for deck remove/unassign operations
- **auth**: Store app passwords locally for multi-user BasicAuth background sync
- **deck**: use correct endpoint for reorder_card to fix cross-stack moves
- **deck**: Always preserve fields in update_card for partial updates
- **astrolabe**: Fix CSS loading for Nextcloud apps
- **astrolabe**: Fix revoke access button HTTP method mismatch

### Refactor

- Use get_settings() for vector sync enabled check
- Extract storage helper and improve PHP error handling

## nextcloud-mcp-server-0.56.2 (2025-12-29)

### Fix

- **oauth**: Enable browser OAuth routes for Management API in hybrid mode

## nextcloud-mcp-server-0.56.1 (2025-12-26)

### Fix

- **mcp**: Move all imports to the top of modules

## nextcloud-mcp-server-0.56.0 (2025-12-26)

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

## nextcloud-mcp-server-0.55.2 (2025-12-22)

### Fix

- **helm**: set OIDC client env vars when using existingSecret

## nextcloud-mcp-server-0.55.1 (2025-12-22)

### Fix

- **helm**: trigger chart release workflow on helm chart tags

## nextcloud-mcp-server-0.55.0 (2025-12-22)

### BREAKING CHANGE

- MCP server now bumps for ANY conventional commit except
those explicitly scoped to helm or astrolabe.

### Feat

- **helm**: add support for multi-user BasicAuth mode
- **config**: enable DCR for multi-user BasicAuth with offline access
- **astrolabe**: implement app password provisioning for multi-user background sync
- **config**: consolidate configuration with smart dependency resolution (ADR-021)
- **auth**: add multi-user BasicAuth pass-through mode
- **astrolabe**: add dynamic MCP server configuration for testing
- **ci**: add --increment flag to bump scripts for manual version control

### Fix

- **helm**: address PR #447 reviewer feedback
- **helm**: include MCP server version bumps in changelog pattern
- **config**: address reviewer feedback
- **astrolabe**: screenshots in info.xml
- **astrolabe**: screenshots in info.xml
- **astrolabe**: Update screenshots
- **ci**: skip existing Helm chart releases to prevent duplicate release errors
- **astrolabe**: add contents:write permission to appstore workflow
- **astrolabe**: update commitizen pattern to properly update info.xml version
- **astrolabe**: prevent workflow failure when only helm/astrolabe commits exist
- **astrolabe**: info.xml
- **ci**: push all tags explicitly in bump workflow
- **ci**: make MCP server default bump target for all non-scoped commits
- **ci**: restrict docker build to MCP server tags only
- **ci**: correct appstore-push-action version to v1.0.4

### Refactor

- **config**: centralize configuration validation and simplify startup

## nextcloud-mcp-server-0.54.0 (2025-12-19)

### Feat

- **ci**: implement monorepo-aware version bumping workflow
- **astrolabe**: add Nextcloud App Store deployment automation
- configure commitizen monorepo with independent versioning

### Fix

- **ci**: improve versioning and error handling
- **ci**: address critical workflow and validation issues
- **astrolabe**: address code review feedback

## nextcloud-mcp-server-0.53.0 (2025-12-19)

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

## nextcloud-mcp-server-0.52.1 (2025-12-13)

## nextcloud-mcp-server-0.52.0 (2025-12-13)

## nextcloud-mcp-server-0.51.0 (2025-12-13)

### Feat

- **vector**: add Deck card vector search with visualization support
- **vector-viz**: add news_item support for links and chunk expansion

### Perf

- **deck**: optimize card lookup by storing board_id/stack_id in metadata

## nextcloud-mcp-server-0.50.2 (2025-12-13)

### Fix

- **news**: revert get_item() to use get_items() + filter

## nextcloud-mcp-server-0.50.1 (2025-12-12)

### Fix

- Disable DNS rebinding protection for containerized deployments
- **deps**: update dependency mcp to >=1.23,<1.24

## nextcloud-mcp-server-0.50.0 (2025-12-11)

### Feat

- add MCP tool annotations for enhanced UX

### Fix

- address PR review feedback

## nextcloud-mcp-server-0.49.2 (2025-12-09)

### Fix

- Update lockfile

## nextcloud-mcp-server-0.49.1 (2025-12-09)

### Fix

- Revert mcp version <1.23

## nextcloud-mcp-server-0.49.0 (2025-12-08)

### Fix

- resolve all type checking errors (8 errors fixed)
- **deps**: update dependency mcp to >=1.23,<1.24

### Perf

- **news**: use direct API endpoint for get_item()

## nextcloud-mcp-server-0.48.5 (2025-11-28)

### Feat

- **news**: add Nextcloud News app integration

### Fix

- **deps**: update dependency pillow to v12

### Refactor

- **news**: simplify vector sync to fetch all items

## nextcloud-mcp-server-0.48.4 (2025-11-23)

### Fix

- Add rate limit retry logic to OpenAI provider

## nextcloud-mcp-server-0.48.3 (2025-11-23)

### Fix

- Increase MCP sampling timeout to 5 minutes for slower LLMs

## nextcloud-mcp-server-0.48.2 (2025-11-23)

### Fix

- Share vector sync state with FastMCP session lifespan via module singleton

## nextcloud-mcp-server-0.48.1 (2025-11-23)

## nextcloud-mcp-server-0.48.0 (2025-11-23)

## nextcloud-mcp-server-0.47.0 (2025-11-23)

### Feat

- Add tag management methods to WebDAV client
- Add OpenAI provider support for embeddings and generation

### Fix

- Share vector sync state with FastMCP session lifespan via module singleton
- Use WebDAV for tag creation and add LLM-as-a-judge for RAG tests

### Refactor

- Move background tasks to server lifespan and deprecate SSE transport

## nextcloud-mcp-server-0.46.2 (2025-11-22)

### Fix

- **smithery**: Enable JSON response format for scanner compatibility

## nextcloud-mcp-server-0.46.1 (2025-11-22)

### Perf

- Optimize vector viz search performance

## nextcloud-mcp-server-0.46.0 (2025-11-22)

### Feat

- Add Smithery CLI deployment support
- Implement ADR-016 Smithery stateless deployment mode

### Fix

- **smithery**: Add JSON Schema metadata to mcp-config endpoint
- **smithery**: Use container runtime pattern for config discovery
- Add Smithery lifespan and auth mode detection

## nextcloud-mcp-server-0.45.0 (2025-11-22)

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

## nextcloud-mcp-server-0.44.1 (2025-11-21)

### Fix

- **deps**: update dependency mcp to >=1.22,<1.23

## nextcloud-mcp-server-0.44.0 (2025-11-19)

### Feat

- Improve vector visualization with static assets and fixes
- Redesign UI to match Nextcloud ecosystem aesthetic

### Fix

- Improve 3D plot rendering with explicit dimensions and window resize support
- Preserve 3D plot camera and improve documentation
- Preserve 3D plot camera position and fix CSS loading

## nextcloud-mcp-server-0.43.0 (2025-11-18)

### Feat

- Replace custom document chunker with LangChain MarkdownTextSplitter

## nextcloud-mcp-server-0.42.0 (2025-11-17)

### Feat

- **viz**: Add dual-score display and improve UI controls

## nextcloud-mcp-server-0.41.0 (2025-11-17)

### Feat

- add configurable fusion algorithms for BM25 hybrid search
- add chunk position tracking to vector indexing and search
- add vector viz template and chunk context endpoint

### Fix

- prevent infinite loop in DocumentChunker with position tracking
- Relax SearchResult validation to support DBSF fusion scores > 1.0

## nextcloud-mcp-server-0.40.0 (2025-11-16)

### Feat

- add unified provider architecture with Amazon Bedrock support

### Fix

- suppress Starlette middleware type warnings in ty checker

## nextcloud-mcp-server-0.39.0 (2025-11-16)

## nextcloud-mcp-server-0.38.0 (2025-11-16)

### Feat

- add concurrent uploads and --force flag to upload command
- implement RAG evaluation framework with CLI tooling
- Add OpenTelemetry tracing to @instrument_tool decorator
- Implement BM25 hybrid search with native Qdrant RRF fusion

### Fix

- download qrels from BEIR ZIP instead of HuggingFace
- Handle named vectors in visualization and semantic search
- Update vizApp to use bm25_hybrid algorithm and remove deprecated weights
- Update viz routes to use BM25 hybrid search after refactor

### Refactor

- migrate asyncio to anyio for consistent structured concurrency
- replace httpx client with NextcloudClient in upload command

### Perf

- Eliminate double-fetching in semantic search sampling
- fix vector viz search performance and visual encoding
- make note deletion concurrent in upload --force

## nextcloud-mcp-server-0.36.0 (2025-11-15)

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

## nextcloud-mcp-server-0.35.0 (2025-11-15)

### Feat

- Enable SSE transport for mcp service and update test fixtures

## nextcloud-mcp-server-0.34.2 (2025-11-13)

### Fix

- Use NEXTCLOUD_OIDC_CLIENT_ID/SECRET env vars consistently
- return all notes when search query is empty

## nextcloud-mcp-server-0.34.0 (2025-11-13)

### Feat

- Complete Phase 5 - Instrument all 93 MCP tools
- Add instrumentation decorator and apply to notes tools (Phase 5)
- Add OAuth token and database metrics (Phases 3-4)
- Add metrics instrumentation for queue, health, and database operations

## nextcloud-mcp-server-0.33.1 (2025-11-13)

### Fix

- Move grafana_folder from labels to annotations

## nextcloud-mcp-server-0.33.0 (2025-11-13)

### Feat

- Add Grafana dashboard and vector sync metric instrumentation

## nextcloud-mcp-server-0.32.1 (2025-11-12)

### Fix

- add dynamic dimension detection for Ollama embedding models

## nextcloud-mcp-server-0.32.0 (2025-11-11)

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

## nextcloud-mcp-server-0.31.1 (2025-11-10)

### Refactor

- simplify OpenTelemetry tracing configuration

## nextcloud-mcp-server-0.31.0 (2025-11-10)

### Feat

- skip tracing for health and metrics endpoints

### Fix

- add retry logic for ETag conflicts in category change test
- optimize Notes API pagination with pruneBefore parameter

## nextcloud-mcp-server-0.30.0 (2025-11-10)

### Feat

- **helm**: Add document chunking configuration
- **vector**: Add configurable chunk size and overlap for document embedding
- **vector**: Support multiple embedding models with auto-generated collection names

### Fix

- Support in-memory Qdrant for CI testing

## nextcloud-mcp-server-0.29.2 (2025-11-09)

### Fix

- **helm**: Set default strategy to Recreate

## nextcloud-mcp-server-0.29.1 (2025-11-09)

### Fix

- **observability**: isolate metrics endpoint to dedicated port

## nextcloud-mcp-server-0.29.0 (2025-11-09)

### Feat

- **helm**: Add observability support with ServiceMonitor and Grafana dashboard

### Fix

- **readiness**: Only check external Qdrant in network mode

## nextcloud-mcp-server-0.28.0 (2025-11-09)

### Feat

- **observability**: Add comprehensive monitoring with Prometheus and OpenTelemetry

### Fix

- **vector**: Handle missing 'modified' field in notes gracefully

## nextcloud-mcp-server-0.27.3 (2025-11-09)

### Fix

- **ci**: Use helm dependency build instead of update to use Chart.lock

## nextcloud-mcp-server-0.27.2 (2025-11-09)

### Fix

- **helm**: update Qdrant dependency condition to match new mode structure

## nextcloud-mcp-server-0.27.1 (2025-11-09)

### Feat

- **helm**: add Qdrant local mode support with three deployment options [skip ci]
- add Qdrant local mode support with in-memory and persistent storage
- implement ADR-009 - refactor semantic search to use generic semantic:read scope
- implement MCP sampling for semantic search RAG (ADR-008)
- add optional vector database and semantic search to helm chart
- add vector sync processing status to /user/page endpoint
- implement semantic search tool and fix vector sync issues (ADR-007 Phase 3)
- implement vector sync scanner and processor (ADR-007 Phase 2)

### Fix

- **ci**: add Helm repository setup to chart release workflow
- implement deletion grace period and vector sync status tool
- remove unnecessary urllib3<2.0 constraint
- integrate vector sync tasks with Starlette lifespan for streamable-http

### Refactor

- migrate vector sync from asyncio.Queue to anyio memory object streams
- update to Qdrant query_points API and fix Playwright Keycloak login

## nextcloud-mcp-server-0.26.1 (2025-11-08)

### Fix

- **deps**: update dependency mcp to >=1.21,<1.22

## nextcloud-mcp-server-0.26.0 (2025-11-08)

### Feat

- add real elicitation integration test with python-sdk MCP client
- unify session architecture and enhance login status visibility

### Fix

- Consolidate OAuth callbacks and implement PKCE for all flows

## nextcloud-mcp-server-0.25.0 (2025-11-05)

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

## nextcloud-mcp-server-0.24.1 (2025-11-04)

### Fix

- **deps**: update dependency mcp to >=1.20,<1.21

## nextcloud-mcp-server-0.24.0 (2025-11-04)

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

## nextcloud-mcp-server-0.23.0 (2025-11-03)

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

## nextcloud-mcp-server-0.22.7 (2025-10-29)

### Fix

- **helm**: Remove image tag overide

## nextcloud-mcp-server-0.22.6 (2025-10-29)

### Fix

- **helm**: Update helm chart with extraArgs

## nextcloud-mcp-server-0.22.5 (2025-10-29)

### Fix

- Update helm chart variables

## nextcloud-mcp-server-0.22.4 (2025-10-29)

### Fix

- **helm**: Update helm version with release
- **helm**: Update helm version with release
- **helm**: Update helm version with release

## nextcloud-mcp-server-0.1.1 (2025-10-29)

### Fix

- **helm**: Update helm version with release
- Trigger release

## nextcloud-mcp-server-0.1.0 (2025-10-29)

### BREAKING CHANGE

- FASTMCP_-prefixed env vars have been replaced by CLI
arguments. Refer to the README for updated usage.

### Feat

- **server**: Add /live & /health endpoints
- Initialize helm chart
- Add text processing background worker for telling client about progress
- **auth**: Add support for client registration deletion
- Split read/write scopes into app:read/write scopes
- Enable token introspection for opaque tokens
- **server**: Add support for custom OIDC scopes and permissions via JWTs
- Initialize JWT-scoped tools
- **caldav**: Add support for tasks
- **webdav**: Add search and list favorite response tools
- **cookbook**: Add full Cookbook app support with 13 tools and 2 resources
- Add Groups API client
- add sharing API client and server tools
- **server**: Experimental support for OAuth2/OIDC authentication
- **users**: Initialize user API client
- **server**: Add support for `streamable-http` transport type
- Add WebDAV resource copy functionality
- Add WebDAV resource move/rename functionality
- **deck**: Add support for stack, cards, labels
- **deck**: Initialize Deck app client/server
- **cli**: Replace `mcp run` with click CLI and runtime options
- **client**: Preserve fields when modifying contacts/calendar resources
- **server**: Add structured output to all tool/resource output
- **contacts**: Initialize Contacts App
- **calendar**: add comprehensive Calendar app support via CalDAV protocol
- Update webdav client create_directory method to handle recursive directories
- **webdav**: add complete file system support
- Add TablesClient and associated tools
- Switch to using async client
- **notes**: Add append to note functionality

### Fix

- Add support for RFC 7592 client registration and deletion
- Update webdav models for proper serialization
- **deps**: update dependency mcp to >=1.19,<1.20
- Add CORS middleware to allow browser-based clients like MCP Inspector
- Use occ-created OAuth clients with allowed_scopes for all tests
- Separate OAuth fixtures for opaque vs JWT tokens
- **caldav**: Fix caldav search() due to missing todos
- **caldav**: Check that calendar exists after creation to avoid race condition
- **caldav**: Properly parse datetimes as vDDDTypes
- Increase HTTP client timeout to 30s
- Handle RequestError in mcp tools
- **deps**: update dependency mcp to >=1.18,<1.19
- **deps**: update dependency pillow to v12
- **oauth**: Remove the option to force_register new clients
- Update user/groups API to OCS v2
- **deps**: update dependency mcp to >=1.17,<1.18
- **deps**: update dependency mcp to >=1.16,<1.17
- **deps**: update dependency mcp to >=1.15,<1.16
- **docker**: Provide --host 0.0.0.0 in default docker image
- **deps**: update dependency mcp to >=1.13,<1.14
- **server**: Replace ErrorResponses with standard McpErrors
- **notes**: Include ETags in responses to avoid accidently updates
- **notes**: Remove note contents from responses to reduce token usage
- **model**: Serialize timestamps in RFC3339 format
- **client**: Use paging to fetch all notes
- **client**: Strip cookies from responses to avoid falsely raising CSRF errors
- **calendar**: Fix iCalendar date vs datetime format
- **calendar**: Remove try/except in calendar API
- apply ruff formatting to pass CI checks
- **calendar**: address PR feedback from maintainer
- apply ruff formatting to test_webdav_operations.py
- **deps**: update dependency mcp to >=1.10,<1.11
- update tests
- Commitizen release process
- Do not update dependencies when running in Dockerfile
- Configure logging
- Limit search results to notes with score > 0.5
- Install deps before checking service
- **deps**: update dependency mcp to >=1.9,<1.10

### Refactor

- Transform document parsing into pluggable processor architecture
- Update JWT client to use DCR, re-enable tool filtering
- Migrate from internal CalendarClient to caldav library
- Unify logging & remove factory deployment
- Add tools for all resources to enable tool-only workflows
- Add `http` to --transport option
- Use _make_request where available
- **calendar**: optimize logging for production readiness
- Modularize NC and Notes app client

### Perf

- **notes**: Improve notes search performance using async iterators
