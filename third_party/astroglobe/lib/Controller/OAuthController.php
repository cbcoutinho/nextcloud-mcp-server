<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Controller;

use OCA\Astroglobe\Service\McpTokenStorage;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\RedirectResponse;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\Http\Client\IClientService;
use OCP\IConfig;
use OCP\IL10N;
use OCP\IRequest;
use OCP\ISession;
use OCP\IURLGenerator;
use OCP\IUserSession;
use Psr\Log\LoggerInterface;

/**
 * OAuth controller for MCP Server UI.
 *
 * Implements OAuth 2.0 Authorization Code flow with PKCE (Public Client).
 * Does not require client secret, suitable for Nextcloud's public client model.
 */
class OAuthController extends Controller {
	private $config;
	private $session;
	private $userSession;
	private $urlGenerator;
	private $tokenStorage;
	private $logger;
	private $l;
	private $httpClient;

	public function __construct(
		string $appName,
		IRequest $request,
		IConfig $config,
		ISession $session,
		IUserSession $userSession,
		IURLGenerator $urlGenerator,
		McpTokenStorage $tokenStorage,
		LoggerInterface $logger,
		IL10N $l,
		IClientService $clientService
	) {
		parent::__construct($appName, $request);
		$this->config = $config;
		$this->session = $session;
		$this->userSession = $userSession;
		$this->urlGenerator = $urlGenerator;
		$this->tokenStorage = $tokenStorage;
		$this->logger = $logger;
		$this->l = $l;
		$this->httpClient = $clientService->newClient();
	}

	/**
	 * Initiate OAuth authorization flow with PKCE.
	 *
	 * Generates PKCE code verifier and challenge, stores state in session,
	 * then redirects user to IdP authorization endpoint.
	 *
	 * @return RedirectResponse|TemplateResponse
	 */
	#[NoAdminRequired]
	#[NoCSRFRequired]
	public function initiateOAuth() {
		$this->logger->info("initiateOAuth called");

		$user = $this->userSession->getUser();
		if (!$user) {
			$this->logger->error("initiateOAuth: User not authenticated");
			return new TemplateResponse(
				'astroglobe',
				'settings/error',
				['error' => $this->l->t('User not authenticated')]
			);
		}

		$this->logger->info("initiateOAuth: User authenticated: " . $user->getUID());

		try {
			// Get MCP server configuration
			$mcpServerUrl = $this->config->getSystemValue('mcp_server_url', '');
			if (empty($mcpServerUrl)) {
				throw new \Exception('MCP server URL not configured');
			}

			// Generate PKCE values
			$codeVerifier = bin2hex(random_bytes(32));
			$codeChallenge = $this->base64UrlEncode(hash('sha256', $codeVerifier, true));

			// Generate state for CSRF protection
			$state = bin2hex(random_bytes(16));

			// Store PKCE values and state in session
			$this->session->set('mcp_oauth_code_verifier', $codeVerifier);
			$this->session->set('mcp_oauth_state', $state);
			$this->session->set('mcp_oauth_user_id', $user->getUID());

			// Build OAuth authorization URL
			$authUrl = $this->buildAuthorizationUrl(
				$mcpServerUrl,
				$state,
				$codeChallenge
			);

			$this->logger->info("Initiating OAuth flow for user: " . $user->getUID());

			return new RedirectResponse($authUrl);
		} catch (\Exception $e) {
			$this->logger->error('Failed to initiate OAuth flow', [
				'error' => $e->getMessage()
			]);

			return new TemplateResponse(
				'astroglobe',
				'settings/error',
				['error' => $this->l->t('Failed to initiate OAuth: %s', [$e->getMessage()])]
			);
		}
	}

