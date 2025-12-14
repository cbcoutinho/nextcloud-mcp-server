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

		// Admin settings routes
		[
			'name' => 'api#saveSearchSettings',
			'url' => '/api/admin/search-settings',
			'verb' => 'POST',
		],
	],
];
