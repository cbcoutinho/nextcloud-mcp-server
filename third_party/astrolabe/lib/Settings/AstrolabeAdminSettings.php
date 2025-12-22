<?php

declare(strict_types=1);

namespace OCA\Astrolabe\Settings;

use OCP\IL10N;
use OCP\Settings\DeclarativeSettingsTypes;
use OCP\Settings\IDeclarativeSettingsForm;

class AstrolabeAdminSettings implements IDeclarativeSettingsForm {
	public function __construct(
		private IL10N $l,
	) {
	}

	public function getSchema(): array {
		return [
			'id' => 'astrolabe-admin-settings',
			'priority' => 10,
			'section_type' => DeclarativeSettingsTypes::SECTION_TYPE_ADMIN,
			'section_id' => 'astrolabe',
			'storage_type' => DeclarativeSettingsTypes::STORAGE_TYPE_EXTERNAL,
			'title' => $this->l->t('MCP Server Configuration'),
			'description' => $this->l->t('Configure the connection to your Nextcloud MCP Server'),
			'doc_url' => 'https://github.com/cbcoutinho/nextcloud-mcp-server',

			'fields' => [
				[
					'id' => 'mcp_server_url',
					'title' => $this->l->t('MCP Server URL'),
					'description' => $this->l->t('The base URL of your Nextcloud MCP Server instance (e.g., http://localhost:8000)'),
					'type' => DeclarativeSettingsTypes::URL,
					'placeholder' => 'http://localhost:8000',
					'default' => '',
				],
				[
					'id' => 'mcp_server_api_key',
					'title' => $this->l->t('API Key'),
					'description' => $this->l->t('Authentication key for the MCP server (leave empty if not required)'),
					'type' => DeclarativeSettingsTypes::PASSWORD,
					'placeholder' => $this->l->t('Enter API key'),
					'default' => '',
				],
				[
					'id' => 'astrolabe_client_id',
					'title' => $this->l->t('OAuth Client ID'),
					'description' => $this->l->t('The OAuth client ID for Astrolabe (required for multi-user deployments)'),
					'type' => DeclarativeSettingsTypes::TEXT,
					'placeholder' => $this->l->t('Enter OAuth client ID'),
					'default' => '',
				],
				[
					'id' => 'astrolabe_client_secret',
					'title' => $this->l->t('OAuth Client Secret'),
					'description' => $this->l->t('Optional: Client secret for OAuth. If not set, PKCE will be used as fallback.'),
					'type' => DeclarativeSettingsTypes::PASSWORD,
					'placeholder' => $this->l->t('Enter client secret (optional)'),
					'default' => '',
				],
			],
		];
	}
}
