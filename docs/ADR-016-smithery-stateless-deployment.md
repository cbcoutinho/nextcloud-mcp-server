# ADR-016: Smithery Stateless Deployment for Multi-User Public Nextcloud Instances

**Status:** Deprecated — removed in v0.66.0
**Date:** 2025-01-22
**Deprecated:** 2026-03-22
**Related:** ADR-022 (Deployment Mode Consolidation)

This ADR proposed a stateless deployment mode for hosting the MCP server on the
[Smithery](https://smithery.ai) platform: per-session configuration from URL
parameters, no persistent storage, no background vector sync.

**It was removed in v0.66.0.** Smithery sunsetted its free tier, and routing user
data through third-party hosting conflicts with this project's self-hosted,
privacy-first design. The mode, its `SMITHERY_*` configuration and its validation
path no longer exist in the codebase. See ADR-022 for the full rationale and the
surviving deployment modes.

The original design is preserved in git history
(`git log --follow docs/ADR-016-smithery-stateless-deployment.md`).
