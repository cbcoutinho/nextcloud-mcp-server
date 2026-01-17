<?php
/**
 * Personal settings template for Astrolabe.
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
 * @var string $_['serverUrl'] Astrolabe service URL
 */

// Get URL generator from Nextcloud's service container
$urlGenerator = \OC::$server->getURLGenerator();

script('astrolabe', 'astrolabe-personalSettings');
style('astrolabe', 'astrolabe-main');  // All CSS bundled into main
?>

<div class="section">
	<h2><?php p($l->t('Astrolabe')); ?></h2>
	<p><?php p($l->t('AI-powered semantic search across your Nextcloud content. Find documents by meaning, not just keywords.')); ?></p>
</div>

<div class="section">
	<h2><?php p($l->t('Service Status')); ?></h2>
		<table class="mcp-info-table">
			<tr>
				<td><?php p($l->t('Service URL')); ?></td>
				<td><code><?php p($_['serverUrl']); ?></code></td>
			</tr>
			<tr>
				<td><?php p($l->t('Version')); ?></td>
				<td><?php p($_['serverStatus']['version'] ?? 'Unknown'); ?></td>
			</tr>
		</table>
</div>

<div class="section">
	<h2><?php p($l->t('Background Sync Access')); ?></h2>

		<?php
		// Determine if hybrid mode (multi_user_basic + app passwords)
		// In hybrid mode, user needs BOTH OAuth AND app password to be "fully configured"
		$isHybridMode = ($_['authMode'] ?? '') === 'multi_user_basic' && !empty($_['supportsAppPasswords']);
$hasOAuthToken = !empty($_['hasOAuthToken']);
$hasBackgroundAccess = !empty($_['hasBackgroundAccess']) || !empty($_['backgroundAccessGranted']);