	/**
	 * Handle OAuth callback after user authorization.
	 *
	 * Validates state, exchanges authorization code for access token using PKCE,
	 * and stores tokens for the user.
	 *
	 * @param string $code Authorization code
	 * @param string $state State parameter for CSRF protection
	 * @param string|null $error Error from IdP
	 * @param string|null $error_description Error description from IdP
	 * @return RedirectResponse
	 */
	#[NoAdminRequired]
	#[NoCSRFRequired]
	public function oauthCallback(
		string $code = '',
		string $state = '',
		?string $error = null,
		?string $error_description = null
	): RedirectResponse {
		try {
			// Check for errors from IdP
			if ($error) {
				throw new \Exception("OAuth error: $error - " . ($error_description ?? ''));
			}

			// Validate state to prevent CSRF
			$storedState = $this->session->get('mcp_oauth_state');
			if (empty($storedState) || $state !== $storedState) {
				throw new \Exception('Invalid state parameter (CSRF protection)');
			}

			// Get stored PKCE verifier
			$codeVerifier = $this->session->get('mcp_oauth_code_verifier');
			if (empty($codeVerifier)) {
				throw new \Exception('Code verifier not found in session');
			}

			// Get user ID from session
			$userId = $this->session->get('mcp_oauth_user_id');
			if (empty($userId)) {
				throw new \Exception('User ID not found in session');
			}

			// Get MCP server configuration
			$mcpServerUrl = $this->config->getSystemValue('mcp_server_url', '');
			if (empty($mcpServerUrl)) {
				throw new \Exception('MCP server URL not configured');
			}

			// Exchange authorization code for tokens
			$tokenData = $this->exchangeCodeForToken(
				$mcpServerUrl,
				$code,
				$codeVerifier
			);

			// Store tokens for user
			$this->tokenStorage->storeUserToken(
				$userId,
				$tokenData['access_token'],
				$tokenData['refresh_token'] ?? '',
				time() + ($tokenData['expires_in'] ?? 3600)
			);

			// Clean up session
			$this->session->remove('mcp_oauth_code_verifier');
			$this->session->remove('mcp_oauth_state');
			$this->session->remove('mcp_oauth_user_id');

			$this->logger->info("OAuth flow completed successfully for user: $userId");

			// Redirect back to personal settings
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
			);
		} catch (\Exception $e) {
			$this->logger->error('OAuth callback failed', [
				'error' => $e->getMessage()
			]);

			// Clean up session
			$this->session->remove('mcp_oauth_code_verifier');
			$this->session->remove('mcp_oauth_state');
			$this->session->remove('mcp_oauth_user_id');

			// Redirect to settings with error
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', [
					'section' => 'astroglobe',
					'error' => urlencode($e->getMessage())
				])
			);
		}
	}

	/**
	 * Disconnect user's MCP OAuth tokens.
	 *
	 * Deletes stored tokens from Nextcloud. Note: Does not revoke tokens on IdP side.
	 *
	 * @return RedirectResponse
	 */
	#[NoAdminRequired]
	public function disconnect(): RedirectResponse {
		$user = $this->userSession->getUser();
		if (!$user) {
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
			);
		}

		$userId = $user->getUID();

		try {
			$this->tokenStorage->deleteUserToken($userId);
			$this->logger->info("Disconnected MCP OAuth for user: $userId");
		} catch (\Exception $e) {
			$this->logger->error("Failed to disconnect MCP OAuth for user $userId", [
				'error' => $e->getMessage()
			]);
		}

		return new RedirectResponse(
			$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
		);
	}

	/**
	 * Build OAuth authorization URL with PKCE.
	 *
	 * Queries MCP server for IdP configuration, then performs OIDC discovery
	 * to find the authorization endpoint. Supports both Nextcloud OIDC and
	 * external IdPs like Keycloak.
	 *
	 * @param string $mcpServerUrl Base URL of MCP server
	 * @param string $state CSRF state parameter
	 * @param string $codeChallenge PKCE code challenge
	 * @return string Authorization URL
	 * @throws \Exception if OIDC discovery fails
	 */
	private function buildAuthorizationUrl(
		string $mcpServerUrl,
		string $state,
		string $codeChallenge
	): string {
		// First, query MCP server to discover which IdP it's configured to use
		$this->logger->info('buildAuthorizationUrl: Starting', [
			'mcp_server_url' => $mcpServerUrl,
		]);

		try {
			$statusUrl = $mcpServerUrl . '/api/v1/status';
			$this->logger->info('buildAuthorizationUrl: Fetching MCP server status', [
				'url' => $statusUrl,
			]);

			$statusResponse = $this->httpClient->get($statusUrl);
			$statusData = json_decode($statusResponse->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON in status response: ' . json_last_error_msg());
			}

			$this->logger->info('buildAuthorizationUrl: MCP server status received', [
				'auth_mode' => $statusData['auth_mode'] ?? 'unknown',
				'has_oidc' => isset($statusData['oidc']),
				'oidc_discovery_url' => $statusData['oidc']['discovery_url'] ?? 'not_set',
			]);

		} catch (\Exception $e) {
			$this->logger->error('buildAuthorizationUrl: Failed to fetch MCP server status', [
				'url' => $mcpServerUrl . '/api/v1/status',
				'error' => $e->getMessage(),
				'trace' => $e->getTraceAsString(),
			]);
			throw new \Exception('Cannot connect to MCP server: ' . $e->getMessage());
		}

		// Determine OIDC discovery URL
		// Priority: 1) MCP server's configured discovery URL, 2) Nextcloud OIDC app
		if (isset($statusData['oidc']['discovery_url'])) {
			// MCP server has external IdP configured (e.g., Keycloak)
			$discoveryUrl = $statusData['oidc']['discovery_url'];
			$this->logger->info('Using IdP from MCP server configuration', [
				'discovery_url' => $discoveryUrl,
			]);
		} else {
			// Fall back to Nextcloud's OIDC app
			// Use internal localhost URL for HTTP request (always accessible from inside container)
			// The OIDC discovery response will contain proper external URLs based on overwrite.cli.url
			$discoveryUrl = 'http://localhost/.well-known/openid-configuration';

			$this->logger->info('Using Nextcloud OIDC app as IdP (internal request)', [
				'discovery_url' => $discoveryUrl,
			]);
		}

		// Perform OIDC discovery
		$this->logger->info('buildAuthorizationUrl: Starting OIDC discovery', [
			'discovery_url' => $discoveryUrl,
		]);

		try {
			$response = $this->httpClient->get($discoveryUrl);
			$responseBody = $response->getBody();
			$this->logger->info('buildAuthorizationUrl: Got OIDC discovery response', [
				'status_code' => $response->getStatusCode(),
				'body_length' => strlen($responseBody),
			]);

			$discovery = json_decode($responseBody, true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid JSON in OIDC discovery: ' . json_last_error_msg());
			}

			if (!isset($discovery['authorization_endpoint'])) {
				throw new \RuntimeException('Missing authorization_endpoint in OIDC discovery');
			}

			$authEndpoint = $discovery['authorization_endpoint'];
			$this->logger->info('buildAuthorizationUrl: OIDC discovery succeeded', [
				'auth_endpoint' => $authEndpoint,
				'token_endpoint' => $discovery['token_endpoint'] ?? 'not_set',
			]);

		} catch (\Exception $e) {
			$this->logger->error('buildAuthorizationUrl: OIDC discovery failed', [
				'discovery_url' => $discoveryUrl,
				'error' => $e->getMessage(),
				'trace' => $e->getTraceAsString(),
			]);
			throw new \Exception('Failed to discover OAuth endpoints: ' . $e->getMessage());
		}

		// Build callback URL
		$redirectUri = $this->urlGenerator->linkToRouteAbsolute(
			'astroglobe.oauth.oauthCallback'
		);

		// Get public MCP server URL for token audience (RFC 8707 Resource Indicator)
		// Use public URL that clients/browsers see, not internal Docker URL
		$mcpServerPublicUrl = $this->config->getSystemValue('mcp_server_public_url', $mcpServerUrl);

		// Build authorization URL with PKCE
		$params = [
			'client_id' => 'nextcloudMcpServerUIPublicClient',  // Public client ID (32+ chars required by NC OIDC)
			'redirect_uri' => $redirectUri,
			'response_type' => 'code',
			'scope' => 'openid profile email mcp:read mcp:write',  // Request MCP scopes
			'state' => $state,
			'code_challenge' => $codeChallenge,
			'code_challenge_method' => 'S256',
			'resource' => $mcpServerPublicUrl,  // RFC 8707 Resource Indicator - request token with MCP server audience
		];

		return $authEndpoint . '?' . http_build_query($params);
	}

	/**
	 * Exchange authorization code for access token using PKCE.
	 *
	 * Queries MCP server for IdP configuration, then performs OIDC discovery
	 * to find the token endpoint. Supports both Nextcloud OIDC and external IdPs.
	 *
	 * @param string $mcpServerUrl Base URL of MCP server
	 * @param string $code Authorization code
	 * @param string $codeVerifier PKCE code verifier
	 * @return array Token data containing access_token, refresh_token, expires_in
	 * @throws \Exception on HTTP or token error
	 */
	private function exchangeCodeForToken(
		string $mcpServerUrl,
		string $code,
		string $codeVerifier
	): array {
		// Query MCP server to discover which IdP it's configured to use
		try {
			$statusResponse = $this->httpClient->get($mcpServerUrl . '/api/v1/status');
			$statusData = json_decode($statusResponse->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE) {
				throw new \RuntimeException('Invalid status response from MCP server');
			}

		} catch (\Exception $e) {
			$this->logger->error('Failed to fetch MCP server status during token exchange', [
				'error' => $e->getMessage(),
			]);
			throw new \Exception('Cannot connect to MCP server: ' . $e->getMessage());
		}

		// Determine OIDC discovery URL and token endpoint
		$useInternalNextcloud = !isset($statusData['oidc']['discovery_url']);

		if (!$useInternalNextcloud) {
			// External IdP configured - use discovery
			$discoveryUrl = $statusData['oidc']['discovery_url'];

			try {
				$response = $this->httpClient->get($discoveryUrl);
				$discovery = json_decode($response->getBody(), true);

				if (json_last_error() !== JSON_ERROR_NONE || !isset($discovery['token_endpoint'])) {
					throw new \RuntimeException('Invalid OIDC discovery response');
				}

				$tokenEndpoint = $discovery['token_endpoint'];

			} catch (\Exception $e) {
				$this->logger->error('OIDC discovery failed during token exchange', [
					'discovery_url' => $discoveryUrl,
					'error' => $e->getMessage(),
				]);
				throw new \Exception('Failed to discover token endpoint: ' . $e->getMessage());
			}
		} else {
			// Nextcloud's OIDC app - use internal URL directly (no HTTP request needed)
			// This avoids network issues when overwritehost includes external port
			$tokenEndpoint = 'http://localhost/apps/oidc/token';
		}

		$redirectUri = $this->urlGenerator->linkToRouteAbsolute(
			'astroglobe.oauth.oauthCallback'
		);

		$postData = [
			'grant_type' => 'authorization_code',
			'code' => $code,
			'redirect_uri' => $redirectUri,
			'client_id' => 'nextcloudMcpServerUIPublicClient',  // Public client (32+ chars required by NC OIDC)
			'code_verifier' => $codeVerifier,  // PKCE proof
		];

		// Use Nextcloud's HTTP client for token request
		try {
			$response = $this->httpClient->post($tokenEndpoint, [
				'body' => http_build_query($postData),
				'headers' => [
					'Content-Type' => 'application/x-www-form-urlencoded',
					'Accept' => 'application/json',
				],
			]);

			$tokenData = json_decode($response->getBody(), true);

			if (json_last_error() !== JSON_ERROR_NONE || !isset($tokenData['access_token'])) {
				throw new \RuntimeException('Invalid token response from server');
			}

			return $tokenData;

		} catch (\Exception $e) {
			$this->logger->error('Token exchange failed', [
				'error' => $e->getMessage(),
				'token_endpoint' => $tokenEndpoint,
			]);
			throw new \Exception('Token exchange failed: ' . $e->getMessage());
		}
	}

	/**
	 * Base64 URL-safe encoding (for PKCE).
	 *
	 * @param string $data Data to encode
	 * @return string Base64 URL-encoded string
	 */
	private function base64UrlEncode(string $data): string {
		return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
	}
}
