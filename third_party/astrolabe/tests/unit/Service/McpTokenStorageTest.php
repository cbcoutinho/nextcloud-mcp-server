<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Tests\Unit\Service;

use OCA\Astrolabe\Service\McpTokenStorage;
use OCP\DB\IResult;
use OCP\DB\QueryBuilder\IExpressionBuilder;
use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IConfig;
use OCP\IDBConnection;
use OCP\Security\ICrypto;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\LoggerInterface;

/**
 * Unit tests for McpTokenStorage.
 *
 * Tests OAuth token storage and app password functionality for multi-user basic auth.
 */
final class McpTokenStorageTest extends TestCase {
	private IConfig&MockObject $config;
	private ICrypto&MockObject $crypto;
	private IDBConnection&MockObject $db;
	private LoggerInterface&MockObject $logger;
	private McpTokenStorage $storage;

	protected function setUp(): void {
		parent::setUp();

		$this->config = $this->createMock(IConfig::class);
		$this->crypto = $this->createMock(ICrypto::class);
		$this->db = $this->createMock(IDBConnection::class);
		$this->logger = $this->createMock(LoggerInterface::class);

		$this->storage = new McpTokenStorage(
			$this->config,
			$this->crypto,
			$this->db,
			$this->logger
		);
	}

	// =========================================================================
	// OAuth Token Storage Tests
	// =========================================================================

	public function testStoreUserToken(): void {
		$userId = 'testuser';
		$accessToken = 'access-token-123';
		$refreshToken = 'refresh-token-456';
		$expiresAt = time() + 3600;

		$this->crypto->expects($this->once())
			->method('encrypt')
			->with($this->callback(function (string $json) use ($accessToken, $refreshToken, $expiresAt) {
				$data = json_decode($json, true);
				return $data['access_token'] === $accessToken
					&& $data['refresh_token'] === $refreshToken
					&& $data['expires_at'] === $expiresAt
					&& isset($data['issued_at']); // issued_at should be set (defaults to time())
			}))
			->willReturn('encrypted-data');

		$this->config->expects($this->once())
			->method('setUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens', 'encrypted-data');

		$this->storage->storeUserToken($userId, $accessToken, $refreshToken, $expiresAt);
	}

	public function testGetUserTokenReturnsTokenData(): void {
		$userId = 'testuser';
		$tokenData = [
			'access_token' => 'access-token-123',
			'refresh_token' => 'refresh-token-456',
			'expires_at' => time() + 3600,
		];

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens', '')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->with('encrypted-data')
			->willReturn(json_encode($tokenData));

		$result = $this->storage->getUserToken($userId);

		$this->assertEquals($tokenData, $result);
	}

	public function testGetUserTokenReturnsNullWhenNoTokenStored(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens', '')
			->willReturn('');

		$result = $this->storage->getUserToken($userId);

		$this->assertNull($result);
	}

	public function testGetUserTokenReturnsNullOnDecryptionFailure(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->willThrowException(new \Exception('Decryption failed'));

		$result = $this->storage->getUserToken($userId);

		$this->assertNull($result);
	}

	public function testDeleteUserToken(): void {
		$userId = 'testuser';

		$this->config->expects($this->once())
			->method('deleteUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens');

		$this->storage->deleteUserToken($userId);
	}

	// =========================================================================
	// Token Expiration Tests
	// =========================================================================

	public function testIsExpiredReturnsTrueWhenNoExpiresAt(): void {
		$token = ['access_token' => 'test'];

		$this->assertTrue($this->storage->isExpired($token));
	}

	public function testIsExpiredReturnsTrueWhenExpired(): void {
		$token = [
			'access_token' => 'test',
			'expires_at' => time() - 100, // Expired 100 seconds ago
		];

		$this->assertTrue($this->storage->isExpired($token));
	}

	public function testIsExpiredReturnsTrueWhenAboutToExpire(): void {
		$token = [
			'access_token' => 'test',
			'expires_at' => time() + 30, // Expires in 30 seconds (within 60s buffer)
		];

		$this->assertTrue($this->storage->isExpired($token));
	}