// In hybrid mode: both credentials required; otherwise just background access
$isFullyConfigured = $isHybridMode ? ($hasOAuthToken && $hasBackgroundAccess) : $hasBackgroundAccess;
?>
		<?php if ($isFullyConfigured): ?>
			<!-- Already configured -->
			<div class="mcp-background-status">
				<p>
					<span class="badge badge-success">
						<span class="icon icon-checkmark-white"></span>
						<?php p($l->t('Active')); ?>
					</span>
				</p>
				<table class="mcp-info-table">
					<tr>
						<td><?php p($l->t('Credential Type')); ?></td>
						<td>
							<?php if ($_['backgroundSyncType'] === 'app_password'): ?>
								<?php p($l->t('App Password')); ?>
							<?php else: ?>
								<?php p($l->t('OAuth Refresh Token')); ?>
							<?php endif; ?>
						</td>
					</tr>
					<?php if ($_['backgroundSyncProvisionedAt']): ?>
						<tr>
							<td><?php p($l->t('Provisioned At')); ?></td>
							<td><?php p(date('c', $_['backgroundSyncProvisionedAt'])); ?></td>
						</tr>
					<?php elseif (isset($_['session']['background_access_details']['provisioned_at'])): ?>
						<tr>
							<td><?php p($l->t('Provisioned At')); ?></td>
							<td><?php p($_['session']['background_access_details']['provisioned_at']); ?></td>
						</tr>
					<?php endif; ?>
					<?php if (isset($_['session']['background_access_details']['scopes'])): ?>
						<tr>
							<td><?php p($l->t('Indexed Content')); ?></td>
							<td><code><?php p($_['session']['background_access_details']['scopes'] ?? 'N/A'); ?></code></td>
						</tr>
					<?php endif; ?>
				</table>

				<div class="mcp-revoke-section">
					<?php if ($_['backgroundSyncType'] === 'app_password'): ?>
						<form method="post" action="<?php p($urlGenerator->linkToRoute('astrolabe.credentials.deleteCredentials')); ?>" id="mcp-revoke-background-form">
							<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
							<button type="submit" class="button warning" id="mcp-revoke-background-button">
								<span class="icon icon-delete"></span>
								<?php p($l->t('Revoke Access')); ?>
							</button>
							<p class="mcp-help-text">
								<?php p($l->t('This will revoke background sync access. The MCP server will no longer be able to access your Nextcloud data for background operations.')); ?>
							</p>
						</form>
					<?php else: ?>
						<form method="post" action="<?php p($urlGenerator->linkToRoute('astrolabe.api.revokeAccess')); ?>" id="mcp-revoke-form">
							<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
							<button type="submit" class="button warning" id="mcp-revoke-button">
								<span class="icon icon-delete"></span>
								<?php p($l->t('Disable Indexing')); ?>
							</button>
							<p class="mcp-help-text">
								<?php p($l->t('This will stop background indexing and remove your content from semantic search. You can re-enable it at any time.')); ?>
							</p>
						</form>
					<?php endif; ?>
				</div>
			</div>
		<?php else: ?>
			<!-- Not configured - show provisioning options -->
			<?php if ($isHybridMode): ?>
				<!-- Hybrid mode: User needs BOTH OAuth AND app password -->
				<p class="mcp-help-text">
					<?php p($l->t('To use semantic search, you need to complete two setup steps:')); ?>
				</p>

				<!-- Step 1: OAuth Authorization (for Astrolabe→MCP API calls) -->
				<div class="mcp-grant-section">
					<h4>
						<?php if (!empty($_['hasOAuthToken'])): ?>
							<span class="badge badge-success"><span class="icon icon-checkmark-white"></span> <?php p($l->t('Complete')); ?></span>
						<?php else: ?>
							<span class="badge badge-warning"><?php p($l->t('Required')); ?></span>
						<?php endif; ?>
						<?php p($l->t('Step 1: Authorize Search Access')); ?>
					</h4>
					<p class="mcp-help-text">
						<?php p($l->t('Authorize Astrolabe to perform searches on your behalf.')); ?>
					</p>
					<?php if (empty($_['hasOAuthToken'])): ?>
						<a href="<?php p($_['oauthUrl']); ?>" class="button primary">
							<span class="icon icon-confirm"></span>
							<?php p($l->t('Authorize')); ?>
						</a>
					<?php else: ?>
						<p><span class="icon icon-checkmark"></span> <?php p($l->t('Search access authorized.')); ?></p>
					<?php endif; ?>
				</div>

				<!-- Step 2: App Password (for MCP→Nextcloud background sync) -->
				<div class="mcp-grant-section">
					<h4>
						<?php if (!empty($_['hasBackgroundAccess'])): ?>
							<span class="badge badge-success"><span class="icon icon-checkmark-white"></span> <?php p($l->t('Complete')); ?></span>
						<?php else: ?>
							<span class="badge badge-warning"><?php p($l->t('Required')); ?></span>
						<?php endif; ?>
						<?php p($l->t('Step 2: Enable Background Indexing')); ?>
					</h4>
					<p class="mcp-help-text">
						<?php p($l->t('Provide an app password to allow background indexing of your content.')); ?>
					</p>
					<?php if (empty($_['hasBackgroundAccess'])): ?>
						<div class="mcp-app-password-steps">
							<p>
								<a href="<?php p($urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'security'])); ?>" target="_blank">
									<?php p($l->t('Generate app password in Security settings')); ?>
								</a>
							</p>

							<form method="post" action="<?php p($urlGenerator->linkToRoute('astrolabe.credentials.storeAppPassword')); ?>" id="mcp-app-password-form">
								<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
								<div class="mcp-input-group">
									<input type="password" name="appPassword" id="mcp-app-password-input"
										   placeholder="xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
										   pattern="[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}"
										   required>
									<button type="submit" class="button primary" id="mcp-save-app-password-button">
										<span class="icon icon-checkmark"></span>
										<?php p($l->t('Save')); ?>
									</button>
								</div>
								<p class="mcp-help-text">
									<?php p($l->t('The app password will be validated and securely encrypted before storage.')); ?>
								</p>
							</form>
						</div>
					<?php else: ?>
						<p><span class="icon icon-checkmark"></span> <?php p($l->t('Background indexing enabled.')); ?></p>
					<?php endif; ?>
				</div>

			<?php else: ?>
				<!-- Standard OAuth or BasicAuth mode -->
				<p class="mcp-help-text">
					<?php p($l->t('Enable background sync to allow the MCP server to access your Nextcloud data for background operations like content indexing.')); ?>
				</p>

				<div class="mcp-grant-section">
					<h4><?php p($l->t('Option 1: OAuth Refresh Token (Recommended for Future)')); ?></h4>
					<p class="mcp-help-text">
						<?php p($l->t('When Nextcloud fully supports OAuth for app APIs. Currently waiting for upstream PR to merge.')); ?>
					</p>
					<a href="<?php p($_['oauthUrl']); ?>" class="button">
						<span class="icon icon-confirm"></span>
						<?php p($l->t('Authorize via OAuth')); ?>
					</a>
				</div>

				<div class="mcp-grant-section">
					<h4><?php p($l->t('Option 2: App Password (Works Today - Recommended)')); ?></h4>
					<p class="mcp-help-text">
						<?php p($l->t('Generate an app password in Security settings and provide it below. This is the recommended interim solution.')); ?>
					</p>

					<div class="mcp-app-password-steps">
						<p><strong><?php p($l->t('Step 1:')); ?></strong>
							<a href="<?php p($urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'security'])); ?>" target="_blank">
								<?php p($l->t('Generate app password in Security settings')); ?>
							</a>
						</p>

						<p><strong><?php p($l->t('Step 2:')); ?></strong> <?php p($l->t('Enter the app password below:')); ?></p>

						<form method="post" action="<?php p($urlGenerator->linkToRoute('astrolabe.credentials.storeAppPassword')); ?>" id="mcp-app-password-form">
							<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
							<div class="mcp-input-group">
								<input type="password" name="appPassword" id="mcp-app-password-input"
									   placeholder="xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
									   pattern="[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}"
									   required>
								<button type="submit" class="button primary" id="mcp-save-app-password-button">
									<span class="icon icon-checkmark"></span>
									<?php p($l->t('Save')); ?>
								</button>
							</div>
							<p class="mcp-help-text">
								<?php p($l->t('The app password will be validated and securely encrypted before storage.')); ?>
							</p>
						</form>
					</div>
				</div>
			<?php endif; ?>
		<?php endif; ?>
