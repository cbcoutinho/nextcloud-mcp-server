<?php

declare(strict_types=1);

namespace OCA\Astrolabe\AppInfo;

use OCA\Astrolabe\Search\SemanticSearchProvider;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;

class Application extends App implements IBootstrap {
	public const APP_ID = 'astrolabe';

	/** @psalm-suppress PossiblyUnusedMethod */
	public function __construct() {
		parent::__construct(self::APP_ID);
	}

	public function register(IRegistrationContext $context): void {
		// Register unified search provider for semantic search
		$context->registerSearchProvider(SemanticSearchProvider::class);
	}

	public function boot(IBootContext $context): void {
	}
}
