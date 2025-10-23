#!/bin/bash

set -euox pipefail

php /var/www/html/occ config:system:set trusted_domains 2 --value=host.docker.internal
