<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Service;

use OCP\IConfig;
use OCP\IDBConnection;
use OCP\Lock\ILockingProvider;
use OCP\Lock\LockedException;
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
	private $db;
	private $logger;
	private ILockingProvider $lockingProvider;

	public function __construct(
		IConfig $config,
		ICrypto $crypto,
		IDBConnection $db,
		LoggerInterface $logger,
		ILockingProvider $lockingProvider,
	) {
		$this->config = $config;
		$this->crypto = $crypto;
		$this->db = $db;
		$this->logger = $logger;
		$this->lockingProvider = $lockingProvider;
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
	 * @param int|null $issuedAt Unix timestamp when token was issued (for lifetime calculation)
	 */
	public function storeUserToken(
		string $userId,
		string $accessToken,
		string $refreshToken,
		int $expiresAt,
		?int $issuedAt = null,
	): void {
		try {
			$tokenData = [
				'access_token' => $accessToken,
				'refresh_token' => $refreshToken,
				'expires_at' => $expiresAt,
				'issued_at' => $issuedAt ?? time(),
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
	 * Get the lock path for a user's token refresh operation.
	 *
	 * @param string $userId User ID
	 * @return string Lock path
	 */
	private function getTokenRefreshLockPath(string $userId): string {
		return 'astrolabe/oauth/tokens/' . $userId;
	}

	/**
	 * Execute callback while holding exclusive lock on user's token.
	 *
	 * Prevents race conditions between background job and on-demand token refresh.
	 *
	 * Note: Lock TTL is configured at the Nextcloud server level (default: 3600s).
	 * If a process crashes while holding the lock, it will auto-expire after the TTL.
	 * The ILockingProvider interface does not support per-call timeouts.
	 *
	 * @template T
	 * @param string $userId User ID
	 * @param callable(): T $callback
	 * @return T
	 * @throws LockedException If lock cannot be acquired
	 */
	public function withTokenLock(string $userId, callable $callback): mixed {
		$lockPath = $this->getTokenRefreshLockPath($userId);

		$this->lockingProvider->acquireLock($lockPath, ILockingProvider::LOCK_EXCLUSIVE);
		try {
			return $callback();
		} finally {
			$this->lockingProvider->releaseLock($lockPath, ILockingProvider::LOCK_EXCLUSIVE);
		}
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
	 * Get user IDs that have OAuth tokens stored.
	 *
	 * Queries oc_preferences directly since IConfig doesn't support
	 * listing all users with a specific key set.
	 *
	 * @param int $limit Maximum users to return (0 = no limit, for backward compatibility)
	 * @param int $offset Starting offset for pagination
	 * @return list<string> Array of user IDs
	 */
	public function getAllUsersWithTokens(int $limit = 0, int $offset = 0): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('userid')
			->from('preferences')
			->where($qb->expr()->eq('appid', $qb->createNamedParameter('astrolabe')))
			->andWhere($qb->expr()->eq('configkey', $qb->createNamedParameter('oauth_tokens')));

		if ($limit > 0) {
			$qb->setMaxResults($limit);
		}
		if ($offset > 0) {
			$qb->setFirstResult($offset);
		}

		$result = $qb->executeQuery();
		/** @var list<string> $userIds */
		$userIds = [];
		/** @psalm-suppress MixedAssignment - IResult::fetch() returns mixed */
		while (($row = $result->fetch()) !== false) {
			if (is_array($row) && isset($row['userid']) && is_string($row['userid'])) {
				$userIds[] = $row['userid'];
			}
		}
		$result->closeCursor();

		return $userIds;
	}

	/**
	 * Get the access token for a user, handling expiration and refresh.
	 *
	 * This is a convenience method that combines token retrieval,
	 * expiration checking, and automatic refresh if needed.
	 *
	 * Uses double-check locking pattern to prevent race conditions between
	 * background job and on-demand refresh while minimizing lock contention.
	 *
	 * @param string $userId User ID
	 * @param callable|null $refreshCallback Callback to refresh token if expired
	 *                                       Should accept (refreshToken) and return new token data
	 * @return string|null Access token, or null if not available
	 */
	public function getAccessToken(string $userId, ?callable $refreshCallback = null): ?string {
		// Quick check without lock (optimization)
		$token = $this->getUserToken($userId);

		if (!$token) {
			return null;
		}

		// If not expired, return immediately without lock
		if (!$this->isExpired($token)) {
			return $token['access_token'];
		}

		// Token expired - acquire lock for refresh
		try {
			/**
			 * @return string|null
			 * @psalm-suppress MixedInferredReturnType
			 */
			return $this->withTokenLock($userId, function () use ($userId, $refreshCallback): ?string {
				// Re-check after acquiring lock (double-check pattern)
				// Another process may have refreshed while we waited for the lock
				$currentToken = $this->getUserToken($userId);

				if ($currentToken === null) {
					return null;
				}

				// Check if another process already refreshed the token
				if (!$this->isExpired($currentToken)) {
					$this->logger->debug("Token already refreshed for user $userId while waiting for lock");
					/** @var string */
					return $currentToken['access_token'];
				}

				// Still expired, perform refresh
				if ($refreshCallback && isset($currentToken['refresh_token'])) {
					try {
						/** @var string $refreshToken */
						$refreshToken = $currentToken['refresh_token'];
						$newTokenData = $refreshCallback($refreshToken);

						if ($newTokenData && isset($newTokenData['access_token'])) {
							// Store refreshed token
							// Use new refresh token if provided (rotation), otherwise keep old one
							$now = time();
							/** @var string $accessToken */
							$accessToken = $newTokenData['access_token'];
							/** @var string $newRefreshToken */
							$newRefreshToken = $newTokenData['refresh_token'] ?? $refreshToken;
							$expiresIn = (int)($newTokenData['expires_in'] ?? 3600);

							$this->storeUserToken(
								$userId,
								$accessToken,
								$newRefreshToken,
								$now + $expiresIn,
								$now  // issued_at for accurate lifetime calculation
							);

							return $accessToken;
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
			});
		} catch (LockedException $e) {
			// Could not acquire lock - another process is refreshing
			// Return stale token rather than failing - caller can retry if needed
			$this->logger->warning("Could not acquire token lock for user $userId, returning stale token");
			/** @var string|null $staleToken */
			$staleToken = $token['access_token'] ?? null;
			return $staleToken;
		}
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
