<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Controller;

use OCA\Astrolabe\Service\IdpTokenRefresher;
use OCA\Astrolabe\Service\McpServerClient;
use OCA\Astrolabe\Service\McpTokenStorage;
use OCA\Astrolabe\Service\WebhookPresets;
use OCA\Astrolabe\Settings\Admin as AdminSettings;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\AppFramework\Http\RedirectResponse;
use OCP\IConfig;
use OCP\IRequest;
use OCP\IURLGenerator;
use OCP\IUserSession;
use Psr\Log\LoggerInterface;

/**
 * API controller for MCP Server UI.
 *
 * Handles form submissions and AJAX requests from settings panels.
 */
class ApiController extends Controller {
	private $client;
	private $userSession;
	private $urlGenerator;
	private $logger;
	private $tokenStorage;
	private $config;
	private $tokenRefresher;

	public function __construct(
		string $appName,
		IRequest $request,
		McpServerClient $client,
		IUserSession $userSession,
		IURLGenerator $urlGenerator,
		LoggerInterface $logger,
		McpTokenStorage $tokenStorage,
		IConfig $config,
		IdpTokenRefresher $tokenRefresher,
	) {
		parent::__construct($appName, $request);
		$this->client = $client;
		$this->userSession = $userSession;
		$this->urlGenerator = $urlGenerator;
		$this->logger = $logger;
		$this->tokenStorage = $tokenStorage;
		$this->config = $config;
		$this->tokenRefresher = $tokenRefresher;
	}

	/**
	 * Revoke user's background access (delete refresh token).
	 *
	 * Called from personal settings form POST.
	 * Redirects back to personal settings after completion.
	 *
	 * @return RedirectResponse
	 */
	#[NoAdminRequired]
	public function revokeAccess(): RedirectResponse {
		$user = $this->userSession->getUser();
		if (!$user) {
			// Should not happen (NoAdminRequired ensures user is logged in)
			$this->logger->error('Revoke access called without authenticated user');
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astrolabe'])
			);
		}

		$userId = $user->getUID();

