<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Service;

use OCP\IConfig;
use OCP\Security\ICrypto;
use Psr\Log\LoggerInterface;

/**
 * Storage service for per-user MCP OAuth tokens.
 *
 * Stores encrypted access and refresh tokens in user preferences.
 * Handles token expiration checking and refresh logic.
 */
class McpTokenStorage {
	/** Buffer time in seconds before actual expiry to trigger refresh */
	private const TOKEN_EXPIRY_BUFFER_SECONDS = 60;

	private $config;
	private $crypto;
	private $logger;

	public function __construct(
		IConfig $config,
		ICrypto $crypto,
		LoggerInterface $logger,
	) {
		$this->config = $config;
		$this->crypto = $crypto;
		$this->logger = $logger;
	}

	/**
	 * Store MCP OAuth tokens for a user.
	 *
	 * Tokens are encrypted before storage to protect user credentials.
	 *
	 * @param string $userId User ID
	 * @param string $accessToken OAuth access token
	 * @param string $refreshToken OAuth refresh token
	 * @param int $expiresAt Unix timestamp when token expires
	 */
	public function storeUserToken(
		string $userId,
		string $accessToken,
		string $refreshToken,
		int $expiresAt,
	): void {
		try {
			$tokenData = [
				'access_token' => $accessToken,
				'refresh_token' => $refreshToken,
				'expires_at' => $expiresAt,
			];

			// Encrypt token data before storage
			$encrypted = $this->crypto->encrypt(json_encode($tokenData));

			// Store in user preferences
			$this->config->setUserValue(
				$userId,
				'astrolabe',
				'oauth_tokens',
				$encrypted
			);

			$this->logger->info("Stored MCP OAuth tokens for user: $userId");
		} catch (\Exception $e) {
			$this->logger->error("Failed to store MCP tokens for user $userId", [
				'error' => $e->getMessage()
			]);
			throw $e;
		}
	}

	/**
	 * Get MCP OAuth tokens for a user.
	 *
	 * @param string $userId User ID
	 * @return array|null Token data array with keys: access_token, refresh_token, expires_at
	 */
	public function getUserToken(string $userId): ?array {
		try {
			$encrypted = $this->config->getUserValue(
				$userId,
				'astrolabe',
				'oauth_tokens',
				''
			);

			if (empty($encrypted)) {
				return null;
			}

			// Decrypt and parse token data
			$decrypted = $this->crypto->decrypt($encrypted);
			$tokenData = json_decode($decrypted, true);

			if (!$tokenData || !isset($tokenData['access_token'])) {
				$this->logger->warning("Invalid token data for user: $userId");
				return null;
			}

			return $tokenData;
		} catch (\Exception $e) {
			$this->logger->error("Failed to retrieve MCP tokens for user $userId", [
				'error' => $e->getMessage()
			]);
			return null;
		}
	}

	/**
	 * Check if a token is expired or about to expire.
	 *
	 * Uses TOKEN_EXPIRY_BUFFER_SECONDS buffer to refresh tokens before they actually expire.
	 *
	 * @param array $token Token data array
	 * @return bool True if expired or about to expire
	 */
	public function isExpired(array $token): bool {
		if (!isset($token['expires_at'])) {
			return true;
		}

		// Expire early to avoid race conditions
		return time() >= ($token['expires_at'] - self::TOKEN_EXPIRY_BUFFER_SECONDS);
	}

	/**
	 * Delete stored tokens for a user.
	 *
	 * Used when user disconnects or revokes access.
	 *
	 * @param string $userId User ID
	 */
	public function deleteUserToken(string $userId): void {
		try {
			$this->config->deleteUserValue(
				$userId,
				'astrolabe',
				'oauth_tokens'
			);

			$this->logger->info("Deleted MCP OAuth tokens for user: $userId");
		} catch (\Exception $e) {
			$this->logger->error("Failed to delete MCP tokens for user $userId", [
				'error' => $e->getMessage()
			]);
			throw $e;
		}
	}

	/**
	 * Get the access token for a user, handling expiration and refresh.
	 *
	 * This is a convenience method that combines token retrieval,
	 * expiration checking, and automatic refresh if needed.
	 *
	 * @param string $userId User ID
	 * @param callable|null $refreshCallback Callback to refresh token if expired
	 *                                       Should accept (refreshToken) and return new token data
	 * @return string|null Access token, or null if not available
	 */
	public function getAccessToken(string $userId, ?callable $refreshCallback = null): ?string {
		$token = $this->getUserToken($userId);

		if (!$token) {
			return null;
		}

		// Check if token is expired
		if ($this->isExpired($token)) {
			// Try to refresh if callback provided
			if ($refreshCallback && isset($token['refresh_token'])) {
				try {
					$newTokenData = $refreshCallback($token['refresh_token']);

					if ($newTokenData && isset($newTokenData['access_token'])) {
						// Store refreshed token
						// Use new refresh token if provided (rotation), otherwise keep old one
						$this->storeUserToken(
							$userId,
							$newTokenData['access_token'],
							$newTokenData['refresh_token'] ?? $token['refresh_token'],
							time() + ($newTokenData['expires_in'] ?? 3600)
						);

						return $newTokenData['access_token'];
					}
				} catch (\Exception $e) {
					$this->logger->error("Failed to refresh token for user $userId", [
						'error' => $e->getMessage()
					]);
					// Delete stale token to prevent repeated refresh attempts
					$this->deleteUserToken($userId);
					return null;
				}

				// Refresh callback returned null or invalid data - delete stale token
				$this->deleteUserToken($userId);
				$this->logger->info("Deleted stale token for user $userId after refresh failure");
				return null;
			}

			// Token expired and no refresh callback available - delete stale token
			$this->deleteUserToken($userId);
			$this->logger->info("Token expired for user $userId, no refresh available");
			return null;
		}

		return $token['access_token'];
	}

