#!/bin/bash

set -e

echo "====================================="
echo "Testing MCP Server UI App Installation"
echo "====================================="

cd "$(dirname "$0")"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "\n${YELLOW}Step 1: Stopping existing services...${NC}"
docker compose down

echo -e "\n${YELLOW}Step 2: Starting services...${NC}"
docker compose up -d app mcp-oauth

echo -e "\n${YELLOW}Step 3: Waiting for Nextcloud to be healthy...${NC}"
timeout=180
elapsed=0
while ! docker compose exec -T app curl -f http://localhost/status.php 2>/dev/null | grep -q '"installed":true'; do
    if [ $elapsed -ge $timeout ]; then
        echo -e "${RED}ERROR: Nextcloud failed to become healthy within ${timeout}s${NC}"
        echo "Nextcloud logs:"
        docker compose logs app | tail -50
        exit 1
    fi
    echo "Waiting for Nextcloud... ($elapsed/$timeout)"
    sleep 5
    elapsed=$((elapsed + 5))
done
echo -e "${GREEN}✓ Nextcloud is healthy${NC}"

echo -e "\n${YELLOW}Step 4: Checking if astroglobe app is enabled...${NC}"
if docker compose exec -T app php /var/www/html/occ app:list | grep -A 1 "Enabled:" | grep -q "astroglobe"; then
    echo -e "${GREEN}✓ astroglobe app is enabled${NC}"
else
    echo -e "${RED}✗ astroglobe app is NOT enabled${NC}"
    echo "Available apps:"
    docker compose exec -T app php /var/www/html/occ app:list
    exit 1
fi

echo -e "\n${YELLOW}Step 5: Checking app info...${NC}"
docker compose exec -T app php /var/www/html/occ app:list | grep -A 5 astroglobe || true

echo -e "\n${YELLOW}Step 6: Verifying MCP server URL configuration...${NC}"
mcp_url=$(docker compose exec -T app php /var/www/html/occ config:system:get mcp_server_url || echo "NOT_SET")
if [ "$mcp_url" = "http://mcp-oauth:8001" ]; then
    echo -e "${GREEN}✓ MCP server URL is configured correctly: $mcp_url${NC}"
else
    echo -e "${RED}✗ MCP server URL is incorrect: $mcp_url${NC}"
    echo "Expected: http://mcp-oauth:8001"
fi

echo -e "\n${YELLOW}Step 7: Checking if symlink was created...${NC}"
if docker compose exec -T app test -L /var/www/html/custom_apps/astroglobe; then
    echo -e "${GREEN}✓ Symlink exists at /var/www/html/custom_apps/astroglobe${NC}"
    docker compose exec -T app ls -la /var/www/html/custom_apps/astroglobe
else
    echo -e "${RED}✗ Symlink does not exist${NC}"
fi

echo -e "\n${YELLOW}Step 8: Checking app structure...${NC}"
docker compose exec -T app test -f /opt/apps/astroglobe/appinfo/info.xml && echo -e "${GREEN}✓ info.xml exists${NC}" || echo -e "${RED}✗ info.xml missing${NC}"
docker compose exec -T app test -f /opt/apps/astroglobe/lib/Controller/OAuthController.php && echo -e "${GREEN}✓ OAuthController.php exists${NC}" || echo -e "${RED}✗ OAuthController.php missing${NC}"
docker compose exec -T app test -f /opt/apps/astroglobe/lib/Service/McpTokenStorage.php && echo -e "${GREEN}✓ McpTokenStorage.php exists${NC}" || echo -e "${RED}✗ McpTokenStorage.php missing${NC}"
docker compose exec -T app test -f /opt/apps/astroglobe/appinfo/routes.php && echo -e "${GREEN}✓ routes.php exists${NC}" || echo -e "${RED}✗ routes.php missing${NC}"

echo -e "\n${YELLOW}Step 9: Checking if admin can access settings...${NC}"
# Try to access the admin settings page (this will check if the app loads without errors)
if docker compose exec -T app curl -s -u admin:admin http://localhost/index.php/settings/admin/mcp >/dev/null 2>&1; then
    echo -e "${GREEN}✓ Admin settings page is accessible${NC}"
else
    echo -e "${YELLOW}⚠ Admin settings page returned an error (may be expected if not fully configured)${NC}"
fi

echo -e "\n${YELLOW}Step 10: Checking Nextcloud logs for errors...${NC}"
error_count=$(docker compose exec -T app grep -c "astroglobe" /var/www/html/data/nextcloud.log 2>/dev/null || echo "0")
if [ "$error_count" -gt 0 ]; then
    echo -e "${YELLOW}⚠ Found $error_count log entries mentioning astroglobe${NC}"
    docker compose exec -T app grep "astroglobe" /var/www/html/data/nextcloud.log | tail -10 || true
else
    echo -e "${GREEN}✓ No errors in Nextcloud logs${NC}"
fi

echo -e "\n${GREEN}====================================="
echo "Testing Complete!"
echo "=====================================${NC}"
echo ""
echo "Next steps:"
echo "1. Open http://localhost:8080 in your browser"
echo "2. Login with admin/admin"
echo "3. Go to Settings → Personal → MCP Server"
echo "4. You should see the OAuth authorization UI"
echo ""
echo "To view logs:"
echo "  docker compose logs -f app"
echo ""
echo "To access occ commands:"
echo "  docker compose exec app php /var/www/html/occ app:list"
