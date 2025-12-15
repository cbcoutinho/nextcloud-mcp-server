<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Search;

use OCA\Astroglobe\AppInfo\Application;
use OCA\Astroglobe\Service\McpServerClient;
use OCA\Astroglobe\Service\McpTokenStorage;
use OCA\Astroglobe\Settings\Admin as AdminSettings;
use OCP\Files\FileInfo;
use OCP\Files\IMimeTypeDetector;
use OCP\IConfig;
use OCP\IL10N;
use OCP\IPreview;
use OCP\IURLGenerator;
use OCP\IUser;
use OCP\Search\IProvider;
use OCP\Search\ISearchQuery;
use OCP\Search\SearchResult;
use OCP\Search\SearchResultEntry;
use Psr\Log\LoggerInterface;

/**
 * Unified Search provider for MCP Server semantic search.
 *
 * Delegates search queries to the MCP server's vector search API,
 * returning semantically relevant results from indexed Nextcloud content
 * (notes, files, calendar, deck cards).
 *
 * Security: Results are filtered server-side to only include documents
 * owned by the searching user. User identity comes from OAuth token.
 */
class SemanticSearchProvider implements IProvider {
	public function __construct(
		private McpServerClient $client,
		private McpTokenStorage $tokenStorage,
		private IConfig $config,
		private IL10N $l10n,
		private IURLGenerator $urlGenerator,
		private IMimeTypeDetector $mimeTypeDetector,
		private IPreview $previewManager,
		private LoggerInterface $logger,
	) {
	}

	/**
	 * Unique identifier for this search provider.
	 */
	public function getId(): string {
		return Application::APP_ID . '_semantic';
	}

	/**
	 * Display name shown in search results grouping.
	 */
	public function getName(): string {
		return $this->l10n->t('Astroglobe');
	}

	/**
	 * Order in search results. Lower = higher priority.
	 * Use negative value when user is in our app's context.
	 */
	public function getOrder(string $route, array $routeParameters): int {
		if (str_contains($route, Application::APP_ID)) {
			return -1; // Prioritize when in Astroglobe app
		}
		return 40; // Above most apps, below files/mail
	}

	/**
	 * Execute semantic search via MCP server.
	 *
	 * SECURITY: Results are filtered server-side to only include documents
	 * owned by the searching user. User identity comes from OAuth token.
	 */
	public function search(IUser $user, ISearchQuery $query): SearchResult {
		$term = $query->getTerm();
		$limit = $query->getLimit();
		$cursor = $query->getCursor();

		// Skip empty queries
		if (empty(trim($term))) {
			return SearchResult::complete($this->getName(), []);
		}

		// Get OAuth token for user
		$accessToken = $this->tokenStorage->getAccessToken($user->getUID());
		if ($accessToken === null) {
			// User hasn't authorized the app yet - return empty results
			$this->logger->debug('No OAuth token for user in semantic search', [
				'user_id' => $user->getUID(),
			]);
			return SearchResult::complete($this->getName(), []);
		}

		// Check if MCP server is available and vector sync enabled
		$status = $this->client->getStatus();
		if (!empty($status['error']) || !($status['vector_sync_enabled'] ?? false)) {
			$this->logger->debug('MCP server not available or vector sync disabled', [
				'status' => $status,
			]);
			return SearchResult::complete($this->getName(), []);
		}

		// Load admin search settings
		$algorithm = $this->config->getAppValue(
			Application::APP_ID,
			AdminSettings::SETTING_SEARCH_ALGORITHM,
			AdminSettings::DEFAULT_SEARCH_ALGORITHM
		);
		$fusion = $this->config->getAppValue(
			Application::APP_ID,
			AdminSettings::SETTING_SEARCH_FUSION,
			AdminSettings::DEFAULT_SEARCH_FUSION
		);
		$scoreThreshold = (int)$this->config->getAppValue(
			Application::APP_ID,
			AdminSettings::SETTING_SEARCH_SCORE_THRESHOLD,
			(string)AdminSettings::DEFAULT_SEARCH_SCORE_THRESHOLD
		);
		$configuredLimit = (int)$this->config->getAppValue(
			Application::APP_ID,
			AdminSettings::SETTING_SEARCH_LIMIT,
			(string)AdminSettings::DEFAULT_SEARCH_LIMIT
		);

		// Use configured limit if query limit is higher
		$effectiveLimit = min($limit, $configuredLimit);

		// Calculate offset from cursor
		$offset = $cursor ? (int)$cursor : 0;

		// Execute semantic search with OAuth token and admin settings
		// Server extracts user_id from token - results filtered to that user's documents
		$results = $this->client->searchForUnifiedSearch(
			query: $term,
			token: $accessToken,
			limit: $effectiveLimit,
			offset: $offset,
			algorithm: $algorithm,
			fusion: $fusion,
			scoreThreshold: $scoreThreshold / 100.0, // Convert percentage to 0-1 range
		);

		if (!empty($results['error'])) {
			$this->logger->warning('Semantic search failed', [
				'error' => $results['error'],
				'query' => $term,
			]);
			return SearchResult::complete($this->getName(), []);
		}

		// Transform results to SearchResultEntry objects
		$entries = [];
		foreach ($results['results'] ?? [] as $result) {
			$entries[] = $this->transformResult($result);
		}

		// Return paginated if more results might exist
		$totalFound = $results['total_found'] ?? count($entries);
		if (count($entries) >= $effectiveLimit && $totalFound > $offset + $effectiveLimit) {
			return SearchResult::paginated(
				$this->getName(),
				$entries,
				(string)($offset + $effectiveLimit)
			);
		}

		return SearchResult::complete($this->getName(), $entries);
	}

