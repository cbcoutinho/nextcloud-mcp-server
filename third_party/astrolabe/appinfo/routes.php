<?php

declare(strict_types=1);

/**
 * Routes configuration for MCP Server UI app.
 *
 * Defines URL routes for OAuth flow and form handlers.
 */

return [
	'routes' => [
		// OAuth routes
		[
			'name' => 'oauth#initiateOAuth',
			'url' => '/oauth/authorize',
			'verb' => 'GET',
		],
		[
			'name' => 'oauth#oauthCallback',
			'url' => '/oauth/callback',
			'verb' => 'GET',
		],
		[
			'name' => 'oauth#disconnect',
			'url' => '/oauth/disconnect',
			'verb' => 'POST',
		],

		// API routes (form handlers)
		[
			'name' => 'api#revokeAccess',
			'url' => '/api/revoke',
			'verb' => 'POST',
		],

		// Background sync credentials routes
		[
			'name' => 'credentials#storeAppPassword',
			'url' => '/api/v1/background-sync/credentials',
			'verb' => 'POST',
		],
		[
			'name' => 'credentials#getCredentials',
			'url' => '/api/v1/background-sync/credentials/{userId}',
			'verb' => 'GET',
		],
		[
			'name' => 'credentials#deleteCredentials',
			'url' => '/api/v1/background-sync/credentials',
			'verb' => 'DELETE',
		],
		[
			'name' => 'credentials#getStatus',
			'url' => '/api/v1/background-sync/status',
			'verb' => 'GET',
		],

		// Vector search API routes
		[
			'name' => 'api#search',
			'url' => '/api/search',
			'verb' => 'GET',
		],
		[
			'name' => 'api#vectorStatus',
			'url' => '/api/vector-status',
			'verb' => 'GET',
		],
		[
			'name' => 'api#chunkContext',
			'url' => '/api/chunk-context',
			'verb' => 'GET',
		],

		// Admin settings routes
		[
			'name' => 'api#saveSearchSettings',
			'url' => '/api/admin/search-settings',
			'verb' => 'POST',
		],

		// Webhook management routes (admin only)
		[
			'name' => 'api#getWebhookPresets',
			'url' => '/api/admin/webhooks/presets',
			'verb' => 'GET',
		],
		[
			'name' => 'api#enableWebhookPreset',
			'url' => '/api/admin/webhooks/presets/{presetId}/enable',
			'verb' => 'POST',
		],
		[
			'name' => 'api#disableWebhookPreset',
			'url' => '/api/admin/webhooks/presets/{presetId}/disable',
			'verb' => 'POST',
		],
	],
];