</div>

<?php if (isset($_['session']['idp_profile'])): ?>
<div class="section">
	<h2><?php p($l->t('Identity Provider Profile')); ?></h2>
			<table class="mcp-info-table">
				<?php foreach ($_['session']['idp_profile'] as $key => $value): ?>
					<tr>
						<td><?php p(ucfirst(str_replace('_', ' ', $key))); ?></td>
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

<?php if ($_['vectorSyncEnabled']): ?>
<div class="section">
	<h2><?php p($l->t('Search Your Content')); ?></h2>
			<p><?php p($l->t('Use natural language to search across your Notes, Files, Calendar, and Deck cards. Ask questions like "meeting notes from last week" or "recipes with chicken".')); ?></p>
			<a href="<?php p($urlGenerator->linkToRoute('astrolabe.page.index')); ?>" class="button primary">
				<span class="icon icon-search"></span>
				<?php p($l->t('Open Astrolabe')); ?>
			</a>
</div>
<?php else: ?>
<div class="section">
	<h2><?php p($l->t('Semantic Search')); ?></h2>
	<p>
		<?php p($l->t('Semantic search is not enabled on this server. Contact your administrator to enable this feature.')); ?>
	</p>
</div>
<?php endif; ?>

<div class="section">
	<h2><?php p($l->t('Manage Connection')); ?></h2>
		<p><?php p($l->t('You are connected to the Astrolabe service.')); ?></p>

		<div class="mcp-revoke-section">
			<form method="post" action="<?php p($urlGenerator->linkToRoute('astrolabe.oauth.disconnect')); ?>" id="mcp-disconnect-form">
				<input type="hidden" name="requesttoken" value="<?php p($_['requesttoken']); ?>">
				<button type="submit" class="button warning" id="mcp-disconnect-button">
					<span class="icon icon-close"></span>
					<?php p($l->t('Disconnect')); ?>
				</button>
				<p class="mcp-help-text">
					<?php p($l->t('This will disconnect from the Astrolabe service. You will need to re-authorize to use semantic search features.')); ?>
				</p>
			</form>
	</div>
</div>
