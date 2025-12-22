<?php

declare(strict_types=1);

use OCP\Util;

Util::addScript(OCA\Astrolabe\AppInfo\Application::APP_ID, OCA\Astrolabe\AppInfo\Application::APP_ID . '-main');
Util::addStyle(OCA\Astrolabe\AppInfo\Application::APP_ID, OCA\Astrolabe\AppInfo\Application::APP_ID . '-main');

?>

<div id="astrolabe"></div>
