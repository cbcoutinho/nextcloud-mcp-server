#!/bin/bash

set -euox pipefail

echo "Installing and configuring mail app for testing..."

# The Mail account (pointing at the GreenMail service) is provisioned later by
# the test harness, once GreenMail is reachable — see
# scripts/provision-greenmail-account.sh / the mail integration fixtures. Here
# we only need the app installed and enabled.
if [ -d /var/www/html/custom_apps/mail ]; then
    echo "mail app directory found in apps (already installed)"
    php /var/www/html/occ app:enable mail
else
    echo "mail app not found, installing from app store..."
    php /var/www/html/occ app:install mail
    php /var/www/html/occ app:enable mail
fi
