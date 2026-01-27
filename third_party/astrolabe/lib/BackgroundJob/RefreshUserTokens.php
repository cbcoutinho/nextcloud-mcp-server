<?php

declare(strict_types=1);

namespace OCA\Astrolabe\BackgroundJob;

use OCA\Astrolabe\Service\IdpTokenRefresher;
use OCA\Astrolabe\Service\McpTokenStorage;
use OCP\AppFramework\Utility\ITimeFactory;
use OCP\BackgroundJob\IJob;
use OCP\BackgroundJob\TimedJob;
use OCP\Lock\LockedException;
use Psr\Log\LoggerInterface;

/**
 * Background job to proactively refresh OAuth tokens before expiration.
 *
 * Runs every 15 minutes and refreshes tokens based on their actual expiration
 * time. Works with any IdP (Nextcloud OIDC, Keycloak, etc.) since it uses
 * the real token expiration rather than IdP configuration.
 *
 * Refresh strategy: Refresh when less than 50% of token lifetime remains,
 * ensuring tokens are refreshed well before expiration regardless of the
 * IdP's configured token lifetime.
 *
 * @psalm-suppress UnusedClass - Background jobs are loaded dynamically by Nextcloud
 */
class RefreshUserTokens extends TimedJob {
	/** Job runs every 15 minutes */
	private const JOB_INTERVAL_SECONDS = 900;

	/** Refresh when this percentage of token lifetime remains */
	private const REFRESH_AT_REMAINING_PERCENT = 0.5;

	/** Minimum threshold to avoid constant refresh (5 minutes) */
	private const MIN_THRESHOLD_SECONDS = 300;

	/** Default assumed token lifetime if we can't determine it (1 hour) */
	private const DEFAULT_TOKEN_LIFETIME_SECONDS = 3600;

	public function __construct(
		ITimeFactory $time,
		private McpTokenStorage $tokenStorage,
		private IdpTokenRefresher $tokenRefresher,
		private LoggerInterface $logger,
	) {
		parent::__construct($time);
		$this->setInterval(self::JOB_INTERVAL_SECONDS);
		$this->setTimeSensitivity(IJob::TIME_INSENSITIVE);
	}

	protected function run(mixed $argument): void {
		$this->logger->info('RefreshUserTokens: Starting background token refresh');

		$userIds = $this->tokenStorage->getAllUsersWithTokens();
		$this->logger->debug('RefreshUserTokens: Found ' . count($userIds) . ' users with tokens');

		$refreshed = 0;
		$failed = 0;
		$skipped = 0;

		foreach ($userIds as $userId) {
			$result = $this->refreshUserTokenIfNeeded($userId);
			match ($result) {
				'refreshed' => $refreshed++,
				'failed' => $failed++,
				'skipped' => $skipped++,
			};
		}

		$this->logger->info("RefreshUserTokens: Complete - refreshed=$refreshed, failed=$failed, skipped=$skipped");
	}

	/**
	 * Refresh a user's token if it's nearing expiration.
	 *
	 * Calculates the refresh threshold based on the token's actual lifetime,
	 * refreshing when less than 50% of the lifetime remains.
	 *
	 * Uses locking to prevent race conditions with on-demand refresh in
	 * getAccessToken(). If lock cannot be acquired, skips this user since
	 * on-demand refresh is already handling it.
	 *
	 * @return string 'refreshed', 'failed', or 'skipped'
	 */
	private function refreshUserTokenIfNeeded(string $userId): string {
		$token = $this->tokenStorage->getUserToken($userId);

		if ($token === null) {
			return 'skipped';
		}

		$expiresAt = (int)($token['expires_at'] ?? 0);
		$issuedAt = isset($token['issued_at']) ? (int)$token['issued_at'] : null;
		$timeRemaining = $expiresAt - time();

		// Calculate token lifetime from stored data or use default
		if ($issuedAt !== null) {
			$tokenLifetime = $expiresAt - $issuedAt;
		} else {
			// Fallback: use default lifetime assumption
			$tokenLifetime = self::DEFAULT_TOKEN_LIFETIME_SECONDS;
		}

		// Calculate threshold: refresh when 50% of lifetime remains
		$threshold = max(
			(int)($tokenLifetime * self::REFRESH_AT_REMAINING_PERCENT),
			self::MIN_THRESHOLD_SECONDS
		);

		if ($timeRemaining > $threshold) {
			// Token still has plenty of time, skip
			return 'skipped';
		}

		// Token is expiring soon, attempt refresh with lock
		try {
			return $this->tokenStorage->withTokenLock($userId, function () use ($userId) {
				// Re-check token after acquiring lock (double-check pattern)
				// Another process may have refreshed while we waited for lock
				$currentToken = $this->tokenStorage->getUserToken($userId);

				if ($currentToken === null) {
					return 'skipped';
				}

				// Recalculate threshold with current token data
				$currentExpiresAt = (int)($currentToken['expires_at'] ?? 0);
				$currentIssuedAt = isset($currentToken['issued_at']) ? (int)$currentToken['issued_at'] : null;
				$currentTimeRemaining = $currentExpiresAt - time();

				if ($currentIssuedAt !== null) {
					$currentTokenLifetime = $currentExpiresAt - $currentIssuedAt;
				} else {
					$currentTokenLifetime = self::DEFAULT_TOKEN_LIFETIME_SECONDS;
				}

				$currentThreshold = max(
					(int)($currentTokenLifetime * self::REFRESH_AT_REMAINING_PERCENT),
					self::MIN_THRESHOLD_SECONDS
				);

				if ($currentTimeRemaining > $currentThreshold) {
					// Token was refreshed by another process while we waited
					$this->logger->debug("RefreshUserTokens: Token already refreshed for user $userId while waiting for lock");
					return 'skipped';
				}

				// Still needs refresh, proceed
				if (!isset($currentToken['refresh_token'])) {
					$this->logger->warning("RefreshUserTokens: User $userId has no refresh token");
					return 'failed';
				}

				$this->logger->debug("RefreshUserTokens: Refreshing token for user $userId (remaining={$currentTimeRemaining}s, threshold={$currentThreshold}s)");

				/** @var string $refreshToken */
				$refreshToken = $currentToken['refresh_token'];
				$newTokenData = $this->tokenRefresher->refreshAccessToken($refreshToken);

				if ($newTokenData === null) {
					$this->logger->warning("RefreshUserTokens: Refresh returned null for user $userId");
					// Don't delete token here - let on-demand refresh handle cleanup
					return 'failed';
				}

				// Calculate new expiration and store issued_at for future calculations
				$expiresIn = (int)($newTokenData['expires_in'] ?? self::DEFAULT_TOKEN_LIFETIME_SECONDS);
				$now = time();

				/** @var string $accessToken */
				$accessToken = $newTokenData['access_token'];
				/** @var string $newRefreshToken */
				$newRefreshToken = $newTokenData['refresh_token'] ?? $refreshToken;

				$this->tokenStorage->storeUserToken(
					$userId,
					$accessToken,
					$newRefreshToken,
					$now + $expiresIn,
					$now  // issued_at
				);

				$this->logger->debug("RefreshUserTokens: Successfully refreshed token for user $userId");
				return 'refreshed';
			});
		} catch (LockedException $e) {
			// Lock held by on-demand refresh - expected, not an error
			$this->logger->debug("RefreshUserTokens: Lock held for user $userId, skipping");
			return 'skipped';
		} catch (\Exception $e) {
			$this->logger->error("RefreshUserTokens: Failed to refresh for user $userId: " . $e->getMessage());
			return 'failed';
		}
	}
}