	/**
	 * Transform MCP search result to Nextcloud SearchResultEntry.
	 */
	private function transformResult(array $result): SearchResultEntry {
		$docType = $result['doc_type'] ?? 'unknown';
		$title = $result['title'] ?? $this->l10n->t('Untitled');
		$excerpt = $result['excerpt'] ?? '';
		$score = $result['score'] ?? 0;
		$id = isset($result['id']) ? (string)$result['id'] : null;
		$mimeType = $result['mime_type'] ?? null;

		// Build resource URL based on document type
		$resourceUrl = $this->buildResourceUrl($result);

		// Get icon and thumbnail based on document type
		[$thumbnailUrl, $iconClass] = $this->getIconAndThumbnail($docType, $id, $mimeType);

		// Build metadata string with chunk and page info
		$metadataParts = [];

		// Chunk info (always available)
		if (isset($result['chunk_index']) && isset($result['total_chunks'])) {
			$chunkNum = $result['chunk_index'] + 1; // Convert 0-based to 1-based
			$metadataParts[] = sprintf('Chunk %d/%d', $chunkNum, $result['total_chunks']);
		}

		// Page info for PDFs
		if (!empty($result['page_number']) && !empty($result['page_count'])) {
			$metadataParts[] = sprintf('Page %d/%d', $result['page_number'], $result['page_count']);
		}

		// Combine metadata parts
		$metadata = !empty($metadataParts) ? implode(' · ', $metadataParts) : '';

		// Subline shows metadata + excerpt (or just metadata if no excerpt)
		if (!empty($excerpt)) {
			$subline = $metadata ? $metadata . "\n" . $excerpt : $excerpt;
		} else {
			$subline = $metadata ?: sprintf(
				'%s · %d%% %s',
				$this->getDocTypeLabel($docType),
				(int)($score * 100),
				$this->l10n->t('relevant')
			);
		}

		return new SearchResultEntry(
			$thumbnailUrl,
			$title,
			$subline,
			$resourceUrl,
			$iconClass,
			false // not rounded
		);
	}

