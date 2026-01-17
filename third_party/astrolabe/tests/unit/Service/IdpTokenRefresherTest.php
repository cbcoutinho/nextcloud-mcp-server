<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Tests\Unit\Service;

use OCA\Astrolabe\Service\IdpTokenRefresher;
use OCA\Astrolabe\Service\McpServerClient;
use OCP\Http\Client\IClient;
use OCP\Http\Client\IClientService;
use OCP\Http\Client\IResponse;
use OCP\IConfig;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\LoggerInterface;

/**
 * Unit tests for IdpTokenRefresher.
 *
 * Tests the internal URL resolution logic and token refresh flows.
 */
final class IdpTokenRefresherTest extends TestCase {
	private IConfig&MockObject $config;
	private IClientService&MockObject $clientService;
	private IClient&MockObject $httpClient;
	private LoggerInterface&MockObject $logger;
	private McpServerClient&MockObject $mcpServerClient;
	private IdpTokenRefresher $refresher;

	protected function setUp(): void {
		parent::setUp();

		$this->config = $this->createMock(IConfig::class);
		$this->clientService = $this->createMock(IClientService::class);
		$this->httpClient = $this->createMock(IClient::class);
		$this->logger = $this->createMock(LoggerInterface::class);
		$this->mcpServerClient = $this->createMock(McpServerClient::class);

		$this->clientService->method('newClient')->willReturn($this->httpClient);

		$this->refresher = new IdpTokenRefresher(
			$this->config,
			$this->clientService,
			$this->logger,
			$this->mcpServerClient
		);
	}

	// =========================================================================
	// getNextcloudBaseUrl() tests
	// =========================================================================

	/**
	 * @dataProvider provideBaseUrlTestCases
	 */
	public function testGetNextcloudBaseUrl(string $configValue, string $expected): void {
		$this->config->method('getSystemValue')
			->with('astrolabe_internal_url', '')
			->willReturn($configValue);

		// Use reflection to test private method
		$reflection = new \ReflectionClass($this->refresher);
		$method = $reflection->getMethod('getNextcloudBaseUrl');
		$method->setAccessible(true);

		$result = $method->invoke($this->refresher);

		$this->assertEquals($expected, $result);
	}

	/**
	 * Provides test cases for getNextcloudBaseUrl().
	 *
	 * @return array<string, array{string, string}>
	 */
	public static function provideBaseUrlTestCases(): array {
		return [
			'default - no config' => ['', 'http://localhost'],
			'custom internal url' => ['http://web:8080', 'http://web:8080'],
			'custom url with trailing slash' => ['http://web:8080/', 'http://web:8080'],
			'kubernetes service' => ['http://nextcloud.default.svc:80', 'http://nextcloud.default.svc:80'],
			'https internal url' => ['https://internal.example.com', 'https://internal.example.com'],
		];
	}

	// =========================================================================
	// refreshAccessToken() tests
	// =========================================================================

