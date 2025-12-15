<?php
/**
 * Admin settings template for Astroglobe.
 *
 * Displays semantic search service status, indexing metrics, configuration,
 * and provides administrative controls.
 *
 * @var array $_ Template parameters
 * @var array $_['serverStatus'] Server status from API
 * @var array $_['vectorSyncStatus'] Vector sync metrics from API
 * @var string $_['serverUrl'] Configured Astroglobe service URL
 * @var bool $_['apiKeyConfigured'] Whether API key is set in config.php
 * @var bool $_['vectorSyncEnabled'] Whether vector sync is enabled
 */

script('astroglobe', 'astroglobe-adminSettings');
style('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-admin-settings" class="section">
	<h2><?php p($l->t('Astroglobe Administration')); ?></h2>

	<div class="mcp-settings-info">
		<p><?php p($l->t('Monitor and configure the semantic search service for your Nextcloud instance.')); ?></p>
	</div>

	<!-- Configuration Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Configuration')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Service URL')); ?></strong></td>
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
			<tr>
				<td><strong><?php p($l->t('OAuth Client ID')); ?></strong></td>
				<td>
					<?php if ($_['clientIdConfigured']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Configured')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-warning">
							<span class="icon icon-alert"></span>
							<?php p($l->t('Not configured - OAuth will not work')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('OAuth Client Secret')); ?></strong></td>
				<td>
					<?php if ($_['clientSecretConfigured']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Configured')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-info">
							<?php p($l->t('Optional - Uses PKCE fallback')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
		</table>

		<?php if (empty($_['serverUrl']) || !$_['apiKeyConfigured'] || !$_['clientIdConfigured']): ?>
			<div class="notecard notecard-warning">
				<p><strong><?php p($l->t('Configuration Required')); ?></strong></p>
				<p><?php p($l->t('Add the following to your config.php:')); ?></p>
				<pre><code>'mcp_server_url' => 'http://localhost:8000',
'mcp_server_api_key' => 'your-secret-api-key',
'astroglobe_client_id' => 'your-oauth-client-id',</code></pre>
				<p class="mcp-help-text">
					<a href="https://github.com/cbcoutinho/nextcloud-mcp-server" target="_blank">
						<?php p($l->t('See documentation for details')); ?>
					</a>
				</p>
			</div>
		<?php endif; ?>

		<?php if (!$_['clientSecretConfigured']): ?>
			<div class="notecard notecard-info">
				<p><strong><?php p($l->t('Optional: Confidential OAuth Client')); ?></strong></p>
				<p><?php p($l->t('To use refresh tokens for long-lived sessions, generate a client secret:')); ?></p>
				<pre><code>openssl rand -hex 32</code></pre>
				<p><?php p($l->t('Then add it to your config.php:')); ?></p>
				<pre><code>'astroglobe_client_secret' => 'your-generated-secret',</code></pre>
				<p class="mcp-help-text">
					<?php p($l->t('Without a client secret, the system will use PKCE (public client) authentication. Both methods work, but confidential clients provide better security for long-lived sessions.')); ?>
				</p>
			</div>
		<?php endif; ?>
	</div>

	<!-- Service Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Service Status')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Version')); ?></strong></td>
				<td><?php p($_['serverStatus']['version'] ?? 'Unknown'); ?></td>
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
				<td><strong><?php p($l->t('Semantic Search')); ?></strong></td>
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

	<!-- Indexing Metrics -->
	<?php if ($_['vectorSyncEnabled'] && !isset($_['vectorSyncStatus']['error'])): ?>
		<div class="mcp-status-card" id="vector-sync-metrics">
			<h3><?php p($l->t('Indexing Metrics')); ?></h3>
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
			<h3><?php p($l->t('Indexing Metrics')); ?></h3>
			<div class="notecard notecard-error">
				<p><?php p($l->t('Failed to retrieve indexing status:')); ?></p>
				<p><code><?php p($_['vectorSyncStatus']['error'] ?? 'Unknown error'); ?></code></p>
			</div>
		</div>
	<?php endif; ?>

	<!-- Search Settings -->
	<?php if ($_['vectorSyncEnabled']): ?>
		<div class="mcp-status-card" id="search-settings">
			<h3><?php p($l->t('AI Search Provider Settings')); ?></h3>
			<p class="mcp-settings-description">
				<?php p($l->t('Configure the default search parameters for the AI Search provider in Nextcloud unified search.')); ?>
			</p>

			<form id="astroglobe-search-settings-form" class="mcp-settings-form">
				<div class="mcp-form-group">
					<label for="search-algorithm"><?php p($l->t('Search Algorithm')); ?></label>
					<select id="search-algorithm" name="algorithm" class="mcp-select">
						<option value="hybrid" <?php if ($_['searchSettings']['algorithm'] === 'hybrid') {
							echo 'selected';
						} ?>>
							<?php p($l->t('Hybrid (Recommended)')); ?>
						</option>
						<option value="semantic" <?php if ($_['searchSettings']['algorithm'] === 'semantic') {
							echo 'selected';
						} ?>>
							<?php p($l->t('Semantic Only')); ?>
						</option>
						<option value="bm25" <?php if ($_['searchSettings']['algorithm'] === 'bm25') {
							echo 'selected';
						} ?>>
							<?php p($l->t('Keyword (BM25) Only')); ?>
						</option>
					</select>
					<p class="mcp-help-text">
						<?php p($l->t('Hybrid combines semantic understanding with keyword matching. Semantic finds conceptually similar content. BM25 matches exact keywords.')); ?>
					</p>
				</div>

				<div class="mcp-form-group">
					<label for="search-fusion"><?php p($l->t('Fusion Method')); ?></label>
					<select id="search-fusion" name="fusion" class="mcp-select">
						<option value="rrf" <?php if ($_['searchSettings']['fusion'] === 'rrf') {
							echo 'selected';
						} ?>>
							<?php p($l->t('RRF - Reciprocal Rank Fusion (Recommended)')); ?>
						</option>
						<option value="dbsf" <?php if ($_['searchSettings']['fusion'] === 'dbsf') {
							echo 'selected';
						} ?>>
							<?php p($l->t('DBSF - Distribution-Based Score Fusion')); ?>
						</option>
					</select>
					<p class="mcp-help-text">
						<?php p($l->t('Only applies to hybrid search. RRF balances results well for most queries. DBSF may work better when keyword matches are over/under-weighted.')); ?>
					</p>
				</div>

				<div class="mcp-form-group">
					<label for="search-score-threshold">
						<?php p($l->t('Minimum Score Threshold')); ?>:
						<span id="score-threshold-value"><?php p($_['searchSettings']['scoreThreshold']); ?>%</span>
					</label>
					<input type="range"
						   id="search-score-threshold"
						   name="scoreThreshold"
						   min="0"
						   max="100"
						   step="5"
						   value="<?php p($_['searchSettings']['scoreThreshold']); ?>"
						   class="mcp-range" />
					<p class="mcp-help-text">
						<?php p($l->t('Filter out results below this relevance score. Set to 0 to show all results.')); ?>
					</p>
				</div>

				<div class="mcp-form-group">
					<label for="search-limit"><?php p($l->t('Maximum Results')); ?></label>
					<input type="number"
						   id="search-limit"
						   name="limit"
						   min="5"
						   max="100"
						   step="5"
						   value="<?php p($_['searchSettings']['limit']); ?>"
						   class="mcp-input" />
					<p class="mcp-help-text">
						<?php p($l->t('Maximum number of results to return per search query (5-100).')); ?>
					</p>
				</div>

				<div class="mcp-form-actions">
					<button type="submit" class="primary">
						<?php p($l->t('Save Settings')); ?>
					</button>
					<span id="search-settings-status" class="mcp-status-message"></span>
				</div>
			</form>
		</div>

	<?php endif; ?>

	<!-- Webhook Management -->
	<?php if ($_['vectorSyncEnabled']): ?>
		<div class="mcp-status-card" id="webhook-presets">
			<h3><?php p($l->t('Webhook Management')); ?></h3>
			<p class="mcp-settings-description">
				<?php p($l->t('Configure real-time synchronization for Nextcloud apps using webhooks. Webhooks provide instant updates to the MCP server when content changes.')); ?>
			</p>

			<div id="webhook-presets-container">
				<div class="mcp-loading">
					<?php p($l->t('Loading webhook presets...')); ?>
				</div>
			</div>

			<div class="notecard notecard-info">
				<p><strong><?php p($l->t('How Webhooks Work')); ?></strong></p>
				<ul>
					<li><?php p($l->t('Enable a preset to register webhooks for that app with the MCP server')); ?></li>
					<li><?php p($l->t('When content changes in Nextcloud, webhooks notify the MCP server instantly')); ?></li>
					<li><?php p($l->t('The MCP server updates its vector index in real-time for semantic search')); ?></li>
					<li><?php p($l->t('Disable a preset to stop receiving updates for that app')); ?></li>
				</ul>
			</div>

			<div class="notecard notecard-warning">
				<p><strong><?php p($l->t('Requirements')); ?></strong></p>
				<ul>
					<li><?php p($l->t('The webhook_listeners app must be installed and enabled in Nextcloud')); ?></li>
					<li><?php p($l->t('The MCP server must be reachable from your Nextcloud instance')); ?></li>
					<li><?php p($l->t('You must have authorized Astroglobe with the MCP server (see Personal Settings)')); ?></li>
				</ul>
			</div>
		</div>
	<?php endif; ?>

	<!-- Capabilities -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Capabilities')); ?></h3>
		<ul class="mcp-feature-list">
			<li>
				<span class="icon icon-search"></span>
				<strong><?php p($l->t('Semantic Search')); ?></strong>
				<p><?php p($l->t('Search by meaning across Notes, Files, Calendar, and Deck using natural language queries.')); ?></p>
			</li>
			<?php if ($_['vectorSyncEnabled']): ?>
				<li>
					<span class="icon icon-category-monitoring"></span>
					<strong><?php p($l->t('Vector Visualization')); ?></strong>
					<p><?php p($l->t('Explore content relationships in an interactive 2D visualization.')); ?></p>
				</li>
			<?php endif; ?>
			<li>
				<span class="icon icon-user"></span>
				<strong><?php p($l->t('Per-User Indexing')); ?></strong>
				<p><?php p($l->t('Users control their own content indexing via Personal Settings.')); ?></p>
			</li>
			<li>
				<span class="icon icon-toggle"></span>
				<strong><?php p($l->t('Hybrid Search')); ?></strong>
				<p><?php p($l->t('Combines semantic understanding with keyword matching for optimal results.')); ?></p>
			</li>
		</ul>
	</div>

	<!-- Documentation -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Documentation')); ?></h3>
		<ul class="mcp-links">
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
