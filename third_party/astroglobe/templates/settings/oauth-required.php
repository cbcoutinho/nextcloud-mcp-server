<?php
/**
 * OAuth authorization required template.
 *
 * Shown when user needs to authorize Astroglobe for semantic search.
 * Implements OAuth 2.0 Authorization Code flow with PKCE.
 *
 * @var array $_ Template parameters
 * @var string $_['oauth_url'] URL to initiate OAuth flow
 * @var string $_['server_url'] Astroglobe service URL
 * @var bool $_['has_expired'] Whether token exists but is expired
 * @var string|null $_['error_message'] Optional error message to display
 */

use OCP\Util;

Util::addStyle('astroglobe', 'astroglobe-settings');
?>

<div id="mcp-personal-settings">
	<div class="mcp-settings-info">
		<p><?php p($l->t('AI-powered semantic search across your Nextcloud content.')); ?></p>
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
			<span class="icon icon-search"></span>
			<?php p($l->t('Enable Semantic Search')); ?>
		</h3>

		<?php if (isset($_['has_expired']) && $_['has_expired']): ?>
			<p>
				<?php p($l->t('Your authorization has expired. Please sign in again to continue using semantic search.')); ?>
			</p>
		<?php else: ?>
			<p>
				<?php p($l->t('To search your content by meaning, Astroglobe needs permission to index your Nextcloud data.')); ?>
			</p>
		<?php endif; ?>

		<p>
			<strong><?php p($l->t('What happens next?')); ?></strong>
		</p>

		<ol class="mcp-help-text">
			<li><?php p($l->t('Sign in to confirm your identity')); ?></li>
			<li><?php p($l->t('Grant permission to index your content')); ?></li>
			<li><?php p($l->t('Your content will be indexed for semantic search')); ?></li>
			<li><?php p($l->t('Start searching with natural language')); ?></li>
		</ol>

		<h4><?php p($l->t('Content to be Indexed')); ?></h4>

		<ul class="mcp-feature-list">
			<li>
				<span class="icon icon-files"></span>
				<div>
					<strong><?php p($l->t('Notes & Files')); ?></strong>
					<p><?php p($l->t('Your notes and documents will be searchable by meaning')); ?></p>
				</div>
			</li>
			<li>
				<span class="icon icon-calendar"></span>
				<div>
					<strong><?php p($l->t('Calendar & Tasks')); ?></strong>
					<p><?php p($l->t('Find events and tasks with natural language queries')); ?></p>
				</div>
			</li>
			<li>
				<span class="icon icon-category-dashboard"></span>
				<div>
					<strong><?php p($l->t('Deck Cards')); ?></strong>
					<p><?php p($l->t('Search across your Deck boards and cards')); ?></p>
				</div>
			</li>
		</ul>

		<div style="margin-top: 20px;">
			<a href="<?php p($_['oauth_url']); ?>" class="button primary">
				<span class="icon icon-play"></span>
				<?php if (isset($_['has_expired']) && $_['has_expired']): ?>
					<?php p($l->t('Sign In Again')); ?>
				<?php else: ?>
					<?php p($l->t('Enable Semantic Search')); ?>
				<?php endif; ?>
			</a>
		</div>

		<p class="mcp-help-text" style="margin-top: 16px;">
			<?php p($l->t('You can disable indexing at any time from this settings page.')); ?>
		</p>
	</div>

	<div class="mcp-status-card">
		<h3>
			<span class="icon icon-info"></span>
			<?php p($l->t('About Astroglobe')); ?>
		</h3>

		<p>
			<?php p($l->t('Astroglobe enables semantic search - finding content by meaning rather than exact keywords. Ask questions like "meeting notes from last week" or "recipes with chicken" to find relevant documents.')); ?>
		</p>

		<p class="mcp-help-text">
			<?php p($l->t('Your content is processed to understand its meaning, enabling powerful natural language search across all your Nextcloud data.')); ?>
		</p>

		<ul class="mcp-links">
			<li>
				<a href="https://github.com/cbcoutinho/nextcloud-mcp-server" target="_blank" rel="noopener noreferrer">
					<span class="icon icon-external"></span>
					<?php p($l->t('Learn More')); ?>
				</a>
			</li>
		</ul>
	</div>
</div>
