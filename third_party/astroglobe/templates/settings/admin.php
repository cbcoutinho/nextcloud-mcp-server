<?php
/**
 * Admin settings template for MCP Server UI.
 *
 * Displays server status, vector sync metrics, configuration,
 * and provides administrative controls.
 *
 * @var array $_ Template parameters
 * @var array $_['serverStatus'] Server status from API
 * @var array $_['vectorSyncStatus'] Vector sync metrics from API
 * @var string $_['serverUrl'] Configured MCP server URL
 * @var bool $_['apiKeyConfigured'] Whether API key is set in config.php
 * @var bool $_['vectorSyncEnabled'] Whether vector sync is enabled
 */

script('astroglobe', 'astroglobe-adminSettings');
style('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-admin-settings" class="section">
	<h2><?php p($l->t('MCP Server Administration')); ?></h2>

	<div class="mcp-settings-info">
		<p><?php p($l->t('Monitor and configure the Nextcloud MCP (Model Context Protocol) Server.')); ?></p>
	</div>

	<!-- Configuration Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Configuration')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Server URL')); ?></strong></td>
				<td>
					<?php if (!empty($_['serverUrl'])): ?>
						<code><?php p($_['serverUrl']); ?></code>
					<?php else: ?>
						<span class="error"><?php p($l->t('Not configured')); ?></span>
					<?php endif; ?>
				</td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('API Key')); ?></strong></td>
				<td>
					<?php if ($_['apiKeyConfigured']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Configured')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-warning">
							<span class="icon icon-alert"></span>
							<?php p($l->t('Not configured')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
		</table>

		<?php if (empty($_['serverUrl']) || !$_['apiKeyConfigured']): ?>
			<div class="notecard notecard-warning">
				<p><strong><?php p($l->t('Configuration Required')); ?></strong></p>
				<p><?php p($l->t('Add the following to your config.php:')); ?></p>
				<pre><code>'mcp_server_url' => 'http://localhost:8000',
'mcp_server_api_key' => 'your-secret-api-key',</code></pre>
				<p class="mcp-help-text">
					<a href="https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md" target="_blank">
						<?php p($l->t('See documentation for details')); ?>
					</a>
				</p>
			</div>
		<?php endif; ?>
	</div>

	<!-- Server Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Server Status')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Version')); ?></strong></td>
				<td><?php p($_['serverStatus']['version'] ?? 'Unknown'); ?></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Authentication Mode')); ?></strong></td>
				<td><code><?php p($_['serverStatus']['auth_mode'] ?? 'Unknown'); ?></code></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Management API Version')); ?></strong></td>
				<td><?php p($_['serverStatus']['management_api_version'] ?? 'Unknown'); ?></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Uptime')); ?></strong></td>
				<td>
					<?php if (isset($_['serverStatus']['uptime_seconds'])): ?>
						<?php
							$uptime = $_['serverStatus']['uptime_seconds'];
							$hours = floor($uptime / 3600);
							$minutes = floor(($uptime % 3600) / 60);
							p(sprintf('%d hours, %d minutes', $hours, $minutes));
						?>
					<?php else: ?>
						<?php p($l->t('Unknown')); ?>
					<?php endif; ?>
				</td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Vector Sync')); ?></strong></td>
				<td>
					<?php if ($_['vectorSyncEnabled']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Enabled')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-neutral">
							<?php p($l->t('Disabled')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
		</table>
	</div>

	<!-- Vector Sync Metrics -->
	<?php if ($_['vectorSyncEnabled'] && !isset($_['vectorSyncStatus']['error'])): ?>
		<div class="mcp-status-card" id="vector-sync-metrics">
			<h3><?php p($l->t('Vector Sync Metrics')); ?></h3>
			<table class="mcp-info-table">
				<tr>
					<td><strong><?php p($l->t('Status')); ?></strong></td>
					<td>
						<?php
							$status = $_['vectorSyncStatus']['status'] ?? 'unknown';
							$statusClass = $status === 'idle' ? 'success' : ($status === 'syncing' ? 'info' : 'neutral');
						?>
						<span class="badge badge-<?php p($statusClass); ?>">
							<?php p(ucfirst($status)); ?>
						</span>
					</td>
				</tr>
				<tr>
					<td><strong><?php p($l->t('Indexed Documents')); ?></strong></td>
					<td><?php p(number_format($_['vectorSyncStatus']['indexed_documents'] ?? 0)); ?></td>
				</tr>
				<tr>
					<td><strong><?php p($l->t('Pending Documents')); ?></strong></td>
					<td><?php p(number_format($_['vectorSyncStatus']['pending_documents'] ?? 0)); ?></td>
				</tr>
				<tr>
					<td><strong><?php p($l->t('Last Sync')); ?></strong></td>
					<td><?php p($_['vectorSyncStatus']['last_sync_time'] ?? 'Never'); ?></td>
				</tr>
				<tr>
					<td><strong><?php p($l->t('Processing Rate')); ?></strong></td>
					<td><?php p(sprintf('%.1f docs/sec', $_['vectorSyncStatus']['documents_per_second'] ?? 0)); ?></td>
				</tr>
				<tr>
					<td><strong><?php p($l->t('Errors (24h)')); ?></strong></td>
					<td>
						<?php
							$errors = $_['vectorSyncStatus']['errors_24h'] ?? 0;
							if ($errors > 0): ?>
								<span class="error"><?php p($errors); ?></span>
						<?php else: ?>
								<?php p('0'); ?>
						<?php endif; ?>
					</td>
				</tr>
			</table>

			<p class="mcp-help-text">
				<?php p($l->t('Metrics are updated in real-time. Refresh the page to see latest values.')); ?>
			</p>
		</div>
	<?php elseif ($_['vectorSyncEnabled']): ?>
		<div class="mcp-status-card mcp-error">
			<h3><?php p($l->t('Vector Sync Metrics')); ?></h3>
			<div class="notecard notecard-error">
				<p><?php p($l->t('Failed to retrieve vector sync status:')); ?></p>
				<p><code><?php p($_['vectorSyncStatus']['error'] ?? 'Unknown error'); ?></code></p>
			</div>
		</div>
	<?php endif; ?>

	<!-- Additional Features -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Features')); ?></h3>
		<ul class="mcp-feature-list">
			<li>
				<span class="icon icon-user"></span>
				<strong><?php p($l->t('User Settings')); ?></strong>
				<p><?php p($l->t('Users can manage their MCP server connections in Personal Settings.')); ?></p>
			</li>
			<?php if ($_['vectorSyncEnabled']): ?>
				<li>
					<span class="icon icon-search"></span>
					<strong><?php p($l->t('Vector Visualization')); ?></strong>
					<p><?php p($l->t('Interactive semantic search interface with 2D PCA visualization.')); ?></p>
				</li>
			<?php endif; ?>
			<li>
				<span class="icon icon-link"></span>
				<strong><?php p($l->t('MCP Protocol')); ?></strong>
				<p><?php p($l->t('Full support for MCP sampling, elicitation, and bidirectional streaming.')); ?></p>
			</li>
		</ul>
	</div>

	<!-- Documentation -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Documentation')); ?></h3>
		<ul class="mcp-links">
			<li>
				<a href="https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md" target="_blank">
					<?php p($l->t('Architecture Decision Record (ADR-018)')); ?>
				</a>
			</li>
			<li>
				<a href="https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/configuration.md" target="_blank">
					<?php p($l->t('Configuration Guide')); ?>
				</a>
			</li>
			<li>
				<a href="https://github.com/cbcoutinho/nextcloud-mcp-server" target="_blank">
					<?php p($l->t('GitHub Repository')); ?>
				</a>
			</li>
		</ul>
	</div>
</div>
