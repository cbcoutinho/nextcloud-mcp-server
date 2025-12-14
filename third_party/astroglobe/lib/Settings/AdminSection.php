<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Settings;

use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

/**
 * Admin settings section for MCP Server.
 *
 * Creates a dedicated section in admin settings for MCP-related configuration.
 */
class AdminSection implements IIconSection {
	private $l;
	private $urlGenerator;

	public function __construct(IL10N $l, IURLGenerator $urlGenerator) {
		$this->l = $l;
		$this->urlGenerator = $urlGenerator;
	}

	/**
	 * @return string The section ID
	 */
	public function getID(): string {
		return 'mcp';
	}

	/**
	 * @return string The translated section name
	 */
	public function getName(): string {
		return $this->l->t('MCP Server');
	}

	/**
	 * @return int Priority (lower = higher up in list)
	 */
	public function getPriority(): int {
		return 80;
	}

	/**
	 * @return string Section icon (SVG or image URL)
	 */
	public function getIcon(): string {
		return $this->urlGenerator->imagePath('astroglobe', 'app.svg');
	}
}
