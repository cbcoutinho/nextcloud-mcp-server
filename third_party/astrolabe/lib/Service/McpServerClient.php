<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Service;

use OCP\Http\Client\IClient;
use OCP\Http\Client\IClientService;
use OCP\IConfig;
use Psr\Log\LoggerInterface;

/**
 * HTTP client for communicating with the MCP server's management API.
 *
 * This service wraps the MCP server's REST API endpoints defined in ADR-018.
 * It handles authentication via OAuth bearer tokens and provides typed methods
 * for all management operations.
 */
class McpServerClient {
	private IClient $httpClient;
	private IConfig $config;
	private LoggerInterface $logger;
	private string $baseUrl;

	public function __construct(
		IClientService $clientService,
		IConfig $config,
		LoggerInterface $logger,
	) {
		$this->httpClient = $clientService->newClient();
		$this->config = $config;
		$this->logger = $logger;

		// Get MCP server configuration from Nextcloud config
		$baseUrl = $this->config->getSystemValue('mcp_server_url', 'http://localhost:8000');
		$this->baseUrl = is_string($baseUrl) ? $baseUrl : 'http://localhost:8000';
	}

	/**
	 * Get server status (version, auth mode, features).
	 *
	 * Public endpoint - no authentication required.
	 *
	 * @return array{
	 *   version?: string,
	 *   auth_mode?: string,
	 *   vector_sync_enabled?: bool,
	 *   uptime_seconds?: int,
	 *   management_api_version?: string,
	 *   error?: string
	 * }
	 */
	public function getStatus(): array {
		try {
			$response = $this->httpClient->get($this->baseUrl . '/api/v1/status');
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to get MCP server status', [
				'error' => $e->getMessage(),
				'server_url' => $this->baseUrl,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Get user session details.
	 *
	 * Requires authentication via OAuth bearer token.
	 *
	 * @param string $userId The user ID to query
	 * @param string $token OAuth bearer token
	 * @return array{
	 *   session_id?: string,
	 *   background_access_granted?: bool,
	 *   background_access_details?: array,
	 *   idp_profile?: array,
	 *   error?: string
	 * }
	 */
	public function getUserSession(string $userId, string $token): array {
		try {
			$response = $this->httpClient->get(
				$this->baseUrl . '/api/v1/users/' . urlencode($userId) . '/session',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error("Failed to get session for user $userId", [
				'error' => $e->getMessage(),
				'user_id' => $userId,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Revoke user's background access (delete refresh token).
	 *
	 * Requires authentication via OAuth bearer token.
	 *
	 * @param string $userId The user ID whose access to revoke
	 * @param string $token OAuth bearer token
	 * @return array{success?: bool, message?: string, error?: string}
	 */
	public function revokeUserAccess(string $userId, string $token): array {
		try {
			$response = $this->httpClient->post(
				$this->baseUrl . '/api/v1/users/' . urlencode($userId) . '/revoke',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error("Failed to revoke access for user $userId", [
				'error' => $e->getMessage(),
				'user_id' => $userId,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Get vector sync status (indexing metrics).
	 *
	 * Public endpoint - no authentication required.
	 * Only available if VECTOR_SYNC_ENABLED=true on server.
	 *
	 * @return array{
	 *   status?: string,
	 *   indexed_documents?: int,
	 *   pending_documents?: int,
	 *   last_sync_time?: string,
	 *   documents_per_second?: float,
	 *   errors_24h?: int,
	 *   error?: string
	 * }
	 */
	public function getVectorSyncStatus(): array {
		try {
			$response = $this->httpClient->get($this->baseUrl . '/api/v1/vector-sync/status');
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to get vector sync status', [
				'error' => $e->getMessage(),
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Execute semantic search for vector visualization.
	 *
	 * Requires OAuth bearer token for user-filtered search.
	 * Only available if VECTOR_SYNC_ENABLED=true on server.
	 *
	 * @param string $query Search query string
	 * @param string $algorithm Search algorithm: "semantic", "bm25", or "hybrid"
	 * @param int $limit Number of results (max 50)
	 * @param bool $includePca Whether to include PCA coordinates for 2D plot
	 * @param array|null $docTypes Document types to filter (e.g., ['note', 'file'])
	 * @param string|null $token OAuth bearer token for authentication
	 * @return array{
	 *   results?: array,
	 *   pca_coordinates?: array,
	 *   algorithm_used?: string,
	 *   total_documents?: int,
	 *   error?: string
	 * }
	 */
	public function search(
		string $query,
		string $algorithm = 'hybrid',
		int $limit = 10,
		bool $includePca = true,
		?array $docTypes = null,
		?string $token = null,
	): array {
		try {
			$requestBody = [
				'query' => $query,
				'algorithm' => $algorithm,
				'limit' => min($limit, 50), // Enforce max limit
				'include_pca' => $includePca,
			];

			// Add doc_types filter if specified
			if ($docTypes !== null && count($docTypes) > 0) {
				$requestBody['doc_types'] = $docTypes;
			}

			$options = ['json' => $requestBody];

			// Add authorization header if token provided
			if ($token !== null) {
				$options['headers'] = [
					'Authorization' => 'Bearer ' . $token
				];
			}

			$response = $this->httpClient->post(
				$this->baseUrl . '/api/v1/vector-viz/search',
				$options
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to execute search', [
				'error' => $e->getMessage(),
				'query' => $query,
				'algorithm' => $algorithm,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Execute semantic search for Nextcloud Unified Search.
	 *
	 * Simplified search method specifically for the unified search provider.
	 * Uses OAuth bearer token for authentication and user-scoped filtering.
	 *
	 * @param string $query Search query string
	 * @param string $token OAuth bearer token for authentication
	 * @param int $limit Maximum number of results (default: 20)
	 * @param int $offset Pagination offset (default: 0)
	 * @param string $algorithm Search algorithm: hybrid, semantic, or bm25 (default: hybrid)
	 * @param string $fusion Fusion method for hybrid: rrf or dbsf (default: rrf)
	 * @param float $scoreThreshold Minimum score threshold 0-1 (default: 0)
	 * @return array{
	 *   results?: array<array{
	 *     id?: string|int,
	 *     title?: string,
	 *     doc_type?: string,
	 *     excerpt?: string,
	 *     score?: float,
	 *     path?: string,
	 *     board_id?: int,
	 *     card_id?: int
	 *   }>,
	 *   total_found?: int,
	 *   algorithm_used?: string,
	 *   error?: string
	 * }
	 */
	public function searchForUnifiedSearch(
		string $query,
		string $token,
		int $limit = 20,
		int $offset = 0,
		string $algorithm = 'hybrid',
		string $fusion = 'rrf',
		float $scoreThreshold = 0.0,
	): array {
		try {
			$response = $this->httpClient->post(
				$this->baseUrl . '/api/v1/search',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token,
						'Content-Type' => 'application/json',
					],
					'json' => [
						'query' => $query,
						'algorithm' => $algorithm,
						'fusion' => $fusion,
						'score_threshold' => $scoreThreshold,
						'limit' => min($limit, 100),
						'offset' => $offset,
						'include_pca' => false,
						'include_chunks' => true,
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Unified search failed', [
				'error' => $e->getMessage(),
				'query' => $query,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Check if the MCP server is reachable and API key is valid.
	 *
	 * @return bool True if server is reachable and healthy
	 */
	public function isServerReachable(): bool {
		$status = $this->getStatus();
		return !isset($status['error']);
	}

	/**
	 * Get the configured MCP server internal URL (for API calls).
	 *
	 * @return string The internal base URL
	 */
	public function getServerUrl(): string {
		return $this->baseUrl;
	}

	/**
	 * Get the public MCP server URL (for display, OAuth audience).
	 *
	 * Falls back to internal URL if public URL not configured.
	 *
	 * @return string The public URL users/browsers see
	 */
	public function getPublicServerUrl(): string {
		return $this->config->getSystemValue('mcp_server_public_url', $this->baseUrl);
	}

	/**
	 * Get the OAuth client ID from system config.
	 *
	 * The Astrolabe app has its own OAuth client (separate from MCP server's client).
	 * Client ID must be configured in config.php for OAuth functionality to work.
	 *
	 * @return string OAuth client ID or empty string if not configured
	 */
	public function getClientId(): string {
		$clientId = $this->config->getSystemValue('astrolabe_client_id', '');

		if (empty($clientId)) {
			$this->logger->warning('astrolabe_client_id is not configured in config.php - OAuth functionality will not work');
			return '';
		}

		$this->logger->debug('Using client ID from system config: ' . substr($clientId, 0, 8) . '...');
		return $clientId;
	}

	/**
	 * List all registered webhooks for a user.
	 *
	 * Requires OAuth bearer token for authentication.
	 *
	 * @param string $token OAuth bearer token
	 * @return array{
	 *   webhooks?: array<array{
	 *     id?: int,
	 *     event?: string,
	 *     uri?: string,
	 *     event_filter?: array,
	 *     enabled?: bool
	 *   }>,
	 *   error?: string
	 * }
	 */
	public function listWebhooks(string $token): array {
		try {
			$response = $this->httpClient->get(
				$this->baseUrl . '/api/v1/webhooks',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to list webhooks', [
				'error' => $e->getMessage(),
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Create a new webhook registration.
	 *
	 * Requires OAuth bearer token for authentication.
	 *
	 * @param string $event Event type (e.g., "\\OCA\\Files::postCreate")
	 * @param string $uri Callback URI for webhook notifications
	 * @param array|null $eventFilter Optional event filter parameters
	 * @param string $token OAuth bearer token
	 * @return array{
	 *   id?: int,
	 *   event?: string,
	 *   uri?: string,
	 *   event_filter?: array,
	 *   enabled?: bool,
	 *   error?: string
	 * }
	 */
	public function createWebhook(
		string $event,
		string $uri,
		?array $eventFilter,
		string $token,
	): array {
		try {
			$requestBody = [
				'event' => $event,
				'uri' => $uri,
			];

			if ($eventFilter !== null) {
				$requestBody['event_filter'] = $eventFilter;
			}

			$response = $this->httpClient->post(
				$this->baseUrl . '/api/v1/webhooks',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token,
						'Content-Type' => 'application/json',
					],
					'json' => $requestBody
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to create webhook', [
				'error' => $e->getMessage(),
				'event' => $event,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Delete a webhook registration.
	 *
	 * Requires OAuth bearer token for authentication.
	 *
	 * @param int $webhookId Webhook ID to delete
	 * @param string $token OAuth bearer token
	 * @return array{success?: bool, error?: string}
	 */
	public function deleteWebhook(int $webhookId, string $token): array {
		try {
			$response = $this->httpClient->delete(
				$this->baseUrl . '/api/v1/webhooks/' . $webhookId,
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					]
				]
			);

			// Successful DELETE may return 204 No Content
			if ($response->getStatusCode() === 204) {
				return ['success' => true];
			}

			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to delete webhook', [
				'error' => $e->getMessage(),
				'webhook_id' => $webhookId,
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Get list of installed Nextcloud apps.
	 *
	 * Used to filter webhook presets based on available apps.
	 * Requires OAuth bearer token for authentication.
	 *
	 * @param string $token OAuth bearer token
	 * @return array{
	 *   apps?: array<string>,
	 *   error?: string
	 * }
	 */
	public function getInstalledApps(string $token): array {
		try {
			$response = $this->httpClient->get(
				$this->baseUrl . '/api/v1/apps',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to get installed apps', [
				'error' => $e->getMessage(),
			]);
			return ['error' => $e->getMessage()];
		}
	}

	/**
	 * Get chunk context (text, surrounding context, page image).
	 *
	 * Requires OAuth bearer token for authentication.
	 *
	 * @param string $docType Document type
	 * @param string $docId Document ID
	 * @param int $start Start offset
	 * @param int $end End offset
	 * @param string $token OAuth bearer token
	 * @return array
	 */
	public function getChunkContext(
		string $docType,
		string $docId,
		int $start,
		int $end,
		string $token,
	): array {
		try {
			$response = $this->httpClient->get(
				$this->baseUrl . '/api/v1/chunk-context',
				[
					'headers' => [
						'Authorization' => 'Bearer ' . $token
					],
					'query' => [
						'doc_type' => $docType,
						'doc_id' => $docId,
						'start' => $start,
						'end' => $end,
						'context' => 500
					]
				]
			);
			$data = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON response from server');
			}

			return $data;
		} catch (\Exception $e) {
			$this->logger->error('Failed to get chunk context', [
				'error' => $e->getMessage(),
				'doc_type' => $docType,
				'doc_id' => $docId,
			]);
			return ['error' => $e->getMessage()];
		}
	}
}
