<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Service;

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
				'astroglobe',
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
				'astroglobe',
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
	 * Uses a 60-second buffer to refresh tokens before they actually expire.
	 *
	 * @param array $token Token data array
	 * @return bool True if expired or about to expire
	 */
	public function isExpired(array $token): bool {
		if (!isset($token['expires_at'])) {
			return true;
		}

		// Expire 60 seconds early to avoid race conditions
		return time() >= ($token['expires_at'] - 60);
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
				'astroglobe',
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
					// Fall through to return null
				}
			}

			// Token expired and no refresh available
			$this->logger->info("Token expired for user $userId, no refresh available");
			return null;
		}

		return $token['access_token'];
	}
}
