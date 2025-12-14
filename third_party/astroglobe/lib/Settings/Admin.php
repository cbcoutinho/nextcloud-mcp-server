<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Settings;

use OCA\Astroglobe\AppInfo\Application;
use OCA\Astroglobe\Service\McpServerClient;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\AppFramework\Services\IInitialState;
use OCP\IConfig;
use OCP\Settings\ISettings;

/**
 * Admin settings panel for Astroglobe.
 *
 * Displays semantic search service status, indexing metrics,
 * configuration, and provides administrative controls.
 */
class Admin implements ISettings {
	// Search settings keys and defaults
	public const SETTING_SEARCH_ALGORITHM = 'search_algorithm';
	public const SETTING_SEARCH_FUSION = 'search_fusion';
	public const SETTING_SEARCH_SCORE_THRESHOLD = 'search_score_threshold';
	public const SETTING_SEARCH_LIMIT = 'search_limit';

	public const DEFAULT_SEARCH_ALGORITHM = 'hybrid';
	public const DEFAULT_SEARCH_FUSION = 'rrf';
	public const DEFAULT_SEARCH_SCORE_THRESHOLD = 0;
	public const DEFAULT_SEARCH_LIMIT = 20;

	private $client;
	private $config;
	private $initialState;

	public function __construct(
		McpServerClient $client,
		IConfig $config,
		IInitialState $initialState
	) {
		$this->client = $client;
		$this->config = $config;
		$this->initialState = $initialState;
	}

	/**
	 * @return TemplateResponse
	 */
	public function getForm(): TemplateResponse {
		// Fetch data from MCP server
		$serverStatus = $this->client->getStatus();
		$vectorSyncStatus = $this->client->getVectorSyncStatus();

		// Get configuration from config.php
		$serverUrl = $this->config->getSystemValue('mcp_server_url', '');
		$apiKeyConfigured = !empty($this->config->getSystemValue('mcp_server_api_key', ''));

		// Check for server connection error
		if (isset($serverStatus['error'])) {
			return new TemplateResponse(
				Application::APP_ID,
				'settings/error',
				[
					'error' => 'Cannot connect to MCP server',
					'details' => $serverStatus['error'],
					'server_url' => $serverUrl,
					'help_text' => 'Ensure MCP server is running and accessible. Check config.php for correct mcp_server_url.',
				],
				TemplateResponse::RENDER_AS_BLANK
			);
		}

		// Load search settings from app config
		$searchSettings = [
			'algorithm' => $this->config->getAppValue(
				Application::APP_ID,
				self::SETTING_SEARCH_ALGORITHM,
				self::DEFAULT_SEARCH_ALGORITHM
			),
			'fusion' => $this->config->getAppValue(
				Application::APP_ID,
				self::SETTING_SEARCH_FUSION,
				self::DEFAULT_SEARCH_FUSION
			),
			'scoreThreshold' => (int)$this->config->getAppValue(
				Application::APP_ID,
				self::SETTING_SEARCH_SCORE_THRESHOLD,
				(string)self::DEFAULT_SEARCH_SCORE_THRESHOLD
			),
			'limit' => (int)$this->config->getAppValue(
				Application::APP_ID,
				self::SETTING_SEARCH_LIMIT,
				(string)self::DEFAULT_SEARCH_LIMIT
			),
		];

		// Provide initial state for Vue.js frontend (if needed)
		$this->initialState->provideInitialState('server-data', [
			'serverStatus' => $serverStatus,
			'vectorSyncStatus' => $vectorSyncStatus,
			'config' => [
				'serverUrl' => $serverUrl,
				'apiKeyConfigured' => $apiKeyConfigured,
			],
			'searchSettings' => $searchSettings,
		]);

		$parameters = [
			'serverStatus' => $serverStatus,
			'vectorSyncStatus' => $vectorSyncStatus,
			'serverUrl' => $serverUrl,
			'apiKeyConfigured' => $apiKeyConfigured,
			'vectorSyncEnabled' => $serverStatus['vector_sync_enabled'] ?? false,
			'searchSettings' => $searchSettings,
		];

		return new TemplateResponse(
			Application::APP_ID,
			'settings/admin',
			$parameters,
			TemplateResponse::RENDER_AS_BLANK
		);
	}

	/**
	 * @return string The section ID
	 */
	public function getSection(): string {
		return 'astroglobe';
	}

	/**
	 * @return int Priority (lower = higher up)
	 */
	public function getPriority(): int {
		return 10;
	}
}
