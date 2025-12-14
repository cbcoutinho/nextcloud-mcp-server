<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Service;

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
	private $httpClient;
	private $config;
	private $logger;
	private $baseUrl;

	public function __construct(
		IClientService $clientService,
		IConfig $config,
		LoggerInterface $logger
	) {
		$this->httpClient = $clientService->newClient();
		$this->config = $config;
		$this->logger = $logger;

		// Get MCP server configuration from Nextcloud config
		$this->baseUrl = $this->config->getSystemValue('mcp_server_url', 'http://localhost:8000');
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
				$this->baseUrl . "/api/v1/users/" . urlencode($userId) . "/session",
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
				$this->baseUrl . "/api/v1/users/" . urlencode($userId) . "/revoke",
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
		?string $token = null
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
}
