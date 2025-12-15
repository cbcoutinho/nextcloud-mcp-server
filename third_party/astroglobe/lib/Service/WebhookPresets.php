<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Service;

/**
 * Webhook preset configurations for common sync scenarios.
 *
 * Defines pre-configured webhook bundles that simplify webhook setup
 * for common use cases like Notes sync, Calendar sync, etc.
 */
class WebhookPresets {
	// File/Notes webhook events
	public const FILE_EVENT_CREATED = 'OCP\\Files\\Events\\Node\\NodeCreatedEvent';
	public const FILE_EVENT_WRITTEN = 'OCP\\Files\\Events\\Node\\NodeWrittenEvent';
	// Use BeforeNodeDeletedEvent instead of NodeDeletedEvent to get node.id
	// See: https://github.com/nextcloud/server/issues/56371
	public const FILE_EVENT_DELETED = 'OCP\\Files\\Events\\Node\\BeforeNodeDeletedEvent';

	// Calendar webhook events
	public const CALENDAR_EVENT_CREATED = 'OCP\\Calendar\\Events\\CalendarObjectCreatedEvent';
	public const CALENDAR_EVENT_UPDATED = 'OCP\\Calendar\\Events\\CalendarObjectUpdatedEvent';
	public const CALENDAR_EVENT_DELETED = 'OCP\\Calendar\\Events\\CalendarObjectDeletedEvent';

	// Tables webhook events (Nextcloud 30+)
	public const TABLES_EVENT_ROW_ADDED = 'OCA\\Tables\\Event\\RowAddedEvent';
	public const TABLES_EVENT_ROW_UPDATED = 'OCA\\Tables\\Event\\RowUpdatedEvent';
	public const TABLES_EVENT_ROW_DELETED = 'OCA\\Tables\\Event\\RowDeletedEvent';

	// Forms webhook events (Nextcloud 30+)
	public const FORMS_EVENT_FORM_SUBMITTED = 'OCA\\Forms\\Events\\FormSubmittedEvent';

	// NOTE: Deck and Contacts do NOT support webhooks
	// Their event classes do not implement IWebhookCompatibleEvent interface.
	// Alternative sync strategies:
	// - Deck: Use polling with ETag-based change detection
	// - Contacts: Use CardDAV sync-token mechanism for efficient syncing

	/**
	 * Get all available webhook presets.
	 *
	 * @return array<string, array{
	 *   name: string,
	 *   description: string,
	 *   app: string,
	 *   events: array<array{event: string, filter: array}>
	 * }>
	 */
	public static function getPresets(): array {
		return [
			'notes_sync' => [
				'name' => 'Notes Sync',
				'description' => 'Real-time synchronization for Notes app (create, update, delete)',
				'app' => 'notes',
				'events' => [
					[
						'event' => self::FILE_EVENT_CREATED,
						'filter' => ['event.node.path' => '/^\\/.*\\/files\\/Notes\\//'],
					],
					[
						'event' => self::FILE_EVENT_WRITTEN,
						'filter' => ['event.node.path' => '/^\\/.*\\/files\\/Notes\\//'],
					],
					[
						'event' => self::FILE_EVENT_DELETED,
						'filter' => ['event.node.path' => '/^\\/.*\\/files\\/Notes\\//'],
					],
				],
			],
			'calendar_sync' => [
				'name' => 'Calendar Sync',
				'description' => 'Real-time synchronization for Calendar events (create, update, delete)',
				'app' => 'calendar',
				'events' => [
					[
						'event' => self::CALENDAR_EVENT_CREATED,
						'filter' => [],
					],
					[
						'event' => self::CALENDAR_EVENT_UPDATED,
						'filter' => [],
					],
					[
						'event' => self::CALENDAR_EVENT_DELETED,
						'filter' => [],
					],
				],
			],
			'tables_sync' => [
				'name' => 'Tables Sync',
				'description' => 'Real-time synchronization for Tables rows (add, update, delete)',
				'app' => 'tables',
				'events' => [
					[
						'event' => self::TABLES_EVENT_ROW_ADDED,
						'filter' => [],
					],
					[
						'event' => self::TABLES_EVENT_ROW_UPDATED,
						'filter' => [],
					],
					[
						'event' => self::TABLES_EVENT_ROW_DELETED,
						'filter' => [],
					],
				],
			],
			'forms_sync' => [
				'name' => 'Forms Sync',
				'description' => 'Real-time synchronization for Forms submissions',
				'app' => 'forms',
				'events' => [
					[
						'event' => self::FORMS_EVENT_FORM_SUBMITTED,
						'filter' => [],
					],
				],
			],
			'files_sync' => [
				'name' => 'All Files Sync',
				'description' => 'Real-time synchronization for all file operations (create, update, delete)',
				'app' => 'files',
				'events' => [
					[
						'event' => self::FILE_EVENT_CREATED,
						'filter' => [],
					],
					[
						'event' => self::FILE_EVENT_WRITTEN,
						'filter' => [],
					],
					[
						'event' => self::FILE_EVENT_DELETED,
						'filter' => [],
					],
				],
			],
		];
	}

	/**
	 * Get a webhook preset by ID.
	 *
	 * @param string $presetId Preset identifier (e.g., "notes_sync", "calendar_sync")
	 * @return array|null Preset configuration or null if not found
	 */
	public static function getPreset(string $presetId): ?array {
		$presets = self::getPresets();
		return $presets[$presetId] ?? null;
	}

	/**
	 * Get list of event class names for a preset.
	 *
	 * @param string $presetId Preset identifier
	 * @return array<string> List of fully qualified event class names
	 */
	public static function getPresetEvents(string $presetId): array {
		$preset = self::getPreset($presetId);
		if ($preset === null) {
			return [];
		}

		return array_map(
			fn ($eventConfig) => $eventConfig['event'],
			$preset['events']
		);
	}

	/**
	 * Filter webhook presets to only show those for installed apps.
	 *
	 * @param array<string> $installedApps List of installed app names
	 * @return array<string, array> Filtered presets
	 */
	public static function filterPresetsByInstalledApps(array $installedApps): array {
		$filtered = [];
		foreach (self::getPresets() as $presetId => $preset) {
			$appName = $preset['app'];
			// "files" is always available (core functionality)
			if ($appName === 'files' || in_array($appName, $installedApps)) {
				$filtered[$presetId] = $preset;
			}
		}
		return $filtered;
	}
}
