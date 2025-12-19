# Changelog - Astrolabe

All notable changes to the Astrolabe Nextcloud app will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


### Added

- Initial alpha release
- Semantic search across Notes, Files, Calendar, Deck, and Contacts
- Integration with Nextcloud Unified Search
- Personal settings UI for MCP server configuration
- Admin settings for global MCP server URL
- OAuth PKCE authentication flow
- Vector visualization of semantic relationships
- Hybrid search combining semantic and keyword matching
- Background content indexing
- Support for Nextcloud 30-32

### Notes

- This is an alpha release intended for early adopters and testing
- Requires external MCP server deployment
- See documentation for setup: https://github.com/cbcoutinho/nextcloud-mcp-server

## astrolabe-v0.4.3 (2025-12-19)

### Fix

- **astrolabe**: screenshots in info.xml

## astrolabe-v0.4.2 (2025-12-19)

### Fix

- **astrolabe**: Update screenshots
- **ci**: skip existing Helm chart releases to prevent duplicate release errors

## astrolabe-v0.4.1 (2025-12-19)

## astrolabe-v0.4.0 (2025-12-19)

### Feat

- **ci**: add --increment flag to bump scripts for manual version control

## astrolabe-v0.3.2 (2025-12-19)

### Fix

- **astrolabe**: add contents:write permission to appstore workflow

## astrolabe-v0.3.1 (2025-12-19)

### Fix

- **astrolabe**: update commitizen pattern to properly update info.xml version

## astrolabe-v0.3.0 (2025-12-19)

### Fix

- **astrolabe**: prevent workflow failure when only helm/astrolabe commits exist
- **astrolabe**: info.xml

## astrolabe-v0.2.1 (2025-12-19)

### BREAKING CHANGE

- MCP server now bumps for ANY conventional commit except
those explicitly scoped to helm or astrolabe.

### Fix

- **ci**: push all tags explicitly in bump workflow
- **ci**: make MCP server default bump target for all non-scoped commits
- **ci**: restrict docker build to MCP server tags only
- **ci**: correct appstore-push-action version to v1.0.4

## astrolabe-v0.2.0 (2025-12-19)

### BREAKING CHANGE

- Search algorithms now require Qdrant to be populated.
Vector sync must be enabled and documents indexed for search to work.
- All OAuth deployments must be reconfigured to specify
resource URIs (NEXTCLOUD_MCP_SERVER_URL and NEXTCLOUD_RESOURCE_URI) and
choose between multi-audience or token exchange mode.
- FASTMCP_-prefixed env vars have been replaced by CLI
arguments. Refer to the README for updated usage.

### Feat