	/**
	 * Build URL to navigate to the original document.
	 *
	 * URL formats match App.vue's getDocumentUrl() implementation for consistency.
	 */
	private function buildResourceUrl(array $result): string {
		$docType = $result['doc_type'] ?? 'unknown';
		$id = $result['id'] ?? null;
		$path = $result['path'] ?? null;

		return match ($docType) {
			'note' => $id
				? $this->urlGenerator->linkToRoute('notes.page.index') . '/#/note/' . $id
				: $this->urlGenerator->linkToRoute('notes.page.index'),

			'file' => $id
				? $this->urlGenerator->linkToRouteAbsolute('files.view.index') . 'files/' . $id . '?dir=/&editing=false&openfile=true'
				: $this->urlGenerator->linkToRouteAbsolute('files.view.index'),

			'deck_card' => isset($result['board_id']) && $id
				? $this->urlGenerator->linkToRoute('deck.page.index')
				  . "board/{$result['board_id']}/card/{$id}"
				: $this->urlGenerator->linkToRoute('deck.page.index'),

			'calendar', 'calendar_event' => $this->urlGenerator->linkToRoute('calendar.view.index'),

			'news_item' => $this->urlGenerator->linkToRoute('news.page.index'),

			'contact' => $this->urlGenerator->linkToRoute('contacts.page.index'),

			default => $this->urlGenerator->linkToRoute(Application::APP_ID . '.page.index'),
		};
	}

	/**
	 * Get icon and thumbnail for document type.
	 *
	 * Returns [thumbnailUrl, iconClass] tuple.
	 * For files, uses mimetype-specific icons and preview thumbnails when available.
	 * For other document types, uses appropriate icon classes.
	 *
	 * @return array{string, string} [thumbnailUrl, iconClass]
	 */
	private function getIconAndThumbnail(string $docType, ?string $id, ?string $mimeType): array {
		if ($docType === 'file' && $id !== null && $mimeType !== null) {
			// For files, check if preview is supported
			$thumbnailUrl = '';
			if ($this->previewManager->isMimeSupported($mimeType)) {
				$thumbnailUrl = $this->urlGenerator->linkToRouteAbsolute(
					'core.Preview.getPreviewByFileId',
					['x' => 32, 'y' => 32, 'fileId' => $id]
				);
			}

			// Get mimetype-specific icon class
			$iconClass = $mimeType === FileInfo::MIMETYPE_FOLDER
				? 'icon-folder'
				: $this->mimeTypeDetector->mimeTypeIcon($mimeType);

			return [$thumbnailUrl, $iconClass];
		}

		// For non-file document types, use icon classes
		$iconClass = match ($docType) {
			'note' => 'icon-notes',
			'deck_card' => 'icon-deck',
			'calendar', 'calendar_event' => 'icon-calendar',
			'news_item' => 'icon-rss',
			'contact' => 'icon-contacts',
			default => 'icon-file',
		};

		return ['', $iconClass];
	}

	/**
	 * Get human-readable label for document type.
	 */
	private function getDocTypeLabel(string $docType): string {
		return match ($docType) {
			'note' => $this->l10n->t('Note'),
			'file' => $this->l10n->t('File'),
			'deck_card' => $this->l10n->t('Deck Card'),
			'calendar', 'calendar_event' => $this->l10n->t('Calendar'),
			'news_item' => $this->l10n->t('News'),
			'contact' => $this->l10n->t('Contact'),
			default => $this->l10n->t('Document'),
		};
	}

	/**
	 * Truncate excerpt to a maximum length, breaking at word boundaries.
	 */
	private function truncateExcerpt(string $excerpt, int $maxLength): string {
		$excerpt = trim($excerpt);

		if (mb_strlen($excerpt) <= $maxLength) {
			return $excerpt;
		}

		// Find last space before limit
		$truncated = mb_substr($excerpt, 0, $maxLength);
		$lastSpace = mb_strrpos($truncated, ' ');

		if ($lastSpace !== false && $lastSpace > $maxLength * 0.7) {
			$truncated = mb_substr($truncated, 0, $lastSpace);
		}

		return $truncated . '…';
	}
}
