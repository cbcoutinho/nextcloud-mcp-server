#!/bin/bash
#
# Apply upstream CORSMiddleware Bearer token authentication patch
#
# This patch allows Bearer tokens to bypass CORS/CSRF checks, fixing
# authentication issues with app-specific APIs (Notes, Calendar, etc.)
# when using OAuth/OIDC Bearer tokens.
#
# Upstream PR: https://github.com/nextcloud/server/pull/55878
# Commit: 8fb5e77db82 (fix(cors): Allow Bearer token authentication)
#

set -e

PATCH_FILE="/docker-entrypoint-hooks.d/patches/cors-bearer-token.patch"
TARGET_FILE="/var/www/html/lib/private/AppFramework/Middleware/Security/CORSMiddleware.php"

echo "===================================================================="
echo "Applying CORSMiddleware Bearer token authentication patch..."
echo "===================================================================="

# Check if patch file exists
if [ ! -f "$PATCH_FILE" ]; then
    echo "⚠ Warning: Patch file not found: $PATCH_FILE"
    echo "  Skipping CORS Bearer token patch"
    exit 0
fi

# Check if target file exists
if [ ! -f "$TARGET_FILE" ]; then
    echo "⚠ Warning: Target file not found: $TARGET_FILE"
    echo "  Skipping CORS Bearer token patch"
    exit 0
fi

# Check if already patched
if grep -q "Allow Bearer token authentication for CORS requests" "$TARGET_FILE"; then
    echo "✓ CORSMiddleware already patched for Bearer token support"
    exit 0
fi

echo "Applying patch to CORSMiddleware.php..."

# Apply the patch
cd /var/www/html
if patch -p1 --dry-run < "$PATCH_FILE" > /dev/null 2>&1; then
    patch -p1 < "$PATCH_FILE"
    echo "✓ Patch applied successfully"
else
    echo "⚠ Warning: Patch failed to apply (may already be applied or file changed)"
    echo "  This is expected if using a Nextcloud version that already includes the fix"
    exit 0
fi

echo ""
echo "===================================================================="
echo "✓ CORSMiddleware Bearer token patch applied"
echo "===================================================================="
echo ""
echo "Benefits:"
echo "  • Bearer tokens now work with app-specific APIs (Notes, Calendar, etc.)"
echo "  • OAuth/OIDC authentication works without CORS errors"
echo "  • Stateless API authentication is properly supported"
echo ""
