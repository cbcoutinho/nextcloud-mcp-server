<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Service;

use OCP\Http\Client\IClientService;
use OCP\IConfig;
use Psr\Log\LoggerInterface;

/**
 * Refreshes OAuth tokens directly with the Identity Provider.
 *
 * Works with both Nextcloud OIDC and external IdPs like Keycloak.
 * Uses OIDC discovery to find the token endpoint automatically.
 *
 * This service is only used for confidential clients (with client_secret).
 * Public clients without client_secret cannot refresh tokens.
 */
class IdpTokenRefresher {
	private $config;
	private $httpClient;
	private $logger;
	private $mcpServerClient;

	public function __construct(
		IConfig $config,
		IClientService $clientService,
		LoggerInterface $logger,
		McpServerClient $mcpServerClient,
	) {
		$this->config = $config;
		$this->httpClient = $clientService->newClient();
		$this->logger = $logger;
		$this->mcpServerClient = $mcpServerClient;
	}

	/**
	 * Get Nextcloud base URL for constructing internal OIDC endpoint URLs.
	 *
	 * IMPORTANT: This is for INTERNAL server-to-server requests (PHP to local Apache),
	 * NOT for external client URLs. We must use the internal container URL, not the
	 * external URL that browsers see.
	 *
	 * Configuration priority:
	 * 1. astrolabe_internal_url - Explicit internal URL (for custom container setups)
	 * 2. http://localhost - Default for Docker containers (web server on port 80)
	 *
	 * NOTE: We intentionally DO NOT use overwrite.cli.url here because:
	 * - overwrite.cli.url is the EXTERNAL URL (e.g., http://localhost:8080)
	 * - External URLs are not accessible from inside the container
	 * - This method is for internal HTTP requests to the local web server
	 *
	 * @return string Base URL for internal requests (e.g., "http://localhost")
	 */
	private function getNextcloudBaseUrl(): string {
		// Check for explicit internal URL config (for custom container setups)
		$internalUrl = $this->config->getSystemValue('astrolabe_internal_url', '');
		if (!empty($internalUrl)) {
			// Validate URL format
			if (!filter_var($internalUrl, FILTER_VALIDATE_URL)) {
				$this->logger->warning('Invalid astrolabe_internal_url format, using default', [
					'configured_url' => $internalUrl,
				]);
				return 'http://localhost';
			}
			// Warn if it looks like an external URL (common misconfiguration)
			if (preg_match('/:\d{4,5}$/', $internalUrl)) {
				$this->logger->warning('astrolabe_internal_url appears to use external port mapping', [
					'configured_url' => $internalUrl,
					'hint' => 'Internal URLs should use port 80, not mapped ports like :8080',
				]);
			}
			return rtrim($internalUrl, '/');
		}

		// Default: container environment with web server on localhost:80
		// This works because PHP runs inside the same container as Apache
		return 'http://localhost';
	}

	/**
	 * Refresh access token using refresh token.
	 *
	 * Calls IdP's token endpoint directly (NOT MCP server).
	 *
	 * @param string $refreshToken The refresh token
	 * @return array|null New token data or null on failure
	 */
	public function refreshAccessToken(string $refreshToken): ?array {
		// Check if confidential client secret is configured
		$clientSecret = $this->config->getSystemValue('astrolabe_client_secret', '');

		if (empty($clientSecret)) {
			$this->logger->warning('Cannot refresh: no client secret configured. Confidential client required for token refresh.');
			return null;
		}

		try {
			// Get MCP server URL
			$mcpServerUrl = $this->config->getSystemValue('mcp_server_url', '');
			if (empty($mcpServerUrl)) {
				throw new \Exception('MCP server URL not configured');
			}

			// Query MCP server to discover which IdP it's configured to use
			$statusResponse = $this->httpClient->get($mcpServerUrl . '/api/v1/status');
			$statusData = json_decode($statusResponse->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid status response from MCP server');
			}

			// Determine OIDC discovery URL and token endpoint
			$useInternalNextcloud = !isset($statusData['oidc']['discovery_url']);

			if (!$useInternalNextcloud) {
				// External IdP configured - use OIDC discovery
				$discoveryUrl = $statusData['oidc']['discovery_url'];

				$this->logger->debug('IdpTokenRefresher: Using external IdP', [
					'discovery_url' => $discoveryUrl,
				]);

				$discoveryResponse = $this->httpClient->get($discoveryUrl);
				$discovery = json_decode($discoveryResponse->getBody(), true);

				if (json_last_error() !== JSON_ERROR_NONE || !isset($discovery['token_endpoint'])) {
					throw new \RuntimeException('Invalid OIDC discovery response');
				}

				$tokenEndpoint = $discovery['token_endpoint'];
			} else {
				// Nextcloud's OIDC app - use internal URL
				$tokenEndpoint = $this->getNextcloudBaseUrl() . '/apps/oidc/token';

				$this->logger->debug('IdpTokenRefresher: Using Nextcloud OIDC app', [
					'token_endpoint' => $tokenEndpoint,
				]);
			}

			// Call IdP's token endpoint with refresh_token grant
			$postData = [
				'grant_type' => 'refresh_token',
				'refresh_token' => $refreshToken,
				'client_id' => $this->mcpServerClient->getClientId(),
				'client_secret' => $clientSecret,
			];

			$this->logger->info('IdpTokenRefresher: Requesting token refresh');

			$response = $this->httpClient->post($tokenEndpoint, [
				'body' => http_build_query($postData),
				'headers' => [
					'Content-Type' => 'application/x-www-form-urlencoded',
					'Accept' => 'application/json',
				],
			]);

			$tokenData = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE || !isset($tokenData['access_token'])) {
				throw new \RuntimeException('Invalid token response from IdP');
			}

			// Validate refresh_token is present (required for token rotation)
			if (!isset($tokenData['refresh_token'])) {
				$this->logger->error(
					'IdpTokenRefresher: No refresh token in response - token rotation will fail',
					[
						'has_access_token' => isset($tokenData['access_token']),
						'response_keys' => array_keys($tokenData),
					]
				);
				return null;
			}

			$this->logger->info('IdpTokenRefresher: Token refresh successful');

			return $tokenData;

		} catch (\OCP\Http\Client\LocalServerException $e) {
			// Network/connection error - may be transient
			$this->logger->warning('IdpTokenRefresher: Network error during refresh', [
				'error' => $e->getMessage(),
			]);
			return null;
		} catch (\Exception $e) {
			$statusCode = null;
			if (method_exists($e, 'getCode')) {
				$statusCode = $e->getCode();
			}

			// Log with appropriate level based on error type
			if ($statusCode === 401 || $statusCode === 403) {
				// Auth error - token is invalid, should be deleted
				$this->logger->error('IdpTokenRefresher: Auth error - token invalid', [
					'status_code' => $statusCode,
					'error' => $e->getMessage(),
				]);
			} elseif ($statusCode >= 500) {
				// Server error - may be transient
				$this->logger->warning('IdpTokenRefresher: Server error during refresh', [
					'status_code' => $statusCode,
					'error' => $e->getMessage(),
				]);
			} else {
				$this->logger->error('IdpTokenRefresher: Token refresh failed', [
					'status_code' => $statusCode,
					'error' => $e->getMessage(),
				]);
			}
			return null;
		}
	}
}
