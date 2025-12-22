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

		// Fetch server status to determine auth mode
		$serverStatus = $this->client->getStatus();

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

		// Get auth mode from server (defaults to oauth if not specified)
		$authMode = $serverStatus['auth_mode'] ?? 'oauth';
		$supportsAppPasswords = $serverStatus['supports_app_passwords'] ?? false;

		// Check if user has MCP OAuth token
		$token = $this->tokenStorage->getUserToken($userId);

		// For multi_user_basic mode with app password support, check if user has app password
		if ($authMode === 'multi_user_basic' && $supportsAppPasswords) {
			// Check if user has already provided an app password
			$hasBackgroundAccess = $this->tokenStorage->hasBackgroundSyncAccess($userId);

			if (!$hasBackgroundAccess) {
				// No app password yet - show app password entry form
				return new TemplateResponse(
					Application::APP_ID,
					'settings/personal',
					[
						'serverUrl' => $this->client->getPublicServerUrl(), // Changed from server_url to serverUrl
						'serverStatus' => $serverStatus,
						'auth_mode' => $authMode,
						'authMode' => $authMode, // Add camelCase version for template
						'supports_app_passwords' => $supportsAppPasswords,
						'supportsAppPasswords' => $supportsAppPasswords, // Add camelCase version
						'session' => null, // No session yet
						'hasBackgroundAccess' => false, // FIXED: Add missing parameter
						'backgroundAccessGranted' => false, // FIXED: Add missing parameter
						'vectorSyncEnabled' => $serverStatus['vector_sync_enabled'] ?? false,
						'hasToken' => false, // No OAuth token in multi_user_basic mode
						'requesttoken' => \OCP\Util::callRegister(),
					],
					TemplateResponse::RENDER_AS_BLANK
				);
			} else {
				// User has app password - show active status
				$backgroundSyncType = $this->tokenStorage->getBackgroundSyncType($userId);
				$backgroundSyncProvisionedAt = $this->tokenStorage->getBackgroundSyncProvisionedAt($userId);

				$parameters = [
					'userId' => $userId,
					'serverStatus' => $serverStatus,
					'session' => null, // No user session for app passwords
					'vectorSyncEnabled' => $serverStatus['vector_sync_enabled'] ?? false,
					'backgroundAccessGranted' => true, // App password grants background access
					'serverUrl' => $this->client->getPublicServerUrl(),
					'hasToken' => false, // No OAuth token
					'hasBackgroundAccess' => true,
					'backgroundSyncType' => $backgroundSyncType,
					'backgroundSyncProvisionedAt' => $backgroundSyncProvisionedAt,
					'authMode' => $authMode,
					'supportsAppPasswords' => $supportsAppPasswords,
					'requesttoken' => \OCP\Util::callRegister(),
				];

				return new TemplateResponse(
					Application::APP_ID,
					'settings/personal',
					$parameters,
					TemplateResponse::RENDER_AS_BLANK
				);
			}
		}
		// For OAuth modes, if no token or token is expired, show OAuth authorization UI
		elseif (!$token || $this->tokenStorage->isExpired($token)) {
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

		// Check background sync credential status
		$hasBackgroundAccess = $this->tokenStorage->hasBackgroundSyncAccess($userId);
		$backgroundSyncType = $this->tokenStorage->getBackgroundSyncType($userId);
		$backgroundSyncProvisionedAt = $this->tokenStorage->getBackgroundSyncProvisionedAt($userId);

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
			'hasBackgroundAccess' => $hasBackgroundAccess,
			'backgroundSyncType' => $backgroundSyncType,
			'backgroundSyncProvisionedAt' => $backgroundSyncProvisionedAt,
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
