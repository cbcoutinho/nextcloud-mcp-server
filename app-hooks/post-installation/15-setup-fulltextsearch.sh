#!/bin/bash
#
# Install and configure the Nextcloud Full Text Search apps against the
# `elasticsearch` service.
#
# Runs only when the `fulltextsearch` docker-compose profile is active (the
# `elasticsearch` hostname resolves), so the hook is a no-op for every other lane.
#

set -e

if ! getent hosts elasticsearch >/dev/null 2>&1; then
    echo "elasticsearch not on the network, skipping Full Text Search setup."
    exit 0
fi

php /var/www/html/occ app:install fulltextsearch
php /var/www/html/occ app:install fulltextsearch_elasticsearch
php /var/www/html/occ app:install files_fulltextsearch

php /var/www/html/occ config:app:set fulltextsearch search_platform \
    --value='OCA\FullTextSearch_Elasticsearch\Platform\ElasticSearchPlatform'
php /var/www/html/occ config:app:set fulltextsearch_elasticsearch elastic_host \
    --value=http://elasticsearch:9200
php /var/www/html/occ config:app:set fulltextsearch_elasticsearch elastic_index \
    --value=nextcloud
php /var/www/html/occ config:app:set fulltextsearch_elasticsearch analyzer_tokenizer \
    --value=standard
php /var/www/html/occ config:app:set files_fulltextsearch files_local --value=1
