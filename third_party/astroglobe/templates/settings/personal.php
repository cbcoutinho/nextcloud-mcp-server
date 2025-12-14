<?php
/**
 * Personal settings template for Astroglobe.
 *
 * Displays semantic search status, background indexing access,
 * and provides controls for managing content indexing.
 *
 * @var array $_ Template parameters
 * @var string $_['userId'] Current user ID
 * @var array $_['serverStatus'] Server status from API
 * @var array $_['session'] User session details from API
 * @var bool $_['vectorSyncEnabled'] Whether vector sync is enabled
 * @var bool $_['backgroundAccessGranted'] Whether user has granted background access
 * @var string $_['serverUrl'] Astroglobe service URL
 */

// Get URL generator from Nextcloud's service container
$urlGenerator = \OC::$server->getURLGenerator();

script('astroglobe', 'astroglobe-personalSettings');
style('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-personal-settings" class="section">
	<h2><?php p($l->t('Astroglobe')); ?></h2>

	<div class="mcp-settings-info">
		<p><?php p($l->t('AI-powered semantic search across your Nextcloud content. Find documents by meaning, not just keywords.')); ?></p>
	</div>

	<!-- Service Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Service Status')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Service URL')); ?></strong></td>
				<td><code><?php p($_['serverUrl']); ?></code></td>
			</tr>
			<tr>
				<td><strong><?php p($l->t('Version')); ?></strong></td>
				<td><?php p($_['serverStatus']['version'] ?? 'Unknown'); ?></td>
			</tr>
		</table>
	</div>

	<!-- Indexing Status -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Content Indexing')); ?></h3>
		<table class="mcp-info-table">
			<tr>
				<td><strong><?php p($l->t('Status')); ?></strong></td>
				<td>
					<?php if ($_['backgroundAccessGranted']): ?>
						<span class="badge badge-success">
							<span class="icon icon-checkmark-white"></span>
							<?php p($l->t('Active')); ?>
						</span>
					<?php else: ?>
						<span class="badge badge-neutral">
							<?php p($l->t('Not Enabled')); ?>
						</span>
					<?php endif; ?>
				</td>
			</tr>
		</table>

		<?php if (!$_['backgroundAccessGranted']): ?>
			<div class="mcp-grant-section">
				<p class="mcp-help-text">
					<?php p($l->t('Enable background indexing to use semantic search. Your Notes, Files, Calendar events, and Deck cards will be indexed so you can search by meaning.')); ?>
				</p>
				<a href="<?php p($_['serverUrl']); ?>/oauth/login?next=<?php p(urlencode($urlGenerator->getAbsoluteURL($urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])))); ?>" class="button primary" id="mcp-grant-button">
					<span class="icon icon-confirm"></span>
					<?php p($l->t('Enable Semantic Search')); ?>
				</a>
			</div>
		<?php endif; ?>

		<?php if ($_['backgroundAccessGranted'] && isset($_['session']['background_access_details'])): ?>
			<div class="mcp-background-details">
				<h4><?php p($l->t('Indexing Details')); ?></h4>
				<table class="mcp-info-table">
					<tr>
						<td><strong><?php p($l->t('Enabled Since')); ?></strong></td>
						<td><?php p($_['session']['background_access_details']['provisioned_at'] ?? 'N/A'); ?></td>
					</tr>
					<tr>
						<td><strong><?php p($l->t('Indexed Content')); ?></strong></td>
						<td><code style="font-size: 11px;"><?php p($_['session']['background_access_details']['scopes'] ?? 'N/A'); ?></code></td>
					</tr>
				</table>

				<div class="mcp-revoke-section">
					<form method="post" action="<?php p($urlGenerator->linkToRoute('astroglobe.api.revokeAccess')); ?>" id="mcp-revoke-form">
						<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
						<button type="submit" class="button warning" id="mcp-revoke-button">
							<span class="icon icon-delete"></span>
							<?php p($l->t('Disable Indexing')); ?>
						</button>
						<p class="mcp-help-text">
							<?php p($l->t('This will stop background indexing and remove your content from semantic search. You can re-enable it at any time.')); ?>
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

	<!-- Semantic Search Features -->
	<?php if ($_['vectorSyncEnabled']): ?>
		<div class="mcp-status-card">
			<h3><?php p($l->t('Search Your Content')); ?></h3>
			<p><?php p($l->t('Use natural language to search across your Notes, Files, Calendar, and Deck cards. Ask questions like "meeting notes from last week" or "recipes with chicken".')); ?></p>
			<a href="<?php p($urlGenerator->linkToRoute('astroglobe.page.index')); ?>" class="button primary">
				<span class="icon icon-search"></span>
				<?php p($l->t('Open Astroglobe')); ?>
			</a>
		</div>
	<?php else: ?>
		<div class="mcp-status-card mcp-disabled">
			<h3><?php p($l->t('Semantic Search')); ?></h3>
			<p class="mcp-help-text">
				<?php p($l->t('Semantic search is not enabled on this server. Contact your administrator to enable this feature.')); ?>
			</p>
		</div>
	<?php endif; ?>

	<!-- Connection Management -->
	<div class="mcp-status-card">
		<h3><?php p($l->t('Manage Connection')); ?></h3>
		<p><?php p($l->t('You are connected to the Astroglobe service.')); ?></p>

		<div class="mcp-revoke-section">
			<form method="post" action="<?php p($urlGenerator->linkToRoute('astroglobe.oauth.disconnect')); ?>" id="mcp-disconnect-form">
				<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
				<button type="submit" class="button warning" id="mcp-disconnect-button">
					<span class="icon icon-close"></span>
					<?php p($l->t('Disconnect')); ?>
				</button>
				<p class="mcp-help-text">
					<?php p($l->t('This will disconnect from the Astroglobe service. You will need to re-authorize to use semantic search features.')); ?>
				</p>
			</form>
		</div>
	</div>
</div>

<script>
	// Confirm disable and disconnect actions
	document.addEventListener('DOMContentLoaded', function() {
		const revokeForm = document.getElementById('mcp-revoke-form');
		if (revokeForm) {
			revokeForm.addEventListener('submit', function(e) {
				if (!confirm('<?php p($l->t('Are you sure you want to disable indexing? Your content will be removed from semantic search.')); ?>')) {
					e.preventDefault();
				}
			});
		}

		const disconnectForm = document.getElementById('mcp-disconnect-form');
		if (disconnectForm) {
			disconnectForm.addEventListener('submit', function(e) {
				if (!confirm('<?php p($l->t('Are you sure you want to disconnect from Astroglobe? You will need to re-authorize to use semantic search.')); ?>')) {
					e.preventDefault();
				}
			});
		}
	});
</script>
