<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Listener;

use OCA\Astrolabe\AppInfo\Application;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\IConfig;
use OCP\Settings\Events\DeclarativeSettingsGetValueEvent;
use OCP\Settings\Events\DeclarativeSettingsSetValueEvent;
use Psr\Log\LoggerInterface;

/**
 * @template-implements IEventListener<DeclarativeSettingsGetValueEvent|DeclarativeSettingsSetValueEvent>
 */
class AstrolabeAdminSettingsListener implements IEventListener {
	public function __construct(
		private IConfig $config,
		private LoggerInterface $logger,
	) {
	}

	public function handle(Event $event): void {
		if (!$event instanceof DeclarativeSettingsGetValueEvent && !$event instanceof DeclarativeSettingsSetValueEvent) {
			return;
		}

		if ($event->getApp() !== Application::APP_ID) {
			return;
		}

		if ($event->getFormId() !== 'astrolabe-admin-settings') {
			return;
		}

		if ($event instanceof DeclarativeSettingsGetValueEvent) {
			$this->handleGetValue($event);
		} elseif ($event instanceof DeclarativeSettingsSetValueEvent) {
			$this->handleSetValue($event);
		}
	}

	private function handleGetValue(DeclarativeSettingsGetValueEvent $event): void {
		$fieldId = $event->getFieldId();

		// Map field IDs to system config keys
		$value = match($fieldId) {
			'mcp_server_url' => $this->config->getSystemValue('mcp_server_url', ''),
			'mcp_server_api_key' => '****', // Never leak the API key on read
			'astrolabe_client_id' => $this->config->getSystemValue('astrolabe_client_id', ''),
			'astrolabe_client_secret' => '****', // Never leak the secret on read
			default => null,
		};

		if ($value !== null) {
			$event->setValue($value);
		}
	}

	private function handleSetValue(DeclarativeSettingsSetValueEvent $event): void {
		$fieldId = $event->getFieldId();
		$value = $event->getValue();

		// Only save if value is not empty (allow clearing by setting to empty string)
		// For password fields, if the value is '****', don't update (user didn't change it)
		if ($fieldId === 'mcp_server_api_key' && $value === '****') {
			$event->stopPropagation();
			return;
		}
		if ($fieldId === 'astrolabe_client_secret' && $value === '****') {
			$event->stopPropagation();
			return;
		}

		try {
			match($fieldId) {
				'mcp_server_url' => $this->config->setSystemValue('mcp_server_url', (string)$value),
				'mcp_server_api_key' => $this->config->setSystemValue('mcp_server_api_key', (string)$value),
				'astrolabe_client_id' => $this->config->setSystemValue('astrolabe_client_id', (string)$value),
				'astrolabe_client_secret' => $this->config->setSystemValue('astrolabe_client_secret', (string)$value),
				default => null,
			};

			$this->logger->info('Astrolabe admin setting updated', [
				'field' => $fieldId,
				'app' => Application::APP_ID,
			]);
		} catch (\Exception $e) {
			$this->logger->error('Failed to update Astrolabe admin setting', [
				'field' => $fieldId,
				'error' => $e->getMessage(),
				'app' => Application::APP_ID,
			]);
			throw $e;
		}

		$event->stopPropagation();
	}
}
