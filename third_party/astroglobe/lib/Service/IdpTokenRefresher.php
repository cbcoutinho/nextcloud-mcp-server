<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Service;

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
	 * Refresh access token using refresh token.
	 *
	 * Calls IdP's token endpoint directly (NOT MCP server).
	 *
	 * @param string $refreshToken The refresh token
	 * @return array|null New token data or null on failure
	 */
	public function refreshAccessToken(string $refreshToken): ?array {
		// Check if confidential client secret is configured
		$clientSecret = $this->config->getSystemValue('astroglobe_client_secret', '');

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

				$this->logger->info('IdpTokenRefresher: Using external IdP', [
					'discovery_url' => $discoveryUrl,
				]);

				$discoveryResponse = $this->httpClient->get($discoveryUrl);
				$discovery = json_decode($discoveryResponse->getBody(), true);

				if (json_last_error() !== JSON_ERROR_NONE || !isset($discovery['token_endpoint'])) {
					throw new \RuntimeException('Invalid OIDC discovery response');
				}

				$tokenEndpoint = $discovery['token_endpoint'];
			} else {
				// Nextcloud's OIDC app - use internal URL directly
				$tokenEndpoint = 'http://localhost/apps/oidc/token';

				$this->logger->info('IdpTokenRefresher: Using Nextcloud OIDC app', [
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

			$this->logger->info('IdpTokenRefresher: Token refresh successful');

			return $tokenData;

		} catch (\Exception $e) {
			$this->logger->error('IdpTokenRefresher: Token refresh failed', [
				'error' => $e->getMessage(),
			]);
			return null;
		}
	}
}
