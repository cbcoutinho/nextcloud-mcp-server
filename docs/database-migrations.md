# Database Migrations

This document describes the database migration system for nextcloud-mcp-server's token storage database.

## Overview

The token storage database uses [Alembic](https://alembic.sqlalchemy.org/) for schema versioning and migrations. Alembic provides:

- **Version Control**: Track schema changes in Git
- **Rollback Support**: Safely downgrade schema if needed
- **Audit Trail**: Migration files serve as schema changelog
- **Automated Upgrades**: Database schema updates automatically on startup

## Architecture

### Migration Strategy

The system handles three scenarios:

1. **New Database**: Runs migrations from scratch to create all tables
2. **Pre-Alembic Database**: Stamps existing database with initial revision (no changes)
3. **Alembic-Managed Database**: Upgrades to latest version automatically

### Directory Structure

```
nextcloud-mcp-server/
├── alembic/                              # Alembic migrations
│   ├── versions/                         # Migration scripts
│   │   └── 20251217_2200_001_initial_schema.py
│   ├── env.py                            # Alembic environment
│   ├── script.py.mako                    # Migration template
│   └── README                            # Migration usage guide
├── alembic.ini                           # Alembic configuration
└── nextcloud_mcp_server/
    ├── auth/storage.py                   # Uses migrations on init
    └── migrations.py                     # Migration utilities
```

## Usage

### Automatic Migration on Startup

Migrations run automatically when the server starts:

```bash
uv run nextcloud-mcp-server
```

The `RefreshTokenStorage.initialize()` method:
1. Checks if database is Alembic-managed
2. Stamps pre-Alembic databases with initial revision
3. Upgrades to latest version

### Manual Migration Commands

```bash
# Show current database version
uv run nextcloud-mcp-server db current

# Upgrade database to latest version
uv run nextcloud-mcp-server db upgrade

# Show migration history
uv run nextcloud-mcp-server db history

# Downgrade by one version (emergency use only)
uv run nextcloud-mcp-server db downgrade

# Specify custom database path
uv run nextcloud-mcp-server db current -d /path/to/tokens.db
```

### Environment Variables

- `TOKEN_STORAGE_DB`: Path to database file (default: `/app/data/tokens.db`)

## Creating Migrations (Developers)

### Step 1: Create Migration File

```bash
uv run nextcloud-mcp-server db migrate "add user preferences table"
```

This creates a new migration file in `alembic/versions/` with empty `upgrade()` and `downgrade()` functions.

### Step 2: Write Migration SQL

Since we don't use SQLAlchemy models, write raw SQL:

```python
def upgrade() -> None:
    """Add user preferences table."""
    op.execute("""
        CREATE TABLE user_preferences (
            user_id TEXT PRIMARY KEY,
            theme TEXT DEFAULT 'light',
            language TEXT DEFAULT 'en',
            created_at INTEGER NOT NULL
        )
    """)

    op.execute("""
        CREATE INDEX idx_user_preferences_user_id
        ON user_preferences(user_id)
    """)


def downgrade() -> None:
    """Remove user preferences table."""
    op.execute("DROP INDEX IF EXISTS idx_user_preferences_user_id")
    op.execute("DROP TABLE IF EXISTS user_preferences")
```

### Step 3: Test Migration

```bash
# Test upgrade
uv run nextcloud-mcp-server db upgrade -d /tmp/test.db

# Verify schema
sqlite3 /tmp/test.db ".schema"

# Test downgrade
uv run nextcloud-mcp-server db downgrade -d /tmp/test.db

# Verify removal
sqlite3 /tmp/test.db ".schema"
```

### Step 4: Commit Migration

```bash
git add alembic/versions/YYYYMMDD_HHMM_XXX_description.py
git commit -m "feat: add user preferences table migration"
```

## SQLite Limitations

SQLite has limited `ALTER TABLE` support:

### Supported Operations

- ✅ Add columns: `ALTER TABLE table ADD COLUMN ...`
- ✅ Rename table: `ALTER TABLE old RENAME TO new`
- ✅ Rename column: `ALTER TABLE table RENAME COLUMN old TO new` (SQLite 3.25+)

### Unsupported Operations (Requires Table Recreation)

- ❌ Drop column
- ❌ Change column type
- ❌ Add constraints to existing columns

### Table Recreation Pattern

For complex schema changes:

```python
def upgrade() -> None:
    # Create new table with desired schema
    op.execute("""
        CREATE TABLE refresh_tokens_new (
            user_id TEXT PRIMARY KEY,
            encrypted_token BLOB NOT NULL,
            new_field TEXT,  -- New column
            expires_at INTEGER,
            created_at INTEGER NOT NULL
        )
    """)

    # Copy data from old table
    op.execute("""
        INSERT INTO refresh_tokens_new
        (user_id, encrypted_token, expires_at, created_at)
        SELECT user_id, encrypted_token, expires_at, created_at
        FROM refresh_tokens
    """)

    # Drop old table and rename new table
    op.execute("DROP TABLE refresh_tokens")
    op.execute("ALTER TABLE refresh_tokens_new RENAME TO refresh_tokens")

    # Recreate indexes
    op.execute("CREATE INDEX idx_user_id ON refresh_tokens(user_id)")
```

## Best Practices

### Naming Conventions

- **Migrations**: `YYYYMMDD_HHMM_XXX_description.py`
- **Revision IDs**: Sequential numbers (`001`, `002`, `003`)
- **Descriptions**: Imperative mood ("add table", "remove column")

### Migration Guidelines

1. **Test Thoroughly**: Test both upgrade and downgrade paths
2. **Preserve Data**: Ensure data migration logic is correct
3. **Document Changes**: Add comments explaining complex operations
4. **Small Changes**: One logical change per migration
5. **No Breaking Changes**: Maintain backward compatibility when possible

### Downgrade Considerations

- **Data Loss**: Downgrade may lose data (dropped columns, tables)
- **Confirmation**: Downgrade command requires explicit confirmation
- **Testing**: Always test downgrade path before deploying
- **Emergency Only**: Use downgrades only for critical rollbacks

## Backward Compatibility

### Pre-Alembic Databases

Existing databases created before Alembic integration are automatically detected and stamped with revision `001`:

1. Server detects no `alembic_version` table
2. Checks if `refresh_tokens` table exists
3. If yes, stamps database with `001` (no schema changes)
4. Future updates use normal migration path

### Migration Path

```
Pre-Alembic DB → Stamp(001) → Upgrade(002) → Upgrade(003) → ...
New DB → Migrate(001) → Upgrade(002) → Upgrade(003) → ...
```

## Troubleshooting

### Migration Fails

```bash
# Check current state
uv run nextcloud-mcp-server db current -d /path/to/tokens.db

# View migration history
uv run nextcloud-mcp-server db history -d /path/to/tokens.db

# Manually inspect database
sqlite3 /path/to/tokens.db ".schema"
```

### Reset to Initial State

**WARNING: This destroys all data!**

```bash
# Downgrade to base (empty database)
uv run nextcloud-mcp-server db downgrade -d /path/to/tokens.db --revision base

# Upgrade to latest
uv run nextcloud-mcp-server db upgrade -d /path/to/tokens.db
```

### Corrupted Migration State

If `alembic_version` table is corrupted:

```bash
# Manually fix via SQL
sqlite3 /path/to/tokens.db
> DELETE FROM alembic_version;
> INSERT INTO alembic_version (version_num) VALUES ('001');
> .quit

# Verify and upgrade
uv run nextcloud-mcp-server db current -d /path/to/tokens.db
uv run nextcloud-mcp-server db upgrade -d /path/to/tokens.db
```

## CI/CD Integration

### Pre-Deployment

```bash
# Run migrations in test environment
export TOKEN_STORAGE_DB=/app/data/tokens.db
uv run nextcloud-mcp-server db upgrade

# Verify current version
uv run nextcloud-mcp-server db current
```

### Docker Deployment

Migrations run automatically on container startup via `RefreshTokenStorage.initialize()`.

### Rollback Plan

1. Stop application
2. Backup database: `cp tokens.db tokens.db.backup`
3. Downgrade: `uv run nextcloud-mcp-server db downgrade --revision XXX`
4. Deploy previous application version
5. Restart application

## References

- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLite ALTER TABLE Limitations](https://www.sqlite.org/lang_altertable.html)
- [ADR-004: Progressive Consent](./ADR-004-progressive-consent.md) (migration 001)
