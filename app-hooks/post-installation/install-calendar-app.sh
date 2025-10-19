#!/bin/bash

set -euox pipefail

echo "Installing and configuring Calendar app..."

# Enable calendar app
php /var/www/html/occ app:enable calendar
php /var/www/html/occ app:enable --force tasks # Not currently supported on 32

# Wait for calendar app to be fully initialized
echo "Waiting for calendar app to initialize..."
sleep 5

# Disable rate limits on calendar creation for integration tests
# Set to -1 to completely disable rate limiting
# Reference: https://docs.nextcloud.com/server/stable/admin_manual/groupware/calendar.html#rate-limits
php occ config:app:set dav rateLimitCalendarCreation --type=integer --value=-1
php occ config:app:set dav rateLimitPeriodCalendarCreation --type=integer --value=-1
php occ config:app:set dav maximumCalendarsSubscriptions --type=integer --value=-1

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
