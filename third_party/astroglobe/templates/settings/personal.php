<?php
/**
 * Personal settings template for MCP Server UI.
 *
 * Displays user session information, background access status,
 * and provides controls for managing MCP server integration.
 *
 * @var array $_ Template parameters
 * @var string $_['userId'] Current user ID
 * @var array $_['serverStatus'] Server status from API
 * @var array $_['session'] User session details from API
 * @var bool $_['vectorSyncEnabled'] Whether vector sync is enabled
 * @var bool $_['backgroundAccessGranted'] Whether user has granted background access
 * @var string $_['serverUrl'] MCP server URL
 */

// Get URL generator from Nextcloud's service container
$urlGenerator = \OC::$server->getURLGenerator();

script('astroglobe', 'astroglobe-personalSettings');
style('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-personal-settings" class="section">
	<h2><?php p($l->t('MCP Server')); ?></h2>

	<div class="mcp-settings-info">
		<p><?php p($l->t('Manage your connection to the Nextcloud MCP (Model Context Protocol) Server.')); ?></p>
	</div>

	<!-- Server Connection Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Server Connection')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Server URL')); ?></strong></td>
				<td><code><?php p($_['serverUrl']); ?></code></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Server Version')); ?></strong></td>
				<td><?php p($_['serverStatus']['version'] ?? 'Unknown'); ?></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Auth Mode')); ?></strong></td>
				<td><?php p($_['serverStatus']['auth_mode'] ?? 'Unknown'); ?></td>
			</tr>
		</table>
	</div>

	<!-- Session Information -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Session Information')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('User ID')); ?></strong></td>
				<td><code><?php p($_['userId']); ?></code></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Background Access')); ?></strong></td>
				<td>
					<?php if ($_['backgroundAccessGranted']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Granted')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-neutral">
							<?php p($l->t('Not Granted')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
		</table>

		<?php if (!$_['backgroundAccessGranted']): ?>
			<div class="mcp-grant-section">
				<p class="mcp-help-text">
					<?php p($l->t('Background access allows the MCP server to sync your documents in the background for semantic search. Without it, your documents will not be indexed.')); ?>
				</p>
				<a href="<?php p($_['serverUrl']); ?>/oauth/login?next=<?php p(urlencode($urlGenerator->getAbsoluteURL($urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])))); ?>" class="button primary" id="mcp-grant-button">
					<span class="icon icon-confirm"></span>
					<?php p($l->t('Grant Background Access')); ?>
				</a>
			</div>
		<?php endif; ?>

		<?php if ($_['backgroundAccessGranted'] && isset($_['session']['background_access_details'])): ?>
			<div class="mcp-background-details">
				<h4><?php p($l->t('Background Access Details')); ?></h4>
				<table class="mcp-info-table">
					<tr>
						<td><strong><?php p($l->t('Flow Type')); ?></strong></td>
						<td><?php p($_['session']['background_access_details']['flow_type'] ?? 'N/A'); ?></td>
					</tr>
					<tr>
						<td><strong><?php p($l->t('Provisioned At')); ?></strong></td>
						<td><?php p($_['session']['background_access_details']['provisioned_at'] ?? 'N/A'); ?></td>
					</tr>
					<tr>
						<td><strong><?php p($l->t('Token Audience')); ?></strong></td>
						<td><?php p($_['session']['background_access_details']['token_audience'] ?? 'N/A'); ?></td>
					</tr>
					<tr>
						<td><strong><?php p($l->t('Scopes')); ?></strong></td>
						<td><code style="font-size: 11px;"><?php p($_['session']['background_access_details']['scopes'] ?? 'N/A'); ?></code></td>
					</tr>
				</table>

				<div class="mcp-revoke-section">
					<form method="post" action="<?php p($urlGenerator->linkToRoute('astroglobe.api.revokeAccess')); ?>" id="mcp-revoke-form">
						<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
						<button type="submit" class="button warning" id="mcp-revoke-button">
							<span class="icon icon-delete"></span>
							<?php p($l->t('Revoke Background Access')); ?>
						</button>
						<p class="mcp-help-text">
							<?php p($l->t('This will delete the refresh token and prevent background operations from running on your behalf.')); ?>
						</p>
					</form>
				</div>
			</div>
		<?php endif; ?>
	</div>

	<!-- Identity Provider Profile -->
	<?php if (isset($_['session']['idp_profile'])): ?>
		<div class="mcp-status-card">
			<h3><?php p($l->t('Identity Provider Profile')); ?></h3>
			<table class="mcp-info-table">
				<?php foreach ($_['session']['idp_profile'] as $key => $value): ?>
					<tr>
						<td><strong><?php p(ucfirst(str_replace('_', ' ', $key))); ?></strong></td>
						<td>
							<?php if (is_array($value)): ?>
								<?php p(implode(', ', $value)); ?>
							<?php else: ?>
								<?php p((string)$value); ?>
							<?php endif; ?>
						</td>
					</tr>
				<?php endforeach; ?>
			</table>
		</div>
	<?php endif; ?>

	<!-- Vector Sync Features -->
	<?php if ($_['vectorSyncEnabled']): ?>
		<div class="mcp-status-card">
			<h3><?php p($l->t('Semantic Search')); ?></h3>
			<p><?php p($l->t('Search your indexed content using semantic similarity. Find documents by meaning, not just keywords.')); ?></p>
			<a href="<?php p($urlGenerator->linkToRoute('astroglobe.page.index')); ?>" class="button primary">
				<span class="icon icon-search"></span>
				<?php p($l->t('Open MCP Server UI')); ?>
			</a>
		</div>
	<?php else: ?>
		<div class="mcp-status-card mcp-disabled">
			<h3><?php p($l->t('Semantic Search')); ?></h3>
			<p class="mcp-help-text">
				<?php p($l->t('Vector sync is not enabled on the MCP server. Contact your administrator to enable this feature.')); ?>
			</p>
		</div>
	<?php endif; ?>

	<!-- OAuth Connection Management -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Connection Management')); ?></h3>
		<p><?php p($l->t('You are currently connected to the MCP server via OAuth.')); ?></p>

		<div class="mcp-revoke-section">
			<form method="post" action="<?php p($urlGenerator->linkToRoute('astroglobe.oauth.disconnect')); ?>" id="mcp-disconnect-form">
				<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
				<button type="submit" class="button warning" id="mcp-disconnect-button">
					<span class="icon icon-close"></span>
					<?php p($l->t('Disconnect from MCP Server')); ?>
				</button>
				<p class="mcp-help-text">
					<?php p($l->t('This will remove your OAuth connection to the MCP server. You will need to authorize access again to use MCP features.')); ?>
				</p>
			</form>
		</div>
	</div>
</div>

<script>
	// Confirm revoke and disconnect actions
	document.addEventListener('DOMContentLoaded', function() {
		const revokeForm = document.getElementById('mcp-revoke-form');
		if (revokeForm) {
			revokeForm.addEventListener('submit', function(e) {
				if (!confirm('<?php p($l->t('Are you sure you want to revoke background access? This action cannot be undone.')); ?>')) {
					e.preventDefault();
				}
			});
		}

		const disconnectForm = document.getElementById('mcp-disconnect-form');
		if (disconnectForm) {
			disconnectForm.addEventListener('submit', function(e) {
				if (!confirm('<?php p($l->t('Are you sure you want to disconnect from the MCP server? You will need to authorize access again.')); ?>')) {
					e.preventDefault();
				}
			});
		}
	});
</script>
