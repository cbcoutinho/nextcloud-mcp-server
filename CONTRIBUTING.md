# Contributing to Nextcloud MCP Server

Thank you for your interest in contributing! This project is licensed under
AGPL-3.0 and welcomes contributions from the community.

## Contributor License Agreement (CLA)

> **DRAFT — NOT YET IN EFFECT.** The CLA process described below is a planned
> change. Until the [CLA](./CLA.md) is finalized and the CLA Assistant workflow
> is enabled on this repository, no signature is required.

To keep the project's licensing flexible — including the ability to offer
commercial licensing alongside AGPL-3.0 in the future — we ask all contributors
to sign our [Contributor License Agreement](./CLA.md). The CLA gives the
project maintainer the rights needed to relicense or dual-license the project
while letting you retain copyright in your contributions.

**How signing works:**

1. Open a pull request as you normally would.
2. The CLA Assistant bot ([cla-assistant.io](https://cla-assistant.io)) will
   comment on your PR with a link inviting you to sign.
3. Click the link, review the agreement, and sign in with your GitHub account
   to record your signature.
4. Your signature is recorded once per GitHub account. Subsequent PRs are
   covered automatically.

If you contribute on behalf of an employer, please confirm with your employer
that you are permitted to contribute under the CLA. For larger organizations,
a separate Corporate CLA may be required — open an issue if this applies to
you.

## Adding a Dependency

The project distributes under AGPL-3.0 today and Astrolabe Cloud intends to
offer commercial dual-licensed builds in the future, so every direct or
transitive dependency must be licensed under terms that allow both. The
allowlist and per-package exceptions live in
[`.licenses/policy.toml`](./.licenses/policy.toml) and are enforced by the
**License check** workflow on every PR.

Before opening a PR that adds or upgrades a dependency:

```bash
uv run scripts/check-licenses.py
```

If the script flags a new package, either:

- the package is permissively licensed → add the license string to the
  `[allowed]` block in `.licenses/policy.toml`, **or**
- the package is copyleft / dual-licensed and you have a justification → add
  it under `[exceptions]` with `rationale`, `allowed_for`, and
  `review_required_for` fields, **or**
- the metadata is wrong → add an `[overrides]` entry pointing at the actual
  license file shipped in the wheel, **or**
- replace the dep with a permissively-licensed alternative.

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