- **ci**: implement monorepo-aware version bumping workflow
- **astrolabe**: add Nextcloud App Store deployment automation
- configure commitizen monorepo with independent versioning
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
- **vector**: add Deck card vector search with visualization support
- **vector-viz**: add news_item support for links and chunk expansion
- add MCP tool annotations for enhanced UX
- **news**: add Nextcloud News app integration
- Add tag management methods to WebDAV client
- Add OpenAI provider support for embeddings and generation
- Add Smithery CLI deployment support
- Implement ADR-016 Smithery stateless deployment mode
- Add context expansion to semantic search with chunk overlap removal
- Use Ollama native batch API in embed_batch()
- Implement Qdrant placeholder state management
- Switch files to use numeric IDs with file_path resolution
- Implement per-chunk vector visualization with context expansion
- Improve vector visualization with static assets and fixes
- Redesign UI to match Nextcloud ecosystem aesthetic
- Replace custom document chunker with LangChain MarkdownTextSplitter
- **viz**: Add dual-score display and improve UI controls
- add configurable fusion algorithms for BM25 hybrid search
- add chunk position tracking to vector indexing and search
- add vector viz template and chunk context endpoint
- add unified provider architecture with Amazon Bedrock support
- add concurrent uploads and --force flag to upload command
- implement RAG evaluation framework with CLI tooling
- Add OpenTelemetry tracing to @instrument_tool decorator
- Implement BM25 hybrid search with native Qdrant RRF fusion
- Normalize hybrid search RRF scores to 0-1 range
- Enhance vector visualization UI and parallelize search verification
- Add Vector Viz tab to app home page
- Add vector visualization pane with multi-select document types
- Implement custom PCA to remove sklearn dependency
- Add multi-document Protocol with cross-app search support
- Update nc_semantic_search tool with algorithm selection
- Implement unified search algorithm module
- Enable SSE transport for mcp service and update test fixtures
- Complete Phase 5 - Instrument all 93 MCP tools
- Add instrumentation decorator and apply to notes tools (Phase 5)
- Add OAuth token and database metrics (Phases 3-4)
- Add metrics instrumentation for queue, health, and database operations
- Add Grafana dashboard and vector sync metric instrumentation
- **ollama**: Pull model on startup if not available in ollama
- add dynamic vector sync status updates with htmx polling
- add webhook management UI and BeforeNodeDeletedEvent support
- validate Nextcloud webhook schemas and document findings
- skip tracing for health and metrics endpoints
- **helm**: Add document chunking configuration
- **vector**: Add configurable chunk size and overlap for document embedding
- **vector**: Support multiple embedding models with auto-generated collection names
- **helm**: Add observability support with ServiceMonitor and Grafana dashboard
- **observability**: Add comprehensive monitoring with Prometheus and OpenTelemetry
- **helm**: add Qdrant local mode support with three deployment options [skip ci]
- add Qdrant local mode support with in-memory and persistent storage
- implement ADR-009 - refactor semantic search to use generic semantic:read scope
- implement MCP sampling for semantic search RAG (ADR-008)
- add optional vector database and semantic search to helm chart
- add vector sync processing status to /user/page endpoint
- implement semantic search tool and fix vector sync issues (ADR-007 Phase 3)
- implement vector sync scanner and processor (ADR-007 Phase 2)
- add real elicitation integration test with python-sdk MCP client
- unify session architecture and enhance login status visibility
- Implement ADR-005 unified token verifier to eliminate token passthrough vulnerability
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
- Auto-configure impersonation role in Keycloak realm import
- Implement dual-tier token exchange (Standard V2 + Legacy V1 impersonation)
- Add Keycloak external IdP integration with custom scopes
- Implement RFC 8693 token exchange for Keycloak (ADR-002 Tier 2)
- Add Keycloak OAuth provider support with refresh token storage
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

