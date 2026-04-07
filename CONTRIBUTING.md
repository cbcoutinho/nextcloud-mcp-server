# Contributing to Nextcloud MCP Server

## Version Management

This project uses [commitizen](https://commitizen-tools.github.io/commitizen/) for version management following PEP 440 (`major_version_zero = true`, 0.x.x for pre-1.0).

> **Note:** The Helm chart has been moved to [cbcoutinho/helm-charts](https://github.com/cbcoutinho/helm-charts). The Astrolabe Nextcloud app has been moved to [cbcoutinho/astrolabe](https://github.com/cbcoutinho/astrolabe).

### Commit Message Format

Use [conventional commits](https://www.conventionalcommits.org/):

```bash
feat: add new feature
feat(mcp): add calendar sync API
fix: resolve authentication bug
docs: update README
```

### Release Workflow

#### 1. Make Changes with Conventional Commits

```bash
git commit -m "feat: add calendar sync"
```

#### 2. Bump Version

```bash
./scripts/bump-mcp.sh
# → Creates tag: v0.54.0
# → Updates: pyproject.toml
```

#### 3. Push Tags

```bash
git push --follow-tags
```

### Manual Version Bumps

For specific increments:

```bash
# Patch bump (0.53.0 → 0.53.1)
uv run cz bump --increment PATCH

# Minor bump (0.53.0 → 0.54.0)
uv run cz bump --increment MINOR

# Major bump (0.53.0 → 1.0.0)
uv run cz bump --increment MAJOR
```