		// Get user's OAuth token
		$token = $this->tokenStorage->getUserToken($userId);
		if (!$token) {
			$this->logger->error("Cannot revoke access: No token found for user $userId");
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astrolabe'])
			);
		}

		$accessToken = $token['access_token'];

		// Call MCP server API to revoke access
		$result = $this->client->revokeUserAccess($userId, $accessToken);

		if (isset($result['error'])) {
			$this->logger->error("Failed to revoke access for user $userId", [
				'error' => $result['error']
			]);
			// TODO: Add flash message/notification for user feedback
		} else {
			$this->logger->info("Successfully revoked background access for user $userId");

			// Delete local OAuth tokens from Nextcloud config
			// This ensures hasBackgroundAccess() returns false on next page load
			$this->tokenStorage->deleteUserToken($userId);
			$this->logger->debug("Deleted local OAuth tokens for user $userId");

			// TODO: Add success flash message/notification
		}

		// Redirect back to personal settings
		return new RedirectResponse(
			$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astrolabe'])
		);
	}

	/**
	 * Execute semantic search via MCP server.
	 *
	 * AJAX endpoint for vector search UI in app page.
	 * Uses user's OAuth token for authentication.
	 *
	 * @param string $query Search query
	 * @param string $algorithm Search algorithm (semantic, bm25, hybrid)
	 * @param int $limit Number of results (max 50)
	 * @param string $doc_types Comma-separated document types (e.g., "note,file")
	 * @param string $include_pca Whether to include PCA coordinates for visualization
	 * @return JSONResponse
	 */
	#[NoAdminRequired]
	public function search(
		string $query = '',
		string $algorithm = 'hybrid',
		int $limit = 10,
		string $doc_types = '',
		string $include_pca = 'true',
	): JSONResponse {
		if (empty($query)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Missing required parameter: query'
			], Http::STATUS_BAD_REQUEST);
		}

		// Get current user
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse([
				'success' => false,
				'error' => 'User not authenticated'
			], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Create refresh callback that calls IdP directly
		$refreshCallback = function (string $refreshToken) {
			$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

			if (!$newTokenData) {
				return null;
			}

			return [
				'access_token' => $newTokenData['access_token'],
				'refresh_token' => $newTokenData['refresh_token'] ?? $refreshToken,
				'expires_in' => $newTokenData['expires_in'] ?? 3600,
			];
		};

		// Get user's OAuth token for MCP server with automatic refresh
		$accessToken = $this->tokenStorage->getAccessToken($userId, $refreshCallback);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required. Please authorize the app first.'
			], Http::STATUS_UNAUTHORIZED);
		}

		// Validate algorithm
		$validAlgorithms = ['semantic', 'bm25', 'hybrid'];
		if (!in_array($algorithm, $validAlgorithms)) {
			$algorithm = 'hybrid';
		}

		// Enforce limit bounds
		$limit = max(1, min($limit, 50));

		// Parse doc_types filter
		$docTypesArray = null;
		if (!empty($doc_types)) {
			$validDocTypes = ['note', 'file', 'deck_card', 'calendar', 'contact', 'news_item'];
			$docTypesArray = array_filter(
				explode(',', $doc_types),
				fn ($t) => in_array(trim($t), $validDocTypes)
			);
			$docTypesArray = array_map('trim', $docTypesArray);
			if (empty($docTypesArray)) {
				$docTypesArray = null;
			}
		}

		// Parse include_pca (string "true"/"false" from query params)
		$includePcaBool = in_array(strtolower($include_pca), ['true', '1', 'yes'], true);

		// Execute search via MCP server with OAuth token
		$result = $this->client->search($query, $algorithm, $limit, $includePcaBool, $docTypesArray, $accessToken);

		if (isset($result['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $result['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$response = [
			'success' => true,
			'results' => $result['results'] ?? [],
			'algorithm_used' => $result['algorithm_used'] ?? $algorithm,
			'total_documents' => $result['total_documents'] ?? 0,
		];

		// Include PCA visualization coordinates if requested and available
		if ($includePcaBool) {
			$response['coordinates_3d'] = $result['coordinates_3d'] ?? [];
			$response['query_coords'] = $result['query_coords'] ?? [];
			if (isset($result['pca_variance'])) {
				$response['pca_variance'] = $result['pca_variance'];
			}
		}

		return new JSONResponse($response);
	}

	/**
	 * Get vector sync status from MCP server.
	 *
	 * AJAX endpoint for status refresh in personal settings.
	 *
	 * @return JSONResponse
	 */
	#[NoAdminRequired]
	public function vectorStatus(): JSONResponse {
		$status = $this->client->getVectorSyncStatus();

		if (isset($status['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $status['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		return new JSONResponse([
			'success' => true,
			'status' => $status
		]);
	}

	/**
	 * Get MCP server status.
	 *
	 * Admin-only endpoint for admin settings page.
	 * Returns server version, uptime, and vector sync availability.
	 *
	 * @return JSONResponse
	 */
	public function serverStatus(): JSONResponse {
		$status = $this->client->getStatus();

		// Validate that status is an array before accessing
		if (!is_array($status)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Invalid response from MCP server'
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		if (isset($status['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $status['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		return new JSONResponse([
			'success' => true,
			'status' => $status
		]);
	}

	/**
	 * Get vector sync status for admin.
	 *
	 * Admin-only endpoint for admin settings page.
	 * Returns indexing metrics and sync status.
	 *
	 * @return JSONResponse
	 */
	public function adminVectorStatus(): JSONResponse {
		$status = $this->client->getVectorSyncStatus();

		// Validate that status is an array before accessing
		if (!is_array($status)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Invalid response from MCP server'
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		if (isset($status['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $status['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		return new JSONResponse([
			'success' => true,
			'status' => $status
		]);
	}

	/**
	 * Save admin search settings.
	 *
	 * Admin-only endpoint to configure AI Search provider parameters.
	 *
	 * @return JSONResponse
	 */
	public function saveSearchSettings(): JSONResponse {
		// Parse JSON body
		$input = file_get_contents('php://input');
		$data = json_decode($input, true);

		if ($data === null) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Invalid JSON body'
			], Http::STATUS_BAD_REQUEST);
		}

		// Validate and save algorithm
		$validAlgorithms = ['hybrid', 'semantic', 'bm25'];
		$algorithm = $data['algorithm'] ?? AdminSettings::DEFAULT_SEARCH_ALGORITHM;
		if (!in_array($algorithm, $validAlgorithms)) {
			$algorithm = AdminSettings::DEFAULT_SEARCH_ALGORITHM;
		}
		$this->config->setAppValue(
			$this->appName,
			AdminSettings::SETTING_SEARCH_ALGORITHM,
			$algorithm
		);

		// Validate and save fusion method
		$validFusions = ['rrf', 'dbsf'];
		$fusion = $data['fusion'] ?? AdminSettings::DEFAULT_SEARCH_FUSION;
		if (!in_array($fusion, $validFusions)) {
			$fusion = AdminSettings::DEFAULT_SEARCH_FUSION;
		}
		$this->config->setAppValue(
			$this->appName,
			AdminSettings::SETTING_SEARCH_FUSION,
			$fusion
		);

		// Validate and save score threshold (0-100)
		$scoreThreshold = (int)($data['scoreThreshold'] ?? AdminSettings::DEFAULT_SEARCH_SCORE_THRESHOLD);
		$scoreThreshold = max(0, min(100, $scoreThreshold));
		$this->config->setAppValue(
			$this->appName,
			AdminSettings::SETTING_SEARCH_SCORE_THRESHOLD,
			(string)$scoreThreshold
		);

		// Validate and save limit (5-100)
		$limit = (int)($data['limit'] ?? AdminSettings::DEFAULT_SEARCH_LIMIT);
		$limit = max(5, min(100, $limit));
		$this->config->setAppValue(
			$this->appName,
			AdminSettings::SETTING_SEARCH_LIMIT,
			(string)$limit
		);

		$this->logger->info('Admin search settings saved', [
			'algorithm' => $algorithm,
			'fusion' => $fusion,
			'scoreThreshold' => $scoreThreshold,
			'limit' => $limit,
		]);

		return new JSONResponse([
			'success' => true,
			'settings' => [
				'algorithm' => $algorithm,
				'fusion' => $fusion,
				'scoreThreshold' => $scoreThreshold,
				'limit' => $limit,
			]
		]);
	}

	/**
	 * Get available webhook presets.
	 *
	 * Admin-only endpoint that lists webhook presets filtered by installed apps.
	 *
	 * @return JSONResponse
	 */
	public function getWebhookPresets(): JSONResponse {
		// Get admin's OAuth token for API calls
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse([
				'success' => false,
				'error' => 'User not authenticated'
			], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Create refresh callback
		$refreshCallback = function (string $refreshToken) {
			$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

			if (!$newTokenData) {
				return null;
			}

			return [
				'access_token' => $newTokenData['access_token'],
				'refresh_token' => $newTokenData['refresh_token'] ?? $refreshToken,
				'expires_in' => $newTokenData['expires_in'] ?? 3600,
			];
		};

		// Get access token with automatic refresh
		$accessToken = $this->tokenStorage->getAccessToken($userId, $refreshCallback);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required'
			], Http::STATUS_UNAUTHORIZED);
		}

		// Get installed apps to filter presets
		$installedAppsResult = $this->client->getInstalledApps($accessToken);
		if (isset($installedAppsResult['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $installedAppsResult['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$installedApps = $installedAppsResult['apps'] ?? [];

		// Get registered webhooks to check preset status
		$webhooksResult = $this->client->listWebhooks($accessToken);
		if (isset($webhooksResult['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $webhooksResult['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$registeredWebhooks = $webhooksResult['webhooks'] ?? [];

		// Filter presets by installed apps
		$presets = WebhookPresets::filterPresetsByInstalledApps($installedApps);

		// Add enabled status to each preset
		// IMPORTANT: Match both event type AND filter to avoid false positives
		// (e.g., Notes and Files both use FILE_EVENT_* but with different filters)
		$presetsWithStatus = [];
		foreach ($presets as $presetId => $preset) {
			// Check if all events for this preset are registered with matching filters
			$allEventsRegistered = true;
			foreach ($preset['events'] as $presetEvent) {
				$eventMatched = false;
				foreach ($registeredWebhooks as $webhook) {
					// Match event type
					if ($webhook['event'] !== $presetEvent['event']) {
						continue;
					}

					// Match filter (both must have filter or both must not have filter)
					$presetFilter = !empty($presetEvent['filter']) ? $presetEvent['filter'] : null;
					$webhookFilter = !empty($webhook['eventFilter']) ? $webhook['eventFilter'] : null;

					// Compare filters (use json_encode for deep comparison)
					if (json_encode($presetFilter) === json_encode($webhookFilter)) {
						$eventMatched = true;
						break;
					}
				}

				if (!$eventMatched) {
					$allEventsRegistered = false;
					break;
				}
			}

			$presetsWithStatus[$presetId] = array_merge($preset, [
				'enabled' => $allEventsRegistered
			]);
		}

		return new JSONResponse([
			'success' => true,
			'presets' => $presetsWithStatus
		]);
	}

	/**
	 * Enable a webhook preset.
	 *
	 * Admin-only endpoint that registers all webhooks for a preset.
	 *
	 * @param string $presetId Preset ID to enable
	 * @return JSONResponse
	 */
	public function enableWebhookPreset(string $presetId): JSONResponse {
		// Get admin's OAuth token
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse([
				'success' => false,
				'error' => 'User not authenticated'
			], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Create refresh callback
		$refreshCallback = function (string $refreshToken) {
			$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

			if (!$newTokenData) {
				return null;
			}

			return [
				'access_token' => $newTokenData['access_token'],
				'refresh_token' => $newTokenData['refresh_token'] ?? $refreshToken,
				'expires_in' => $newTokenData['expires_in'] ?? 3600,
			];
		};

		// Get access token with automatic refresh
		$accessToken = $this->tokenStorage->getAccessToken($userId, $refreshCallback);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required'
			], Http::STATUS_UNAUTHORIZED);
		}

		// Get preset configuration
		$preset = WebhookPresets::getPreset($presetId);
		if ($preset === null) {
			return new JSONResponse([
				'success' => false,
				'error' => "Unknown preset: $presetId"
			], Http::STATUS_BAD_REQUEST);
		}

		// Get MCP server URL for webhook callback URI
		$mcpServerUrl = $this->client->getServerUrl();
		$callbackUri = $mcpServerUrl . '/api/v1/webhooks/callback';

		// Register each event in the preset
		$registered = [];
		$errors = [];
		foreach ($preset['events'] as $eventConfig) {
			$result = $this->client->createWebhook(
				$eventConfig['event'],
				$callbackUri,
				!empty($eventConfig['filter']) ? $eventConfig['filter'] : null,
				$accessToken
			);

			if (isset($result['error'])) {
				$errors[] = [
					'event' => $eventConfig['event'],
					'error' => $result['error']
				];
			} else {
				$registered[] = $result;
			}
		}

		if (!empty($errors)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Failed to register some webhooks',
				'registered' => $registered,
				'errors' => $errors
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$this->logger->info("Enabled webhook preset $presetId for user $userId", [
			'preset_id' => $presetId,
			'webhooks_registered' => count($registered)
		]);

		return new JSONResponse([
			'success' => true,
			'message' => "Enabled {$preset['name']}",
			'webhooks' => $registered
		]);
	}

	/**
	 * Disable a webhook preset.
	 *
	 * Admin-only endpoint that deletes all webhooks for a preset.
	 *
	 * @param string $presetId Preset ID to disable
	 * @return JSONResponse
	 */
	public function disableWebhookPreset(string $presetId): JSONResponse {
		// Get admin's OAuth token
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse([
				'success' => false,
				'error' => 'User not authenticated'
			], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Create refresh callback
		$refreshCallback = function (string $refreshToken) {
			$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

			if (!$newTokenData) {
				return null;
			}

			return [
				'access_token' => $newTokenData['access_token'],
				'refresh_token' => $newTokenData['refresh_token'] ?? $refreshToken,
				'expires_in' => $newTokenData['expires_in'] ?? 3600,
			];
		};

		// Get access token with automatic refresh
		$accessToken = $this->tokenStorage->getAccessToken($userId, $refreshCallback);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required'
			], Http::STATUS_UNAUTHORIZED);
		}

		// Get preset configuration
		$preset = WebhookPresets::getPreset($presetId);
		if ($preset === null) {
			return new JSONResponse([
				'success' => false,
				'error' => "Unknown preset: $presetId"
			], Http::STATUS_BAD_REQUEST);
		}

		// Get all registered webhooks
		$webhooksResult = $this->client->listWebhooks($accessToken);
		if (isset($webhooksResult['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $webhooksResult['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$registeredWebhooks = $webhooksResult['webhooks'] ?? [];

		// Find webhooks that match this preset's events AND filters
		// IMPORTANT: Must match both event type AND filter to avoid deleting
		// webhooks from other presets (e.g., Notes vs Files both use FILE_EVENT_*)
		$webhooksToDelete = [];
		foreach ($registeredWebhooks as $webhook) {
			// Check if this webhook matches any event in the preset
			foreach ($preset['events'] as $presetEvent) {
				// Match event type
				if ($webhook['event'] !== $presetEvent['event']) {
					continue;
				}

				// Match filter (both must have filter or both must not have filter)
				$presetFilter = !empty($presetEvent['filter']) ? $presetEvent['filter'] : null;
				$webhookFilter = !empty($webhook['eventFilter']) ? $webhook['eventFilter'] : null;

				// Compare filters (use json_encode for deep comparison)
				if (json_encode($presetFilter) === json_encode($webhookFilter)) {
					$webhooksToDelete[] = $webhook;
					break; // This webhook matches, no need to check other preset events
				}
			}
		}

		// Delete each matching webhook
		$deleted = [];
		$errors = [];
		foreach ($webhooksToDelete as $webhook) {
			$result = $this->client->deleteWebhook($webhook['id'], $accessToken);

			if (isset($result['error'])) {
				$errors[] = [
					'webhook_id' => $webhook['id'],
					'event' => $webhook['event'],
					'error' => $result['error']
				];
			} else {
				$deleted[] = $webhook['id'];
			}
		}

		if (!empty($errors)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Failed to delete some webhooks',
				'deleted' => $deleted,
				'errors' => $errors
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$this->logger->info("Disabled webhook preset $presetId for user $userId", [
			'preset_id' => $presetId,
			'webhooks_deleted' => count($deleted)
		]);

		return new JSONResponse([
			'success' => true,
			'message' => "Disabled {$preset['name']}",
			'deleted' => $deleted
		]);
	}

	/**
	 * Get chunk context for visualization.
	 *
	 * @param string $doc_type Document type
	 * @param string $doc_id Document ID
	 * @param int $start Start offset
	 * @param int $end End offset
	 * @return JSONResponse
	 */
	#[NoAdminRequired]
	public function chunkContext(
		string $doc_type,
		string $doc_id,
		int $start,
		int $end,
	): JSONResponse {
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse(['error' => 'User not authenticated'], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Create refresh callback
		$refreshCallback = function (string $refreshToken) {
			$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

			if (!$newTokenData) {
				return null;
			}

			return [
				'access_token' => $newTokenData['access_token'],
				'refresh_token' => $newTokenData['refresh_token'] ?? $refreshToken,
				'expires_in' => $newTokenData['expires_in'] ?? 3600,
			];
		};

		// Get user's OAuth token for MCP server with automatic refresh
		$accessToken = $this->tokenStorage->getAccessToken($userId, $refreshCallback);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required.'
			], Http::STATUS_UNAUTHORIZED);
		}

		$result = $this->client->getChunkContext($doc_type, $doc_id, $start, $end, $accessToken);

		if (isset($result['error'])) {
			return new JSONResponse(['success' => false, 'error' => $result['error']], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		return new JSONResponse($result);
	}
}
