<?php

declare(strict_types=1);

use OCA\Astrolabe\AppInfo\Application;
use OCP\Util;

Util::addScript(Application::APP_ID, Application::APP_ID . '-main');
Util::addStyle(Application::APP_ID, Application::APP_ID . '-main');

?>

<div id="astrolabe"></div>