	/**
	 * Store app password for background sync.
	 *
	 * App passwords are encrypted before storage and used as an alternative
	 * to OAuth refresh tokens for background sync operations.
	 *
	 * @param string $userId User ID
	 * @param string $appPassword Nextcloud app password
	 */
	public function storeBackgroundSyncPassword(
		string $userId,
		string $appPassword,
	): void {
		try {
			// Encrypt app password before storage
			$encrypted = $this->crypto->encrypt($appPassword);

			// Store in user preferences
			$this->config->setUserValue(
				$userId,
				'astrolabe',
				'background_sync_password',
				$encrypted
			);

			// Mark credential type
			$this->config->setUserValue(
				$userId,
				'astrolabe',
				'background_sync_type',
				'app_password'
			);

			// Store provisioned timestamp
			$this->config->setUserValue(
				$userId,
				'astrolabe',
				'background_sync_provisioned_at',
				(string)time()
			);

			$this->logger->info("Stored background sync app password for user: $userId");
		} catch (\Exception $e) {
			$this->logger->error("Failed to store app password for user $userId", [
				'error' => $e->getMessage()
			]);
			throw $e;
		}
	}

	/**
	 * Get app password for background sync.
	 *
	 * @param string $userId User ID
	 * @return string|null Decrypted app password, or null if not set
	 */
	public function getBackgroundSyncPassword(string $userId): ?string {
		try {
			$encrypted = $this->config->getUserValue(
				$userId,
				'astrolabe',
				'background_sync_password',
				''
			);

			if (empty($encrypted)) {
				return null;
			}

			// Decrypt app password
			return $this->crypto->decrypt($encrypted);
		} catch (\Exception $e) {
			$this->logger->error("Failed to retrieve app password for user $userId", [
				'error' => $e->getMessage()
			]);
			return null;
		}
	}

	/**
	 * Delete background sync app password for a user.
	 *
	 * @param string $userId User ID
	 */
	public function deleteBackgroundSyncPassword(string $userId): void {
		try {
			$this->config->deleteUserValue(
				$userId,
				'astrolabe',
				'background_sync_password'
			);

			$this->config->deleteUserValue(
				$userId,
				'astrolabe',
				'background_sync_type'
			);

			$this->config->deleteUserValue(
				$userId,
				'astrolabe',
				'background_sync_provisioned_at'
			);

			$this->logger->info("Deleted background sync app password for user: $userId");
		} catch (\Exception $e) {
			$this->logger->error("Failed to delete app password for user $userId", [
				'error' => $e->getMessage()
			]);
			throw $e;
		}
	}

	/**
	 * Check if user has provisioned background sync access.
	 *
	 * Returns true if either OAuth tokens or app password is configured.
	 *
	 * @param string $userId User ID
	 * @return bool True if background sync is provisioned
	 */
	public function hasBackgroundSyncAccess(string $userId): bool {
		// Check for OAuth tokens
		$oauthToken = $this->getUserToken($userId);
		if ($oauthToken !== null) {
			return true;
		}

		// Check for app password
		$appPassword = $this->getBackgroundSyncPassword($userId);
		return $appPassword !== null;
	}

	/**
	 * Get background sync credential type for a user.
	 *
	 * @param string $userId User ID
	 * @return string|null 'oauth' or 'app_password', or null if not provisioned
	 */
	public function getBackgroundSyncType(string $userId): ?string {
		$type = $this->config->getUserValue(
			$userId,
			'astrolabe',
			'background_sync_type',
			''
		);

		// Fallback to OAuth if tokens exist but type not set
		if (empty($type) && $this->getUserToken($userId) !== null) {
			return 'oauth';
		}

		return empty($type) ? null : $type;
	}

	/**
	 * Get background sync provisioned timestamp for a user.
	 *
	 * @param string $userId User ID
	 * @return int|null Unix timestamp, or null if not provisioned
	 */
	public function getBackgroundSyncProvisionedAt(string $userId): ?int {
		$timestamp = $this->config->getUserValue(
			$userId,
			'astrolabe',
			'background_sync_provisioned_at',
			''
		);

		return empty($timestamp) ? null : (int)$timestamp;
	}
}