	public function testIsExpiredReturnsFalseWhenValid(): void {
		$token = [
			'access_token' => 'test',
			'expires_at' => time() + 3600, // Expires in 1 hour
		];

		$this->assertFalse($this->storage->isExpired($token));
	}

	// =========================================================================
	// getAccessToken with Refresh Callback Tests
	// =========================================================================

	public function testGetAccessTokenReturnsNullWhenNoToken(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturn('');

		$result = $this->storage->getAccessToken($userId);

		$this->assertNull($result);
	}

	public function testGetAccessTokenReturnsTokenWhenValid(): void {
		$userId = 'testuser';
		$tokenData = [
			'access_token' => 'valid-access-token',
			'refresh_token' => 'refresh-token',
			'expires_at' => time() + 3600, // Valid for 1 hour
		];

		$this->config->method('getUserValue')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->willReturn(json_encode($tokenData));

		$result = $this->storage->getAccessToken($userId);

		$this->assertEquals('valid-access-token', $result);
	}

	public function testGetAccessTokenRefreshesExpiredToken(): void {
		$userId = 'testuser';
		$expiredTokenData = [
			'access_token' => 'expired-access-token',
			'refresh_token' => 'old-refresh-token',
			'expires_at' => time() - 100, // Expired
		];

		$newTokenData = [
			'access_token' => 'new-access-token',
			'refresh_token' => 'new-refresh-token',
			'expires_in' => 3600,
		];

		// First call returns expired token, subsequent calls for storing new token
		$this->config->method('getUserValue')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->willReturn(json_encode($expiredTokenData));

		// Encrypt is called when storing the new token
		$this->crypto->method('encrypt')
			->willReturn('new-encrypted-data');

		$this->config->expects($this->once())
			->method('setUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens', 'new-encrypted-data');

		// Refresh callback
		$refreshCallback = function (string $refreshToken) use ($newTokenData) {
			$this->assertEquals('old-refresh-token', $refreshToken);
			return $newTokenData;
		};

		$result = $this->storage->getAccessToken($userId, $refreshCallback);

		$this->assertEquals('new-access-token', $result);
	}

	public function testGetAccessTokenReturnsNullWhenRefreshFailsAndDeletesToken(): void {
		$userId = 'testuser';
		$expiredTokenData = [
			'access_token' => 'expired-access-token',
			'refresh_token' => 'old-refresh-token',
			'expires_at' => time() - 100, // Expired
		];

		$this->config->method('getUserValue')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->willReturn(json_encode($expiredTokenData));

		// Expect stale token to be deleted when refresh fails
		$this->config->expects($this->once())
			->method('deleteUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens');

		// Refresh callback returns null (failure)
		$refreshCallback = fn (string $refreshToken) => null;

		$result = $this->storage->getAccessToken($userId, $refreshCallback);

		$this->assertNull($result);
	}

	public function testGetAccessTokenReturnsNullWhenExpiredAndNoCallbackAndDeletesToken(): void {
		$userId = 'testuser';
		$expiredTokenData = [
			'access_token' => 'expired-access-token',
			'refresh_token' => 'old-refresh-token',
			'expires_at' => time() - 100, // Expired
		];

		$this->config->method('getUserValue')
			->willReturn('encrypted-data');

		$this->crypto->method('decrypt')
			->willReturn(json_encode($expiredTokenData));

		// Expect stale token to be deleted when expired with no callback
		$this->config->expects($this->once())
			->method('deleteUserValue')
			->with($userId, 'astrolabe', 'oauth_tokens');

		// No refresh callback provided
		$result = $this->storage->getAccessToken($userId, null);

		$this->assertNull($result);
	}

	// =========================================================================
	// App Password Storage Tests (Multi-User Basic Auth)
	// =========================================================================

