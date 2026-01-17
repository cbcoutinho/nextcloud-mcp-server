<?php

declare(strict_types=1);

/**
 * Bootstrap for unit tests.
 *
 * Unit tests use mocked dependencies and don't require a full Nextcloud
 * environment. This bootstrap only loads the composer autoloader which
 * includes the OCP interface definitions needed for mocking.
 */

require_once __DIR__ . '/../../vendor/autoload.php';
