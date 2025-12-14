<?php

declare(strict_types=1);

namespace OCA\Astroglobe\Controller;

use OCA\Astroglobe\Service\McpServerClient;
use OCA\Astroglobe\Service\McpTokenStorage;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\AppFramework\Http\RedirectResponse;
use OCP\IRequest;
use OCP\IURLGenerator;
use OCP\IUserSession;
use Psr\Log\LoggerInterface;

/**
 * API controller for MCP Server UI.
 *
 * Handles form submissions and AJAX requests from settings panels.
 */
class ApiController extends Controller {
	private $client;
	private $userSession;
	private $urlGenerator;
	private $logger;
	private $tokenStorage;

	public function __construct(
		string $appName,
		IRequest $request,
		McpServerClient $client,
		IUserSession $userSession,
		IURLGenerator $urlGenerator,
		LoggerInterface $logger,
		McpTokenStorage $tokenStorage
	) {
		parent::__construct($appName, $request);
		$this->client = $client;
		$this->userSession = $userSession;
		$this->urlGenerator = $urlGenerator;
		$this->logger = $logger;
		$this->tokenStorage = $tokenStorage;
	}

	/**
	 * Revoke user's background access (delete refresh token).
	 *
	 * Called from personal settings form POST.
	 * Redirects back to personal settings after completion.
	 *
	 * @return RedirectResponse
	 */
	#[NoAdminRequired]
	public function revokeAccess(): RedirectResponse {
		$user = $this->userSession->getUser();
		if (!$user) {
			// Should not happen (NoAdminRequired ensures user is logged in)
			$this->logger->error('Revoke access called without authenticated user');
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
			);
		}

		$userId = $user->getUID();

		// Get user's OAuth token
		$token = $this->tokenStorage->getUserToken($userId);
		if (!$token) {
			$this->logger->error("Cannot revoke access: No token found for user $userId");
			return new RedirectResponse(
				$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
			);
		}

		$accessToken = $token['access_token'];

		// Call MCP server API to revoke access
		$result = $this->client->revokeUserAccess($userId, $accessToken);

		if (isset($result['error'])) {
			$this->logger->error("Failed to revoke access for user $userId", [
				'error' => $result['error']
			]);
			// TODO: Add flash message/notification for user feedback
		} else {
			$this->logger->info("Successfully revoked background access for user $userId");
			// TODO: Add success flash message/notification
		}

		// Redirect back to personal settings
		return new RedirectResponse(
			$this->urlGenerator->linkToRoute('settings.PersonalSettings.index', ['section' => 'astroglobe'])
		);
	}

	/**
	 * Execute semantic search via MCP server.
	 *
	 * AJAX endpoint for vector search UI in app page.
	 * Uses user's OAuth token for authentication.
	 *
	 * @param string $query Search query
	 * @param string $algorithm Search algorithm (semantic, bm25, hybrid)
	 * @param int $limit Number of results (max 50)
	 * @param string $doc_types Comma-separated document types (e.g., "note,file")
	 * @param string $include_pca Whether to include PCA coordinates for visualization
	 * @return JSONResponse
	 */
	#[NoAdminRequired]
	public function search(
		string $query = '',
		string $algorithm = 'hybrid',
		int $limit = 10,
		string $doc_types = '',
		string $include_pca = 'true'
	): JSONResponse {
		if (empty($query)) {
			return new JSONResponse([
				'success' => false,
				'error' => 'Missing required parameter: query'
			], Http::STATUS_BAD_REQUEST);
		}

		// Get current user
		$user = $this->userSession->getUser();
		if (!$user) {
			return new JSONResponse([
				'success' => false,
				'error' => 'User not authenticated'
			], Http::STATUS_UNAUTHORIZED);
		}

		$userId = $user->getUID();

		// Get user's OAuth token for MCP server
		$accessToken = $this->tokenStorage->getAccessToken($userId);
		if (!$accessToken) {
			return new JSONResponse([
				'success' => false,
				'error' => 'MCP server authorization required. Please authorize the app first.'
			], Http::STATUS_UNAUTHORIZED);
		}

		// Validate algorithm
		$validAlgorithms = ['semantic', 'bm25', 'hybrid'];
		if (!in_array($algorithm, $validAlgorithms)) {
			$algorithm = 'hybrid';
		}

		// Enforce limit bounds
		$limit = max(1, min($limit, 50));

		// Parse doc_types filter
		$docTypesArray = null;
		if (!empty($doc_types)) {
			$validDocTypes = ['note', 'file', 'deck_card', 'calendar', 'contact', 'news_item'];
			$docTypesArray = array_filter(
				explode(',', $doc_types),
				fn($t) => in_array(trim($t), $validDocTypes)
			);
			$docTypesArray = array_map('trim', $docTypesArray);
			if (empty($docTypesArray)) {
				$docTypesArray = null;
			}
		}

		// Parse include_pca (string "true"/"false" from query params)
		$includePcaBool = in_array(strtolower($include_pca), ['true', '1', 'yes'], true);

		// Execute search via MCP server with OAuth token
		$result = $this->client->search($query, $algorithm, $limit, $includePcaBool, $docTypesArray, $accessToken);

		if (isset($result['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $result['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		$response = [
			'success' => true,
			'results' => $result['results'] ?? [],
			'algorithm_used' => $result['algorithm_used'] ?? $algorithm,
			'total_documents' => $result['total_documents'] ?? 0,
		];

		// Include PCA visualization coordinates if requested and available
		if ($includePcaBool) {
			$response['coordinates_3d'] = $result['coordinates_3d'] ?? [];
			$response['query_coords'] = $result['query_coords'] ?? [];
			if (isset($result['pca_variance'])) {
				$response['pca_variance'] = $result['pca_variance'];
			}
		}

		return new JSONResponse($response);
	}

	/**
	 * Get vector sync status from MCP server.
	 *
	 * AJAX endpoint for status refresh in personal settings.
	 *
	 * @return JSONResponse
	 */
	#[NoAdminRequired]
	public function vectorStatus(): JSONResponse {
		$status = $this->client->getVectorSyncStatus();

		if (isset($status['error'])) {
			return new JSONResponse([
				'success' => false,
				'error' => $status['error']
			], Http::STATUS_INTERNAL_SERVER_ERROR);
		}

		return new JSONResponse([
			'success' => true,
			'status' => $status
		]);
	}
}
