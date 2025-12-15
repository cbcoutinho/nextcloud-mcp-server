<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Settings;

use OCA\Astrolabe\AppInfo\Application;
use OCA\Astrolabe\Service\McpServerClient;
use OCA\Astrolabe\Service\McpTokenStorage;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\AppFramework\Services\IInitialState;
use OCP\IURLGenerator;
use OCP\IUserSession;
use OCP\Settings\ISettings;

/**
 * Personal settings panel for Astrolabe.
 *
 * Displays semantic search status, background indexing access,
 * and provides controls for managing content indexing.
 *
 * Uses OAuth PKCE flow - each user must authorize background access.
 */
class Personal implements ISettings {
	private $client;
	private $userSession;
	private $initialState;
	private $tokenStorage;
	private $urlGenerator;

	public function __construct(
		McpServerClient $client,
		IUserSession $userSession,
		IInitialState $initialState,
		McpTokenStorage $tokenStorage,
		IURLGenerator $urlGenerator,
	) {
		$this->client = $client;
		$this->userSession = $userSession;
		$this->initialState = $initialState;
		$this->tokenStorage = $tokenStorage;
		$this->urlGenerator = $urlGenerator;
	}

	/**
	 * @return TemplateResponse
	 */
	public function getForm(): TemplateResponse {
		$user = $this->userSession->getUser();
		if (!$user) {
			return new TemplateResponse(Application::APP_ID, 'settings/error', [
				'error' => 'User not authenticated'
			], TemplateResponse::RENDER_AS_BLANK);
		}

		$userId = $user->getUID();

		// Check if user has MCP OAuth token
		$token = $this->tokenStorage->getUserToken($userId);

		// If no token or token is expired, show OAuth authorization UI
		if (!$token || $this->tokenStorage->isExpired($token)) {
			$oauthUrl = $this->urlGenerator->linkToRoute('astrolabe.oauth.initiateOAuth');

			return new TemplateResponse(
				Application::APP_ID,
				'settings/oauth-required',
				[
					'oauth_url' => $oauthUrl,
					'server_url' => $this->client->getPublicServerUrl(),
					'has_expired' => ($token !== null), // true if token exists but expired
				],
				TemplateResponse::RENDER_AS_BLANK
			);
		}

		// User has valid token - fetch data from MCP server
		$accessToken = $token['access_token'];

		// Fetch server status (public endpoint, no token needed)
		$serverStatus = $this->client->getStatus();

		// Fetch user session data (requires token)
		$userSession = $this->client->getUserSession($userId, $accessToken);

		// Check for server connection error
		if (isset($serverStatus['error'])) {
			return new TemplateResponse(
				Application::APP_ID,
				'settings/error',
				[
					'error' => 'Cannot connect to MCP server',
					'details' => $serverStatus['error'],
					'server_url' => $this->client->getPublicServerUrl(),
				],
				TemplateResponse::RENDER_AS_BLANK
			);
		}

		// Check for authentication error (invalid/expired token)
		if (isset($userSession['error'])) {
			// Token might be invalid - delete it and show OAuth UI
			$this->tokenStorage->deleteUserToken($userId);

			$oauthUrl = $this->urlGenerator->linkToRoute('astrolabe.oauth.initiateOAuth');

			return new TemplateResponse(
				Application::APP_ID,
				'settings/oauth-required',
				[
					'oauth_url' => $oauthUrl,
					'server_url' => $this->client->getPublicServerUrl(),
					'has_expired' => true,
					'error_message' => 'Your session has expired. Please sign in again.',
				],
				TemplateResponse::RENDER_AS_BLANK
			);
		}

		// Provide initial state for Vue.js frontend (if needed)
		$this->initialState->provideInitialState('user-data', [
			'userId' => $userId,
			'serverStatus' => $serverStatus,
			'session' => $userSession,
		]);

		$parameters = [
			'userId' => $userId,
			'serverStatus' => $serverStatus,
			'session' => $userSession,
			'vectorSyncEnabled' => $serverStatus['vector_sync_enabled'] ?? false,
			'backgroundAccessGranted' => $userSession['background_access_granted'] ?? false,
			'serverUrl' => $this->client->getPublicServerUrl(),
			'hasToken' => true,
		];

		return new TemplateResponse(
			Application::APP_ID,
			'settings/personal',
			$parameters,
			TemplateResponse::RENDER_AS_BLANK
		);
	}

	/**
	 * @return string The section ID
	 */
	public function getSection(): string {
		return 'astrolabe';
	}

	/**
	 * @return int Priority (lower = higher up)
	 */
	public function getPriority(): int {
		return 50;
	}
}
