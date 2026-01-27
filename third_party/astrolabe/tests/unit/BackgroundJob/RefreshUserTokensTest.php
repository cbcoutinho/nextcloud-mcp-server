<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Tests\Unit\BackgroundJob;

use OCA\Astrolabe\BackgroundJob\RefreshUserTokens;
use OCA\Astrolabe\Service\IdpTokenRefresher;
use OCA\Astrolabe\Service\McpTokenStorage;
use OCP\AppFramework\Utility\ITimeFactory;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\LoggerInterface;

/**
 * Unit tests for RefreshUserTokens background job.
 *
 * Tests proactive OAuth token refresh functionality.
 */
final class RefreshUserTokensTest extends TestCase {
	private ITimeFactory&MockObject $timeFactory;
	private McpTokenStorage&MockObject $tokenStorage;
	private IdpTokenRefresher&MockObject $tokenRefresher;
	private LoggerInterface&MockObject $logger;
	private RefreshUserTokens $job;

	protected function setUp(): void {
		parent::setUp();

		$this->timeFactory = $this->createMock(ITimeFactory::class);
		$this->tokenStorage = $this->createMock(McpTokenStorage::class);
		$this->tokenRefresher = $this->createMock(IdpTokenRefresher::class);
		$this->logger = $this->createMock(LoggerInterface::class);

		$this->job = new RefreshUserTokens(
			$this->timeFactory,
			$this->tokenStorage,
			$this->tokenRefresher,
			$this->logger
		);
	}

	// =========================================================================
	// Constructor Tests
	// =========================================================================

	public function testConstructorSetsInterval(): void {
		// Use reflection to access the protected interval property
		$reflection = new \ReflectionClass($this->job);
		$property = $reflection->getProperty('interval');
		$property->setAccessible(true);

		$this->assertEquals(900, $property->getValue($this->job));
	}

	// =========================================================================
	// run() Method Tests
	// =========================================================================

	public function testRunWithNoUsers(): void {
		$this->tokenStorage->method('getAllUsersWithTokens')
			->willReturn([]);

		$this->logger->expects($this->exactly(2))
			->method('info')
			->willReturnCallback(function (string $message) {
				static $callCount = 0;
				$callCount++;
				if ($callCount === 1) {
					$this->assertStringContainsString('Starting', $message);
				} else {
					$this->assertStringContainsString('refreshed=0, failed=0, skipped=0', $message);
				}
			});

		$this->logger->expects($this->once())
			->method('debug')
			->with($this->stringContains('Found 0 users'));

		// Call run() via reflection since it's protected
		$this->invokeRun();
	}

	public function testRunWithMultipleUsersAndMixedResults(): void {
		$this->tokenStorage->method('getAllUsersWithTokens')
			->willReturn(['alice', 'bob', 'charlie']);

		// Alice: token with plenty of time (skipped)
		// Bob: token near expiry with refresh token (refreshed)
		// Charlie: token near expiry without refresh token (failed)
		$this->tokenStorage->method('getUserToken')
			->willReturnCallback(function (string $userId) {
				$now = time();
				return match ($userId) {
					'alice' => [
						'access_token' => 'alice-token',
						'refresh_token' => 'alice-refresh',
						'expires_at' => $now + 3600, // 1 hour remaining (>50% of default lifetime)
						'issued_at' => $now,
					],
					'bob' => [
						'access_token' => 'bob-token',
						'refresh_token' => 'bob-refresh',
						'expires_at' => $now + 100, // ~100s remaining (<50% of default lifetime)
						'issued_at' => $now - 3500,
					],
					'charlie' => [
						'access_token' => 'charlie-token',
						// No refresh_token
						'expires_at' => $now + 100,
						'issued_at' => $now - 3500,
					],
					default => null,
				};
			});

		// Bob's refresh should succeed
		$this->tokenRefresher->method('refreshAccessToken')
			->with('bob-refresh')
			->willReturn([
				'access_token' => 'bob-new-token',
				'refresh_token' => 'bob-new-refresh',
				'expires_in' => 3600,
			]);

		$this->tokenStorage->expects($this->once())
			->method('storeUserToken')
			->with(
				'bob',
				'bob-new-token',
				'bob-new-refresh',
				$this->anything(),
				$this->anything()
			);

		$this->logger->expects($this->exactly(2))
			->method('info')
			->willReturnCallback(function (string $message) {
				static $callCount = 0;
				$callCount++;
				if ($callCount === 2) {
					$this->assertStringContainsString('refreshed=1, failed=1, skipped=1', $message);
				}
			});

		$this->invokeRun();
	}

	// =========================================================================
	// refreshUserTokenIfNeeded() Tests
	// =========================================================================

