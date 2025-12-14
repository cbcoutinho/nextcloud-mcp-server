<?php
/**
 * Error template for MCP Server UI settings.
 *
 * Displayed when the MCP server cannot be reached or returns an error.
 *
 * @var array $_ Template parameters
 * @var string $_['error'] Error title
 * @var string $_['details'] Error details/message
 * @var string $_['server_url'] Configured server URL (optional)
 * @var string $_['help_text'] Additional help text (optional)
 */

style('astroglobe', 'astroglobe-settings');
?>

<div class="mcp-settings-error">
	<div class="notecard notecard-error">
		<h3>
			<span class="icon icon-error"></span>
			<?php p($_['error'] ?? 'Error'); ?>
		</h3>

		<?php if (isset($_['details'])): ?>
			<p><strong><?php p($l->t('Details:')); ?></strong></p>
			<p><code><?php p($_['details']); ?></code></p>
		<?php endif; ?>

		<?php if (isset($_['server_url'])): ?>
			<p><strong><?php p($l->t('Server URL:')); ?></strong></p>
			<p><code><?php p($_['server_url']); ?></code></p>
		<?php endif; ?>

		<?php if (isset($_['help_text'])): ?>
			<p class="mcp-help-text"><?php p($_['help_text']); ?></p>
		<?php endif; ?>

		<h4><?php p($l->t('Troubleshooting Steps:')); ?></h4>
		<ol>
			<li><?php p($l->t('Verify the MCP server is running and accessible')); ?></li>
			<li><?php p($l->t('Check that mcp_server_url in config.php is correct')); ?></li>
			<li><?php p($l->t('Ensure mcp_server_api_key matches the server configuration')); ?></li>
			<li><?php p($l->t('Check firewall rules and network connectivity')); ?></li>
			<li><?php p($l->t('Review MCP server logs for errors')); ?></li>
		</ol>

		<p>
			<a href="https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md" target="_blank" class="button">
				<?php p($l->t('View Documentation')); ?>
			</a>
		</p>
	</div>
</div>
