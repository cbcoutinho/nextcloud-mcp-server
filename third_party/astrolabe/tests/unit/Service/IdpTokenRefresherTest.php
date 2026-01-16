<?php

declare(strict_types=1);

namespace Service;

use OCA\Astrolabe\Service\IdpTokenRefresher;
use OCA\Astrolabe\Service\McpServerClient;
use OCP\Http\Client\IClient;
use OCP\Http\Client\IClientService;
use OCP\IConfig;
use PHPUnit\Framework\MockObject\MockObject;
use PHPUnit\Framework\TestCase;
use Psr\Log\LoggerInterface;

/**
 * Unit tests for IdpTokenRefresher::getNextcloudBaseUrl().
 *
 * Tests the internal URL resolution logic for OAuth token refresh requests.
 */
final class IdpTokenRefresherTest extends TestCase {
	private IConfig&MockObject $config;
	private IClientService&MockObject $clientService;
	private LoggerInterface&MockObject $logger;
	private McpServerClient&MockObject $mcpServerClient;
	private IdpTokenRefresher $refresher;

	protected function setUp(): void {
		parent::setUp();

		$this->config = $this->createMock(IConfig::class);
		$this->clientService = $this->createMock(IClientService::class);
		$this->logger = $this->createMock(LoggerInterface::class);
		$this->mcpServerClient = $this->createMock(McpServerClient::class);

		$mockClient = $this->createMock(IClient::class);
		$this->clientService->method('newClient')->willReturn($mockClient);

		$this->refresher = new IdpTokenRefresher(
			$this->config,
			$this->clientService,
			$this->logger,
			$this->mcpServerClient
		);
	}

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
}
