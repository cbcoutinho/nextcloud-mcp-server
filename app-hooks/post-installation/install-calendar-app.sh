#!/bin/bash

set -euox pipefail

echo "Installing and configuring Calendar app..."

# Enable calendar app
php /var/www/html/occ app:enable calendar

# Wait for calendar app to be fully initialized
echo "Waiting for calendar app to initialize..."
sleep 5

# Increase limits on calendar creation for integration tests (100 in 60s)
php occ config:app:set dav rateLimitCalendarCreation --type=integer --value=100
php occ config:app:set dav rateLimitPeriodCalendarCreation --type=integer --value=60

# Ensure maintenance mode is off before calendar operations
php /var/www/html/occ maintenance:mode --off

# Sync DAV system to ensure proper initialization
echo "Syncing DAV system..."
php /var/www/html/occ dav:sync-system-addressbook

# Repair calendar app to ensure proper setup
echo "Repairing calendar app..."
php /var/www/html/occ maintenance:repair --include-expensive

# Final wait to ensure CalDAV service is fully ready
echo "Final CalDAV initialization wait..."
sleep 5

echo "Calendar app installation complete!"