	public function testStoreBackgroundSyncPassword(): void {
		$userId = 'testuser';
		$appPassword = 'app-password-secret';

		$this->crypto->expects($this->once())
			->method('encrypt')
			->with($appPassword)
			->willReturn('encrypted-password');

		// Expect three setUserValue calls: password, type, timestamp
		$this->config->expects($this->exactly(3))
			->method('setUserValue')
			->willReturnCallback(function ($uid, $app, $key, $value) use ($userId) {
				$this->assertEquals($userId, $uid);
				$this->assertEquals('astrolabe', $app);
				$this->assertContains($key, [
					'background_sync_password',
					'background_sync_type',
					'background_sync_provisioned_at'
				]);
				return null;
			});

		$this->storage->storeBackgroundSyncPassword($userId, $appPassword);
	}

	public function testGetBackgroundSyncPasswordReturnsPassword(): void {
		$userId = 'testuser';
		$appPassword = 'app-password-secret';

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'background_sync_password', '')
			->willReturn('encrypted-password');

		$this->crypto->method('decrypt')
			->with('encrypted-password')
			->willReturn($appPassword);

		$result = $this->storage->getBackgroundSyncPassword($userId);

		$this->assertEquals($appPassword, $result);
	}

	public function testGetBackgroundSyncPasswordReturnsNullWhenNotSet(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'background_sync_password', '')
			->willReturn('');

		$result = $this->storage->getBackgroundSyncPassword($userId);

		$this->assertNull($result);
	}

	public function testGetBackgroundSyncPasswordReturnsNullOnDecryptionFailure(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturn('encrypted-password');

		$this->crypto->method('decrypt')
			->willThrowException(new \Exception('Decryption failed'));

		$result = $this->storage->getBackgroundSyncPassword($userId);

		$this->assertNull($result);
	}

	public function testDeleteBackgroundSyncPassword(): void {
		$userId = 'testuser';

		// Expect three deleteUserValue calls
		$this->config->expects($this->exactly(3))
			->method('deleteUserValue')
			->willReturnCallback(function ($uid, $app, $key) use ($userId) {
				$this->assertEquals($userId, $uid);
				$this->assertEquals('astrolabe', $app);
				$this->assertContains($key, [
					'background_sync_password',
					'background_sync_type',
					'background_sync_provisioned_at'
				]);
				return null;
			});

		$this->storage->deleteBackgroundSyncPassword($userId);
	}

	// =========================================================================
	// Background Sync Access Check Tests
	// =========================================================================

	public function testHasBackgroundSyncAccessReturnsTrueWithOAuthToken(): void {
		$userId = 'testuser';
		$tokenData = [
			'access_token' => 'access-token',
			'refresh_token' => 'refresh-token',
			'expires_at' => time() + 3600,
		];

		$this->config->method('getUserValue')
			->willReturnCallback(function ($uid, $app, $key, $default) use ($tokenData) {
				if ($key === 'oauth_tokens') {
					return 'encrypted-oauth-data';
				}
				return $default;
			});

		$this->crypto->method('decrypt')
			->willReturn(json_encode($tokenData));

		$result = $this->storage->hasBackgroundSyncAccess($userId);

		$this->assertTrue($result);
	}

	public function testHasBackgroundSyncAccessReturnsTrueWithAppPassword(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturnCallback(function ($uid, $app, $key, $default) {
				if ($key === 'oauth_tokens') {
					return ''; // No OAuth tokens
				}
				if ($key === 'background_sync_password') {
					return 'encrypted-password';
				}
				return $default;
			});

		$this->crypto->method('decrypt')
			->willReturn('decrypted-app-password');

		$result = $this->storage->hasBackgroundSyncAccess($userId);

		$this->assertTrue($result);
	}

	public function testHasBackgroundSyncAccessReturnsFalseWithNeither(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturn(''); // No tokens or passwords

		$result = $this->storage->hasBackgroundSyncAccess($userId);

		$this->assertFalse($result);
	}

	// =========================================================================
	// Background Sync Type Tests
	// =========================================================================

	public function testGetBackgroundSyncTypeReturnsAppPassword(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturnCallback(function ($uid, $app, $key, $default) {
				if ($key === 'background_sync_type') {
					return 'app_password';
				}
				return $default;
			});

		$result = $this->storage->getBackgroundSyncType($userId);

		$this->assertEquals('app_password', $result);
	}

	public function testGetBackgroundSyncTypeFallsBackToOAuth(): void {
		$userId = 'testuser';
		$tokenData = [
			'access_token' => 'access-token',
			'refresh_token' => 'refresh-token',
			'expires_at' => time() + 3600,
		];

		$this->config->method('getUserValue')
			->willReturnCallback(function ($uid, $app, $key, $default) {
				if ($key === 'background_sync_type') {
					return ''; // Type not explicitly set
				}
				if ($key === 'oauth_tokens') {
					return 'encrypted-oauth-data';
				}
				return $default;
			});

		$this->crypto->method('decrypt')
			->willReturn(json_encode($tokenData));

		$result = $this->storage->getBackgroundSyncType($userId);

		$this->assertEquals('oauth', $result);
	}

	public function testGetBackgroundSyncTypeReturnsNullWhenNotProvisioned(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->willReturn('');

		$result = $this->storage->getBackgroundSyncType($userId);

		$this->assertNull($result);
	}

	// =========================================================================
	// Background Sync Provisioned Timestamp Tests
	// =========================================================================

	public function testGetBackgroundSyncProvisionedAtReturnsTimestamp(): void {
		$userId = 'testuser';
		$timestamp = time();

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'background_sync_provisioned_at', '')
			->willReturn((string)$timestamp);

		$result = $this->storage->getBackgroundSyncProvisionedAt($userId);

		$this->assertEquals($timestamp, $result);
	}

	public function testGetBackgroundSyncProvisionedAtReturnsNullWhenNotSet(): void {
		$userId = 'testuser';

		$this->config->method('getUserValue')
			->with($userId, 'astrolabe', 'background_sync_provisioned_at', '')
			->willReturn('');

		$result = $this->storage->getBackgroundSyncProvisionedAt($userId);

		$this->assertNull($result);
	}

	// =========================================================================
	// getAllUsersWithTokens Tests
	// =========================================================================

	public function testGetAllUsersWithTokensReturnsUserIds(): void {
		$qb = $this->createMock(IQueryBuilder::class);
		$expr = $this->createMock(IExpressionBuilder::class);
		$result = $this->createMock(IResult::class);

		// Chain builder methods
		$qb->method('select')->willReturnSelf();
		$qb->method('from')->willReturnSelf();
		$qb->method('where')->willReturnSelf();
		$qb->method('andWhere')->willReturnSelf();
		$qb->method('expr')->willReturn($expr);
		$qb->method('createNamedParameter')->willReturnArgument(0);
		$qb->method('executeQuery')->willReturn($result);

		// Mock expression builder
		$expr->method('eq')->willReturn('mocked_condition');

		// Mock result set with multiple users
		$result->method('fetch')->willReturnOnConsecutiveCalls(
			['userid' => 'admin'],
			['userid' => 'alice'],
			['userid' => 'bob'],
			false  // End of results
		);
		$result->expects($this->once())->method('closeCursor');

		$this->db->method('getQueryBuilder')->willReturn($qb);

		$userIds = $this->storage->getAllUsersWithTokens();

		$this->assertEquals(['admin', 'alice', 'bob'], $userIds);
	}

	public function testGetAllUsersWithTokensReturnsEmptyArrayWhenNoTokens(): void {
		$qb = $this->createMock(IQueryBuilder::class);
		$expr = $this->createMock(IExpressionBuilder::class);
		$result = $this->createMock(IResult::class);

		// Chain builder methods
		$qb->method('select')->willReturnSelf();
		$qb->method('from')->willReturnSelf();
		$qb->method('where')->willReturnSelf();
		$qb->method('andWhere')->willReturnSelf();
		$qb->method('expr')->willReturn($expr);
		$qb->method('createNamedParameter')->willReturnArgument(0);
		$qb->method('executeQuery')->willReturn($result);

		// Mock expression builder
		$expr->method('eq')->willReturn('mocked_condition');

		// Mock empty result set
		$result->method('fetch')->willReturn(false);
		$result->expects($this->once())->method('closeCursor');

		$this->db->method('getQueryBuilder')->willReturn($qb);

		$userIds = $this->storage->getAllUsersWithTokens();

		$this->assertEquals([], $userIds);
	}
}