	public function testRefreshAccessTokenFailsWithoutClientSecret(): void {
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', ''],
			]);

		$this->logger->expects($this->once())
			->method('warning')
			->with($this->stringContains('no client secret configured'));

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenFailsWithoutMcpServerUrl(): void {
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', ''],
			]);

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('Token refresh failed'),
				$this->callback(fn ($ctx) => str_contains($ctx['error'], 'MCP server URL not configured'))
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenWithInternalNextcloudOidc(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
				['astrolabe_internal_url', '', ''],
			]);

		$this->mcpServerClient->method('getClientId')
			->willReturn('test-client-id');

		// Mock MCP server status response (no external IdP configured)
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn(json_encode([
				'version' => '1.0.0',
				'auth_mode' => 'multi_user_oauth',
				// No 'oidc.discovery_url' = use internal Nextcloud OIDC
			]));

		// Mock token endpoint response
		$tokenResponse = $this->createMock(IResponse::class);
		$tokenResponse->method('getBody')
			->willReturn(json_encode([
				'access_token' => 'new-access-token',
				'refresh_token' => 'new-refresh-token',
				'expires_in' => 3600,
				'token_type' => 'Bearer',
			]));

		// Setup HTTP client to return appropriate responses
		$this->httpClient->method('get')
			->with('http://mcp-server:8000/api/v1/status')
			->willReturn($statusResponse);

		$this->httpClient->method('post')
			->with(
				'http://localhost/apps/oidc/token',
				$this->callback(function ($options) {
					// Verify the POST body contains expected parameters
					$body = $options['body'] ?? '';
					return str_contains($body, 'grant_type=refresh_token')
						&& str_contains($body, 'client_id=test-client-id')
						&& str_contains($body, 'client_secret=test-secret')
						&& str_contains($body, 'refresh_token=test-refresh-token');
				})
			)
			->willReturn($tokenResponse);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNotNull($result);
		$this->assertEquals('new-access-token', $result['access_token']);
		$this->assertEquals('new-refresh-token', $result['refresh_token']);
		$this->assertEquals(3600, $result['expires_in']);
	}

	public function testRefreshAccessTokenWithExternalIdp(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
			]);

		$this->mcpServerClient->method('getClientId')
			->willReturn('test-client-id');

		// Mock MCP server status response (external IdP configured)
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn(json_encode([
				'version' => '1.0.0',
				'auth_mode' => 'multi_user_oauth',
				'oidc' => [
					'discovery_url' => 'https://keycloak.example.com/realms/test/.well-known/openid-configuration',
				],
			]));

		// Mock OIDC discovery response
		$discoveryResponse = $this->createMock(IResponse::class);
		$discoveryResponse->method('getBody')
			->willReturn(json_encode([
				'issuer' => 'https://keycloak.example.com/realms/test',
				'token_endpoint' => 'https://keycloak.example.com/realms/test/protocol/openid-connect/token',
				'authorization_endpoint' => 'https://keycloak.example.com/realms/test/protocol/openid-connect/auth',
			]));

		// Mock token endpoint response
		$tokenResponse = $this->createMock(IResponse::class);
		$tokenResponse->method('getBody')
			->willReturn(json_encode([
				'access_token' => 'keycloak-access-token',
				'refresh_token' => 'keycloak-refresh-token',
				'expires_in' => 300,
				'token_type' => 'Bearer',
			]));

		// Setup HTTP client calls in order
		$this->httpClient->method('get')
			->willReturnCallback(function ($url) use ($statusResponse, $discoveryResponse) {
				if (str_contains($url, 'status')) {
					return $statusResponse;
				}
				if (str_contains($url, '.well-known/openid-configuration')) {
					return $discoveryResponse;
				}
				throw new \Exception("Unexpected URL: $url");
			});

		$this->httpClient->method('post')
			->with(
				'https://keycloak.example.com/realms/test/protocol/openid-connect/token',
				$this->anything()
			)
			->willReturn($tokenResponse);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNotNull($result);
		$this->assertEquals('keycloak-access-token', $result['access_token']);
		$this->assertEquals('keycloak-refresh-token', $result['refresh_token']);
		$this->assertEquals(300, $result['expires_in']);
	}

	public function testRefreshAccessTokenFailsOnMissingRefreshTokenInResponse(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
				['astrolabe_internal_url', '', ''],
			]);

		$this->mcpServerClient->method('getClientId')
			->willReturn('test-client-id');

		// Mock MCP server status response
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn(json_encode(['version' => '1.0.0']));

		// Mock token response WITHOUT refresh_token (token rotation failure)
		$tokenResponse = $this->createMock(IResponse::class);
		$tokenResponse->method('getBody')
			->willReturn(json_encode([
				'access_token' => 'new-access-token',
				// Missing refresh_token!
				'expires_in' => 3600,
			]));

		$this->httpClient->method('get')->willReturn($statusResponse);
		$this->httpClient->method('post')->willReturn($tokenResponse);

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('No refresh token in response'),
				$this->anything()
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenHandlesHttpException(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
			]);

		// HTTP client throws exception
		$this->httpClient->method('get')
			->willThrowException(new \Exception('Connection refused'));

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('Token refresh failed'),
				$this->callback(fn ($ctx) => str_contains($ctx['error'], 'Connection refused'))
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenHandlesInvalidStatusResponse(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
			]);

		// Mock invalid JSON response
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn('not valid json');

		$this->httpClient->method('get')->willReturn($statusResponse);

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('Token refresh failed'),
				$this->callback(fn ($ctx) => str_contains($ctx['error'], 'Invalid status response'))
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenHandlesInvalidDiscoveryResponse(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
			]);

		$this->mcpServerClient->method('getClientId')
			->willReturn('test-client-id');

		// Mock MCP server status response with external IdP
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn(json_encode([
				'oidc' => [
					'discovery_url' => 'https://keycloak.example.com/.well-known/openid-configuration',
				],
			]));

		// Mock invalid discovery response (missing token_endpoint)
		$discoveryResponse = $this->createMock(IResponse::class);
		$discoveryResponse->method('getBody')
			->willReturn(json_encode([
				'issuer' => 'https://keycloak.example.com',
				// Missing token_endpoint!
			]));

		$this->httpClient->method('get')
			->willReturnCallback(function ($url) use ($statusResponse, $discoveryResponse) {
				if (str_contains($url, 'status')) {
					return $statusResponse;
				}
				return $discoveryResponse;
			});

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('Token refresh failed'),
				$this->callback(fn ($ctx) => str_contains($ctx['error'], 'Invalid OIDC discovery response'))
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}

	public function testRefreshAccessTokenHandlesInvalidTokenResponse(): void {
		// Setup config
		$this->config->method('getSystemValue')
			->willReturnMap([
				['astrolabe_client_secret', '', 'test-secret'],
				['mcp_server_url', '', 'http://mcp-server:8000'],
				['astrolabe_internal_url', '', ''],
			]);

		$this->mcpServerClient->method('getClientId')
			->willReturn('test-client-id');

		// Mock MCP server status response
		$statusResponse = $this->createMock(IResponse::class);
		$statusResponse->method('getBody')
			->willReturn(json_encode(['version' => '1.0.0']));

		// Mock token response without access_token
		$tokenResponse = $this->createMock(IResponse::class);
		$tokenResponse->method('getBody')
			->willReturn(json_encode([
				'error' => 'invalid_grant',
				'error_description' => 'Refresh token expired',
			]));

		$this->httpClient->method('get')->willReturn($statusResponse);
		$this->httpClient->method('post')->willReturn($tokenResponse);

		$this->logger->expects($this->once())
			->method('error')
			->with(
				$this->stringContains('Token refresh failed'),
				$this->callback(fn ($ctx) => str_contains($ctx['error'], 'Invalid token response'))
			);

		$result = $this->refresher->refreshAccessToken('test-refresh-token');

		$this->assertNull($result);
	}
}