	public function testRefreshSkippedWhenTokenHasPlentyOfTime(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'valid-token',
				'refresh_token' => 'refresh-token',
				'expires_at' => $now + 3600, // 1 hour remaining
				'issued_at' => $now,
			]);

		$this->tokenRefresher->expects($this->never())
			->method('refreshAccessToken');

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('skipped', $result);
	}

	public function testRefreshTriggeredWhenTokenNearExpiry(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'expiring-token',
				'refresh_token' => 'refresh-token',
				'expires_at' => $now + 300, // 5 min remaining (< 50% of 3600s)
				'issued_at' => $now - 3300, // Issued 55 min ago
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->with('refresh-token')
			->willReturn([
				'access_token' => 'new-token',
				'refresh_token' => 'new-refresh-token',
				'expires_in' => 3600,
			]);

		$this->tokenStorage->expects($this->once())
			->method('storeUserToken');

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('refreshed', $result);
	}

	public function testRefreshFailsWhenNoRefreshToken(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'expiring-token',
				// No refresh_token
				'expires_at' => $now + 100,
				'issued_at' => $now - 3500,
			]);

		$this->logger->expects($this->once())
			->method('warning')
			->with($this->stringContains('no refresh token'));

		$this->tokenRefresher->expects($this->never())
			->method('refreshAccessToken');

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('failed', $result);
	}

	public function testRefreshFailsWhenRefresherReturnsNull(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'expiring-token',
				'refresh_token' => 'invalid-refresh',
				'expires_at' => $now + 100,
				'issued_at' => $now - 3500,
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->with('invalid-refresh')
			->willReturn(null);

		$this->logger->expects($this->once())
			->method('warning')
			->with($this->stringContains('Refresh returned null'));

		// Should NOT delete token - let on-demand refresh handle cleanup
		$this->tokenStorage->expects($this->never())
			->method('deleteUserToken');

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('failed', $result);
	}

	public function testRefreshUsesIssuedAtForLifetimeCalculation(): void {
		$now = time();
		// Token with custom lifetime: issued 50 min ago, expires in 10 min (total 60 min)
		// 10/60 = 16.7% remaining, which is < 50%, so should refresh
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'token',
				'refresh_token' => 'refresh',
				'expires_at' => $now + 600, // 10 min remaining
				'issued_at' => $now - 3000, // 50 min ago, total lifetime 60 min
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->willReturn([
				'access_token' => 'new-token',
				'refresh_token' => 'new-refresh',
				'expires_in' => 3600,
			]);

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('refreshed', $result);
	}

	public function testRefreshUsesDefaultLifetimeWhenNoIssuedAt(): void {
		$now = time();
		// Token without issued_at, uses default 3600s lifetime
		// 300s remaining / 3600s = 8.3% remaining, which is < 50%, so should refresh
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'token',
				'refresh_token' => 'refresh',
				'expires_at' => $now + 300, // 5 min remaining
				// No issued_at
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->willReturn([
				'access_token' => 'new-token',
				'refresh_token' => 'new-refresh',
				'expires_in' => 3600,
			]);

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('refreshed', $result);
	}

	public function testRefreshStoresNewTokenWithIssuedAt(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'old-token',
				'refresh_token' => 'old-refresh',
				'expires_at' => $now + 100,
				'issued_at' => $now - 3500,
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->willReturn([
				'access_token' => 'new-token',
				'refresh_token' => 'new-refresh',
				'expires_in' => 3600,
			]);

		// Verify storeUserToken is called with issued_at parameter
		$this->tokenStorage->expects($this->once())
			->method('storeUserToken')
			->with(
				'testuser',
				'new-token',
				'new-refresh',
				$this->greaterThan($now), // expires_at = now + 3600
				$this->greaterThanOrEqual($now) // issued_at = now
			);

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('refreshed', $result);
	}

	public function testRefreshKeepsOldRefreshTokenIfNotRotated(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'old-token',
				'refresh_token' => 'original-refresh',
				'expires_at' => $now + 100,
				'issued_at' => $now - 3500,
			]);

		// IdP returns new access token but no new refresh token (no rotation)
		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->willReturn([
				'access_token' => 'new-token',
				// No refresh_token in response
				'expires_in' => 3600,
			]);

		// Should use the original refresh token
		$this->tokenStorage->expects($this->once())
			->method('storeUserToken')
			->with(
				'testuser',
				'new-token',
				'original-refresh', // Original refresh token preserved
				$this->anything(),
				$this->anything()
			);

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('refreshed', $result);
	}

	public function testRefreshHandlesException(): void {
		$now = time();
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn([
				'access_token' => 'token',
				'refresh_token' => 'refresh',
				'expires_at' => $now + 100,
				'issued_at' => $now - 3500,
			]);

		$this->tokenRefresher->expects($this->once())
			->method('refreshAccessToken')
			->willThrowException(new \Exception('Network error'));

		$this->logger->expects($this->once())
			->method('error')
			->with($this->stringContains('Failed to refresh'));

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('failed', $result);
	}

	public function testRefreshSkippedWhenNoToken(): void {
		$this->tokenStorage->method('getUserToken')
			->with('testuser')
			->willReturn(null);

		$this->tokenRefresher->expects($this->never())
			->method('refreshAccessToken');

		$result = $this->invokeRefreshUserTokenIfNeeded('testuser');

		$this->assertEquals('skipped', $result);
	}

	// =========================================================================
	// Helper Methods
	// =========================================================================

	/**
	 * Invoke the protected run() method.
	 */
	private function invokeRun(): void {
		$reflection = new \ReflectionClass($this->job);
		$method = $reflection->getMethod('run');
		$method->setAccessible(true);
		$method->invoke($this->job, null);
	}

	/**
	 * Invoke the private refreshUserTokenIfNeeded() method.
	 */
	private function invokeRefreshUserTokenIfNeeded(string $userId): string {
		$reflection = new \ReflectionClass($this->job);
		$method = $reflection->getMethod('refreshUserTokenIfNeeded');
		$method->setAccessible(true);
		return $method->invoke($this->job, $userId);
	}
}
