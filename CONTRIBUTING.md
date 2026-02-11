# Contributing to Nextcloud MCP Server

## Version Management

This monorepo uses commitizen for version management with **independent versioning** for two components:

### Components

| Component | Scope | Bump Command | Tag Example |
|-----------|-------|--------------|-------------|
| MCP Server | `mcp` or none | `./scripts/bump-mcp.sh` | `v0.54.0` |
| Helm Chart | `helm` | `./scripts/bump-helm.sh` | `nextcloud-mcp-server-0.54.0` |

> **Note:** The Astrolabe Nextcloud app has been moved to its own repository at [cbcoutinho/astrolabe](https://github.com/cbcoutinho/astrolabe).

### Commit Message Format

Use conventional commits with **scopes** to target specific components:

```bash
# MCP server changes
feat(mcp): add calendar sync API
fix(mcp): resolve authentication bug

# Helm chart changes
feat(helm): add resource limits
docs(helm): update values documentation
```

**Unscoped commits** default to the MCP server:
```bash
feat: add new feature  # → MCP server (v0.54.0)
```

### Release Workflow

#### 1. Make Changes with Scoped Commits

```bash
git commit -m "feat(helm): add ingress annotations"
git commit -m "feat(mcp): add calendar sync"
```

#### 2. Bump Component Versions

```bash
# Bump MCP server (reads commits with scope=mcp or unscoped)
./scripts/bump-mcp.sh
# → Creates tag: v0.54.0
# → Updates: pyproject.toml, Chart.yaml:appVersion

# Bump Helm chart (reads commits with scope=helm)
./scripts/bump-helm.sh
# → Creates tag: nextcloud-mcp-server-0.54.0
# → Updates: Chart.yaml:version

```

#### 3. Push Tags

```bash
git push --follow-tags
```

### Changelog Filtering

Each component maintains its own `CHANGELOG.md`:

- **MCP Server**: `CHANGELOG.md` (root) - includes `feat(mcp):` and unscoped commits
- **Helm Chart**: `charts/nextcloud-mcp-server/CHANGELOG.md` - includes `feat(helm):` only

### Manual Version Bumps

For specific increments:

```bash
# Patch bump (0.53.0 → 0.53.1)
uv run cz bump --increment PATCH

# Minor bump (0.53.0 → 0.54.0)
uv run cz bump --increment MINOR

# Major bump (0.53.0 → 1.0.0)
uv run cz bump --increment MAJOR

# For non-MCP components, use --config
cd charts/nextcloud-mcp-server
uv run cz --config .cz.toml bump --increment MINOR
```

### Versioning Philosophy

- **MCP Server**: Follows PEP 440, `major_version_zero = true` (0.x.x for pre-1.0)
- **Helm Chart**: Follows PEP 440, starts at 0.53.0 (continues from current)

### Chart.yaml Version vs appVersion

The Helm chart has TWO version fields:

- **`version`**: Chart packaging version (bumped by `feat(helm):`)
  - Example: `0.53.0` → `0.54.0` when adding resource limits

- **`appVersion`**: MCP server version being deployed (bumped by `feat(mcp):`)
  - Example: `"0.53.0"` → `"0.54.0"` when MCP server releases

This allows the chart to evolve independently from the application.
