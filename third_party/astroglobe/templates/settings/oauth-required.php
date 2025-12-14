<?php
/**
 * OAuth authorization required template.
 *
 * Shown when user needs to authorize Nextcloud to access MCP server.
 * Implements OAuth 2.0 Authorization Code flow with PKCE.
 *
 * @var array $_ Template parameters
 * @var string $_['oauth_url'] URL to initiate OAuth flow
 * @var string $_['server_url'] MCP server base URL
 * @var bool $_['has_expired'] Whether token exists but is expired
 * @var string|null $_['error_message'] Optional error message to display
 */

use OCP\Util;

Util::addStyle('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-personal-settings">
	<div class="mcp-settings-info">
		<p><?php p($l->t('Configure your personal MCP Server integration.')); ?></p>
	</div>

	<?php if (isset($_['error_message'])): ?>
		<div class="mcp-status-card mcp-error">
			<h3>
				<span class="icon icon-error"></span>
				<?php p($l->t('Session Expired')); ?>
			</h3>
			<p><?php p($_['error_message']); ?></p>
		</div>
	<?php endif; ?>

	<div class="mcp-status-card">
		<h3>
			<span class="icon icon-password"></span>
			<?php p($l->t('Authorization Required')); ?>
		</h3>

		<?php if (isset($_['has_expired']) && $_['has_expired']): ?>
			<p>
				<?php p($l->t('Your MCP server access has expired. Please sign in again to continue using MCP features.')); ?>
			</p>
		<?php else: ?>
			<p>
				<?php p($l->t('To access MCP server features, you need to authorize Nextcloud to connect to your MCP server on your behalf.')); ?>
			</p>
		<?php endif; ?>

		<p>
			<strong><?php p($l->t('What happens next?')); ?></strong>
		</p>

		<ol class="mcp-help-text">
			<li><?php p($l->t('You will be redirected to your identity provider')); ?></li>
			<li><?php p($l->t('Sign in with your credentials')); ?></li>
			<li><?php p($l->t('Authorize Nextcloud to access the MCP server')); ?></li>
			<li><?php p($l->t('You will be redirected back to this page')); ?></li>
		</ol>

		<h4><?php p($l->t('Permissions Requested')); ?></h4>

		<ul class="mcp-feature-list">
			<li>
				<span class="icon icon-info"></span>
				<div>
					<strong><?php p($l->t('Profile Information')); ?></strong>
					<p><?php p($l->t('Basic profile information (user ID, email) for identification')); ?></p>
				</div>
			</li>
			<li>
				<span class="icon icon-files"></span>
				<div>
					<strong><?php p($l->t('Read Access')); ?></strong>
					<p><?php p($l->t('View your Notes, Calendar, Files, and other Nextcloud data')); ?></p>
				</div>
			</li>
			<li>
				<span class="icon icon-rename"></span>
				<div>
					<strong><?php p($l->t('Write Access')); ?></strong>
					<p><?php p($l->t('Create and modify Notes, Calendar events, Files, and other Nextcloud data')); ?></p>
				</div>
			</li>
		</ul>

		<div style="margin-top: 20px;">
			<a href="<?php p($_['oauth_url']); ?>" class="button primary">
				<span class="icon icon-play"></span>
				<?php if (isset($_['has_expired']) && $_['has_expired']): ?>
					<?php p($l->t('Sign In Again')); ?>
				<?php else: ?>
					<?php p($l->t('Authorize Access')); ?>
				<?php endif; ?>
			</a>
		</div>

		<p class="mcp-help-text" style="margin-top: 16px;">
			<?php p($l->t('By authorizing, you allow Nextcloud to access the MCP server at:')); ?>
			<br>
			<code><?php p($_['server_url']); ?></code>
		</p>

		<p class="mcp-help-text">
			<?php p($l->t('You can revoke this access at any time from this settings page.')); ?>
		</p>
	</div>

	<div class="mcp-status-card">
		<h3>
			<span class="icon icon-info"></span>
			<?php p($l->t('About MCP Server')); ?>
		</h3>

		<p>
			<?php p($l->t('The Model Context Protocol (MCP) server provides AI assistants with access to your Nextcloud data.')); ?>
		</p>

		<p class="mcp-help-text">
			<?php p($l->t('Once authorized, you can use AI tools like Claude Desktop to interact with your Notes, Calendar, Files, and more through natural language.')); ?>
		</p>

		<ul class="mcp-links">
			<li>
				<a href="https://github.com/cbcoutinho/nextcloud-mcp-server" target="_blank" rel="noopener noreferrer">
					<span class="icon icon-external"></span>
					<?php p($l->t('MCP Server Documentation')); ?>
				</a>
			</li>
			<li>
				<a href="https://modelcontextprotocol.io" target="_blank" rel="noopener noreferrer">
					<span class="icon icon-external"></span>
					<?php p($l->t('Learn about Model Context Protocol')); ?>
				</a>
			</li>
		</ul>
	</div>
</div>
