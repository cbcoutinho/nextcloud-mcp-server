<?php

declare(strict_types=1);

namespace OCA\Astrolabe\AppInfo;

use OCA\Astrolabe\Listener\AstrolabeAdminSettingsListener;
use OCA\Astrolabe\Search\SemanticSearchProvider;
use OCA\Astrolabe\Settings\AstrolabeAdminSettings;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;
use OCP\Settings\Events\DeclarativeSettingsGetValueEvent;
use OCP\Settings\Events\DeclarativeSettingsSetValueEvent;

class Application extends App implements IBootstrap {
	public const APP_ID = 'astrolabe';

	/** @psalm-suppress PossiblyUnusedMethod */
	public function __construct() {
		parent::__construct(self::APP_ID);
	}

	public function register(IRegistrationContext $context): void {
		// Register unified search provider for semantic search
		$context->registerSearchProvider(SemanticSearchProvider::class);

		// Register declarative admin settings
		$context->registerDeclarativeSettings(AstrolabeAdminSettings::class);

		// Register event listeners for declarative settings
		$context->registerEventListener(
			DeclarativeSettingsGetValueEvent::class,
			AstrolabeAdminSettingsListener::class
		);
		$context->registerEventListener(
			DeclarativeSettingsSetValueEvent::class,
			AstrolabeAdminSettingsListener::class
		);
	}

	public function boot(IBootContext $context): void {
	}
}