- **ci**: improve versioning and error handling
- **ci**: address critical workflow and validation issues
- **astrolabe**: address code review feedback
- **security**: address critical security issues from PR #401 code review
- **oauth**: enable PKCE for all clients and add token_broker to oauth_context
- **astrolabe**: revert invalid files_pdfviewer URL for file links
- resolve type checking warnings for CI
- move Alembic to package submodule for Docker compatibility
- update unified search results to match chunk viz display
- **astrolabe**: handle OAuth refresh token rotation
- address critical code review issues (4 fixes)
- resolve CI linting issues for Astroglobe
- **news**: revert get_item() to use get_items() + filter
- Disable DNS rebinding protection for containerized deployments
- **deps**: update dependency mcp to >=1.23,<1.24
- address PR review feedback
- Update lockfile
- Revert mcp version <1.23
- resolve all type checking errors (8 errors fixed)
- **deps**: update dependency mcp to >=1.23,<1.24
- **deps**: update dependency pillow to v12
- Add rate limit retry logic to OpenAI provider
- Increase MCP sampling timeout to 5 minutes for slower LLMs
- Share vector sync state with FastMCP session lifespan via module singleton
- Share vector sync state with FastMCP session lifespan via module singleton
- Use WebDAV for tag creation and add LLM-as-a-judge for RAG tests
- **smithery**: Enable JSON response format for scanner compatibility
- **smithery**: Add JSON Schema metadata to mcp-config endpoint
- **smithery**: Use container runtime pattern for config discovery
- Add Smithery lifespan and auth mode detection
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
- **deps**: update dependency mcp to >=1.22,<1.23
- Improve 3D plot rendering with explicit dimensions and window resize support
- Preserve 3D plot camera and improve documentation
- Preserve 3D plot camera position and fix CSS loading
- prevent infinite loop in DocumentChunker with position tracking
- Relax SearchResult validation to support DBSF fusion scores > 1.0
- suppress Starlette middleware type warnings in ty checker
- download qrels from BEIR ZIP instead of HuggingFace
- Handle named vectors in visualization and semantic search
- Update vizApp to use bm25_hybrid algorithm and remove deprecated weights
- Update viz routes to use BM25 hybrid search after refactor
- Reorder tabs and fix viz pane session access
- Use NEXTCLOUD_OIDC_CLIENT_ID/SECRET env vars consistently
- return all notes when search query is empty
- Move grafana_folder from labels to annotations
- add dynamic dimension detection for Ollama embedding models
- improve webapp tab UI with CSS Grid and viewport-filling container
- add retry logic for ETag conflicts in category change test
- optimize Notes API pagination with pruneBefore parameter
- Support in-memory Qdrant for CI testing
- **helm**: Set default strategy to Recreate
- **observability**: isolate metrics endpoint to dedicated port
- **readiness**: Only check external Qdrant in network mode
- **vector**: Handle missing 'modified' field in notes gracefully
- **ci**: Use helm dependency build instead of update to use Chart.lock
- **helm**: update Qdrant dependency condition to match new mode structure
- **ci**: add Helm repository setup to chart release workflow
- implement deletion grace period and vector sync status tool
- remove unnecessary urllib3<2.0 constraint
- integrate vector sync tasks with Starlette lifespan for streamable-http
- **deps**: update dependency mcp to >=1.21,<1.22
- Consolidate OAuth callbacks and implement PKCE for all flows
- Implement proper OAuth resource parameters and PRM-based discovery
- Simplify token verifier to be RFC 7519 compliant
- Use Keycloak client ID for NEXTCLOUD_RESOURCE_URI in token exchange
- Correct OAuth token audience validation for multi-audience mode
- **deps**: update dependency mcp to >=1.20,<1.21
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
- Complete Keycloak external IdP integration with all tests passing
- Complete Keycloak external IdP integration with all tests passing
- Update DCR token_type tests for OIDC app changes
- **helm**: Remove image tag overide
- **helm**: Update helm chart with extraArgs
- Update helm chart variables
- **helm**: Update helm version with release
- **helm**: Update helm version with release
- **helm**: Update helm version with release
- **helm**: Update helm version with release
- Trigger release
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

- **astrolabe**: extract PDF viewer to dedicated component
- **astrolabe**: reframe UI as semantic search service
- **news**: simplify vector sync to fetch all items
- Move background tasks to server lifespan and deprecate SSE transport
- Simplify PDF text extraction with single to_markdown call
- migrate asyncio to anyio for consistent structured concurrency
- replace httpx client with NextcloudClient in upload command
- Optimize Nextcloud access verification with centralized filtering
- Make all search algorithms query Qdrant payload, not Nextcloud
- move webapp from /user/page to /app
- consolidate database storage for webhooks and OAuth tokens
- simplify OpenTelemetry tracing configuration
- migrate vector sync from asyncio.Queue to anyio memory object streams
- update to Qdrant query_points API and fix Playwright Keycloak login
- Eliminate duplicate validation logic in UnifiedTokenVerifier
- integrate token exchange into unified get_client() pattern
- Remove NEXTCLOUD_OIDC_CLIENT_STORAGE environment variable
- Remove unnecessary user_oidc patch - CORSMiddleware patch is sufficient
- Unify OAuth configuration to be provider-agnostic
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

- **deck**: optimize card lookup by storing board_id/stack_id in metadata
- **news**: use direct API endpoint for get_item()
- Optimize vector viz search performance
- Optimize PDF processing with parallel extraction and single-render highlights
- Eliminate double-fetching in semantic search sampling
- fix vector viz search performance and visual encoding
- make note deletion concurrent in upload --force
- Exclude vector-sync status polling from distributed tracing
- **notes**: Improve notes search performance using async iterators
