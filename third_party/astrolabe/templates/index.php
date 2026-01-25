<?php

declare(strict_types=1);

use OCA\Astrolabe\AppInfo\Application;
use OCP\Util;

// Load PDF.js loader first (must be external, not bundled by Vite,
// to avoid ES private field transformation issues with fake worker fallback)
// The loader imports pdf.mjs and sets window.pdfjsLib before the main app runs
Util::addScript(Application::APP_ID, 'pdfjs-loader');
Util::addScript(Application::APP_ID, Application::APP_ID . '-main');
Util::addStyle(Application::APP_ID, Application::APP_ID . '-main');

?>

<div id="astrolabe"></div>
