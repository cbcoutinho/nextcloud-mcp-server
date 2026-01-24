<template>
	<NcContent app-name="astrolabe">
		<NcAppNavigation>
			<template #list>
				<NcAppNavigationItem
					:name="t('astrolabe', 'Semantic Search')"
					:active="activeSection === 'search'"
					@click="activeSection = 'search'">
					<template #icon>
						<Magnify :size="20" />
					</template>
				</NcAppNavigationItem>

				<NcAppNavigationItem
					:name="t('astrolabe', 'Index Status')"
					:active="activeSection === 'status'"
					@click="activeSection = 'status'; loadVectorStatus()">
					<template #icon>
						<ChartBox :size="20" />
					</template>
				</NcAppNavigationItem>
			</template>

			<template #footer>
				<ul class="app-navigation-entry__settings">
					<NcAppNavigationItem
						:name="t('astrolabe', 'Settings')"
						@click="goToSettings">
						<template #icon>
							<Cog :size="20" />
						</template>
					</NcAppNavigationItem>
				</ul>
			</template>
		</NcAppNavigation>

		<NcAppContent>
			<!-- Search Section -->
			<div v-show="activeSection === 'search'" class="mcp-section">
				<div class="mcp-section-header">
					<h2>{{ t('astrolabe', 'Semantic Search') }}</h2>
					<p class="mcp-description">
						{{ t('astrolabe', 'Search your indexed content using semantic similarity. Find documents by meaning, not just keywords.') }}
					</p>
				</div>

				<!-- Search Controls -->
				<div class="mcp-search-card">
					<div class="mcp-search-row">
						<NcTextField
							v-model="query"
							:label="t('astrolabe', 'Search query')"
							:placeholder="t('astrolabe', 'Enter your search query...')"
							class="mcp-search-input"
							@keyup.enter="performSearch" />

						<NcSelect
							:model-value="selectedAlgorithmOption"
							:options="algorithmOptions"
							:placeholder="t('astrolabe', 'Algorithm')"
							class="mcp-algorithm-select"
							@update:model-value="algorithm = $event ? $event.id : 'hybrid'" />

						<NcButton
							variant="primary"
							:disabled="!query.trim() || loading"
							@click="performSearch">
							<template #icon>
								<Magnify :size="20" />
							</template>
							{{ t('astrolabe', 'Search') }}
						</NcButton>
					</div>

					<!-- Advanced Options Toggle -->
					<NcButton
						variant="tertiary"
						class="mcp-advanced-toggle"
						@click="showAdvanced = !showAdvanced">
						<template #icon>
							<ChevronDown v-if="!showAdvanced" :size="20" />
							<ChevronUp v-else :size="20" />
						</template>
						{{ showAdvanced ? t('astrolabe', 'Hide advanced') : t('astrolabe', 'Advanced options') }}
					</NcButton>

					<!-- Advanced Options -->
					<div v-show="showAdvanced" class="mcp-advanced-options">
						<div class="mcp-advanced-grid">
							<div class="mcp-option-group">
								<label>{{ t('astrolabe', 'Document Types') }}</label>
								<div class="mcp-checkbox-grid">
									<NcCheckboxRadioSwitch
										v-for="docType in docTypeOptions"
										:key="docType.id"
										:model-value="selectedDocTypes.includes(docType.id)"
										type="checkbox"
										@update:model-value="toggleDocType(docType.id, $event)">
										{{ docType.label }}
									</NcCheckboxRadioSwitch>
								</div>
							</div>

							<div class="mcp-option-group">
								<label>{{ t('astrolabe', 'Result Limit') }}</label>
								<NcTextField
									v-model="limit"
									type="number"
									:min="1"
									:max="100" />
							</div>

							<div class="mcp-option-group">
								<label>{{ t('astrolabe', 'Minimum Score') }}: {{ scoreThreshold }}%</label>
								<input
									v-model="scoreThreshold"
									type="range"
									min="0"
									max="100"
									step="5"
									class="mcp-score-slider">
							</div>
						</div>
					</div>
				</div>

				<!-- Loading State -->
				<div v-if="loading" class="mcp-loading">
					<NcLoadingIcon :size="32" />
					<span>{{ t('astrolabe', 'Searching...') }}</span>
				</div>

				<!-- Error State -->
				<NcNoteCard v-if="error" type="error" class="mcp-error">
					{{ error }}
				</NcNoteCard>

				<!-- Results -->
				<div v-if="results.length > 0 && !loading" class="mcp-results">
					<div class="mcp-results-header">
						<span>
							{{ filteredResults.length }} {{ t('astrolabe', 'results') }}
							<span v-if="filteredResults.length !== results.length" class="mcp-filter-info">
								({{ results.length - filteredResults.length }} {{ t('astrolabe', 'filtered by score') }})
							</span>
						</span>
						<span class="mcp-algorithm-badge">{{ algorithmUsed }}</span>
					</div>

					<!-- 3D Visualization -->
					<div v-if="coordinates.length > 0" class="mcp-viz-container">
						<div class="mcp-viz-header">
							<h3>{{ t('astrolabe', 'Vector Space Visualization') }}</h3>
							<NcCheckboxRadioSwitch
								:model-value="showQueryPoint"
								type="switch"
								@update:model-value="showQueryPoint = $event; updatePlot()">
								{{ t('astrolabe', 'Show query point') }}
							</NcCheckboxRadioSwitch>
						</div>
						<div id="viz-plot-container" class="mcp-viz-plot-container">
							<div id="viz-plot" ref="vizPlot" />
						</div>
					</div>

					<div class="mcp-results-list">
						<div
							v-for="(result, index) in filteredResults"
							:key="result.id || index"
							class="mcp-result-item"
							:class="'mcp-doc-type-' + (result.doc_type || 'unknown')">
							<div class="mcp-result-header">
								<span class="mcp-result-type">{{ result.doc_type || 'unknown' }}</span>
								<div class="mcp-result-actions">
									<NcButton
										variant="tertiary"
										:aria-label="t('astrolabe', 'Show Chunk')"
										@click="viewChunk(result)">
										<template #icon>
											<Eye :size="18" />
										</template>
										{{ t('astrolabe', 'Show Chunk') }}
									</NcButton>
									<span class="mcp-result-score">{{ formatScore(result.score) }}%</span>
								</div>
							</div>
							<a
								:href="getDocumentUrl(result)"
								class="mcp-result-title"
								@click.prevent="navigateToDocument(result)">
								{{ result.title || t('astrolabe', 'Untitled') }}
								<OpenInNew :size="14" class="mcp-external-icon" />
							</a>
							<div class="mcp-result-metadata">
								<span v-if="result.chunk_index !== undefined && result.total_chunks">
									{{ t('astrolabe', 'Chunk {chunk}/{total}', { chunk: result.chunk_index + 1, total: result.total_chunks }) }}
								</span>
								<span v-if="result.page_number && result.page_count" class="mcp-metadata-separator">
									· {{ t('astrolabe', 'Page {page}/{total}', { page: result.page_number, total: result.page_count }) }}
								</span>
							</div>
						</div>
					</div>
				</div>

				<!-- No Results -->
				<NcEmptyContent
					v-if="searched && results.length === 0 && !loading && !error"
					:name="t('astrolabe', 'No results found')"
					:description="t('astrolabe', 'Try a different query or search algorithm.')">
					<template #icon>
						<Magnify />
					</template>
				</NcEmptyContent>

				<!-- Initial State -->
				<NcEmptyContent
					v-if="!searched && !loading"
					:name="t('astrolabe', 'Semantic Search')"
					:description="t('astrolabe', 'Enter a query above to search your indexed content.')">
					<template #icon>
						<Magnify />
					</template>
				</NcEmptyContent>
			</div>

			<!-- Index Status Section -->
			<div v-show="activeSection === 'status'" class="mcp-section">
				<div class="mcp-section-header">
					<h2>{{ t('astrolabe', 'Index Status') }}</h2>
					<p class="mcp-description">
						{{ t('astrolabe', 'View the status of your vector index and sync progress.') }}
					</p>
				</div>

				<div v-if="statusLoading" class="mcp-loading">
					<NcLoadingIcon :size="32" />
					<span>{{ t('astrolabe', 'Loading status...') }}</span>
				</div>

				<NcNoteCard v-else-if="statusError" type="error">
					{{ statusError }}
				</NcNoteCard>

				<div v-else-if="vectorStatus" class="mcp-status-cards">
					<div class="mcp-status-card">
						<div class="mcp-status-label">
							{{ t('astrolabe', 'Sync Status') }}
						</div>
						<div class="mcp-status-value" :class="'status-' + vectorStatus.status">
							{{ vectorStatus.status }}
						</div>
					</div>

					<div class="mcp-status-card">
						<div class="mcp-status-label">
							{{ t('astrolabe', 'Indexed Documents') }}
						</div>
						<div class="mcp-status-value">
							{{ vectorStatus.indexed_documents || 0 }}
						</div>
					</div>

					<div class="mcp-status-card">
						<div class="mcp-status-label">
							{{ t('astrolabe', 'Pending Documents') }}
						</div>
						<div class="mcp-status-value">
							{{ vectorStatus.pending_documents || 0 }}
						</div>
					</div>

					<div v-if="vectorStatus.last_sync_time" class="mcp-status-card">
						<div class="mcp-status-label">
							{{ t('astrolabe', 'Last Sync') }}
						</div>
						<div class="mcp-status-value">
							{{ vectorStatus.last_sync_time }}
						</div>
					</div>
				</div>

				<NcButton variant="secondary" :disabled="statusLoading" @click="loadVectorStatus">
					<template #icon>
						<Refresh :size="20" />
					</template>
					{{ t('astrolabe', 'Refresh') }}
				</NcButton>
			</div>
		</NcAppContent>

		<!-- PDF/Chunk Viewer Modal -->
		<div v-if="showViewer" class="mcp-modal-overlay" @click.self="closeViewer">
			<div class="mcp-modal">
				<!-- Fixed Header -->
				<div class="mcp-modal-header">
					<h3>
						<a
							v-if="currentResult"
							:href="getDocumentUrl(currentResult)"
							class="mcp-modal-title-link"
							@click.prevent="navigateToDocument(currentResult)">
							{{ viewerTitle }}
							<OpenInNew :size="16" class="mcp-modal-title-icon" />
						</a>
						<span v-else>{{ viewerTitle }}</span>
					</h3>
					<NcButton variant="tertiary" @click="closeViewer">
						<template #icon>
							<Close :size="20" />
						</template>
					</NcButton>
				</div>

				<!-- Scrollable Content -->
				<div class="mcp-modal-body">
					<!-- Loading State -->
					<div v-if="viewerLoading" class="mcp-viewer-loading">
						<NcLoadingIcon :size="32" />
						<span>{{ t('astrolabe', 'Loading content...') }}</span>
					</div>

					<!-- PDF Viewer (canvas only, controls in footer) -->
					<PDFViewer
						v-else-if="viewerType === 'pdf'"
						:file-path="currentPdfPath"
						:page-number="viewerPage"
						@prev-page="viewerPage--"
						@next-page="viewerPage++"
						@loaded="handlePdfLoaded"
						@error="handlePdfError" />

					<!-- Markdown Viewer (for non-PDFs) -->
					<MarkdownViewer
						v-else
						:content="getMarkdownContent()" />
				</div>

				<!-- Fixed Footer (navigation controls) -->
				<div v-if="!viewerLoading && viewerType === 'pdf' && pdfTotalPages > 0" class="mcp-modal-footer">
					<NcButton
						:disabled="viewerPage <= 1"
						@click="viewerPage--">
						<template #icon>
							<ChevronLeft :size="20" />
						</template>
						{{ t('astrolabe', 'Previous') }}
					</NcButton>
					<span class="mcp-page-info">
						{{ t('astrolabe', 'Page {current} of {total}', { current: viewerPage, total: pdfTotalPages }) }}
					</span>
					<NcButton
						:disabled="viewerPage >= pdfTotalPages"
						@click="viewerPage++">
						<template #icon>
							<ChevronRight :size="20" />
						</template>
						{{ t('astrolabe', 'Next') }}
					</NcButton>
				</div>
			</div>
		</div>
	</NcContent>
</template>

<script>
import NcContent from '@nextcloud/vue/components/NcContent'
import NcAppNavigation from '@nextcloud/vue/components/NcAppNavigation'
import NcAppNavigationItem from '@nextcloud/vue/components/NcAppNavigationItem'
import NcAppContent from '@nextcloud/vue/components/NcAppContent'
import NcButton from '@nextcloud/vue/components/NcButton'
import NcTextField from '@nextcloud/vue/components/NcTextField'
import NcSelect from '@nextcloud/vue/components/NcSelect'
import NcLoadingIcon from '@nextcloud/vue/components/NcLoadingIcon'
import NcNoteCard from '@nextcloud/vue/components/NcNoteCard'
import NcEmptyContent from '@nextcloud/vue/components/NcEmptyContent'
import NcCheckboxRadioSwitch from '@nextcloud/vue/components/NcCheckboxRadioSwitch'

import Magnify from 'vue-material-design-icons/Magnify.vue'
import ChartBox from 'vue-material-design-icons/ChartBox.vue'
import Cog from 'vue-material-design-icons/Cog.vue'
import ChevronDown from 'vue-material-design-icons/ChevronDown.vue'
import ChevronUp from 'vue-material-design-icons/ChevronUp.vue'
import ChevronLeft from 'vue-material-design-icons/ChevronLeft.vue'
import ChevronRight from 'vue-material-design-icons/ChevronRight.vue'
import Refresh from 'vue-material-design-icons/Refresh.vue'
import OpenInNew from 'vue-material-design-icons/OpenInNew.vue'
import Eye from 'vue-material-design-icons/Eye.vue'
import Close from 'vue-material-design-icons/Close.vue'

import PDFViewer from './components/PDFViewer.vue'
import MarkdownViewer from './components/MarkdownViewer.vue'

import axios from '@nextcloud/axios'
import { generateUrl } from '@nextcloud/router'
import Plotly from 'plotly.js-dist-min'
import * as pdfjsLib from 'pdfjs-dist'

// Set worker source with error handling
try {
	pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
		'pdfjs-dist/build/pdf.worker.mjs',
		import.meta.url,
	).toString()
} catch (e) {
	console.warn('Failed to set PDF.js worker, will use fallback', e)
	// PDF.js will use fake worker automatically
}

export default {
	name: 'App',
	components: {
		NcContent,
		NcAppNavigation,
		NcAppNavigationItem,
		NcAppContent,
		NcButton,
		NcTextField,
		NcSelect,
		NcLoadingIcon,
		NcNoteCard,
		NcEmptyContent,
		NcCheckboxRadioSwitch,
		PDFViewer,
		MarkdownViewer,
		Magnify,
		ChartBox,
		Cog,
		ChevronDown,
		ChevronUp,
		ChevronLeft,
		ChevronRight,
		Refresh,
		OpenInNew,
		Eye,
		Close,
	},
	data() {
		return {
			activeSection: 'search',
			// Search state
			query: '',
			algorithm: 'hybrid',
			showAdvanced: false,
			selectedDocTypes: [],
			limit: 20,
			scoreThreshold: 0,
			loading: false,
			error: null,
			results: [],
			algorithmUsed: '',
			searched: false,
			expandedExcerpts: {},
			// Visualization state
			coordinates: [],
			queryCoords: [],
			showQueryPoint: true,
			// Vector status state
			vectorStatus: null,
			statusLoading: false,
			statusError: null,
			// Viewer state
			showViewer: false,
			viewerLoading: false,
			viewerTitle: '',
			viewerType: 'text',
			viewerPage: 1,
			pdfTotalPages: 0,
			currentPdfPath: '',
			currentResult: null, // Store the current result for document linking
			viewerContext: {
				chunk: '',
				before: '',
				after: '',
			},
		}
	},
	computed: {
		algorithmOptions() {
			return [
				{ id: 'hybrid', label: this.t('astrolabe', 'Hybrid') },
				{ id: 'semantic', label: this.t('astrolabe', 'Semantic') },
				{ id: 'bm25', label: this.t('astrolabe', 'Keyword (BM25)') },
			]
		},
		docTypeOptions() {
			return [
				{ id: 'note', label: this.t('astrolabe', 'Notes') },
				{ id: 'file', label: this.t('astrolabe', 'Files') },
				{ id: 'deck_card', label: this.t('astrolabe', 'Deck Cards') },
				{ id: 'calendar', label: this.t('astrolabe', 'Calendar') },
				{ id: 'contact', label: this.t('astrolabe', 'Contacts') },
				{ id: 'news_item', label: this.t('astrolabe', 'News') },
			]
		},
		selectedAlgorithmOption() {
			return this.algorithmOptions.find(opt => opt.id === this.algorithm) || this.algorithmOptions[0]
		},
		filteredResults() {
			const threshold = this.scoreThreshold / 100
			return this.results.filter(r => (r.score || 0) >= threshold)
		},
	},
	mounted() {
		// Check for URL parameters to open chunk viewer
		this.handleUrlParameters()
	},
	beforeUnmount() {
		// Clean up Plotly event handlers to prevent memory leaks
		const plotDiv = document.getElementById('viz-plot')
		if (plotDiv && plotDiv.on) {
			plotDiv.removeAllListeners('plotly_click')
		}
	},
	methods: {
		handleUrlParameters() {
			// Parse URL parameters
			const urlParams = new URLSearchParams(window.location.search)
			const docType = urlParams.get('doc_type')
			const docId = urlParams.get('doc_id')
			const chunkStart = urlParams.get('chunk_start')
			const chunkEnd = urlParams.get('chunk_end')

			// If we have chunk parameters, open the viewer
			if (docType && docId && chunkStart !== null && chunkEnd !== null) {
				// Construct a minimal result object
				const result = {
					doc_type: docType,
					id: parseInt(docId, 10),
					chunk_start_offset: parseInt(chunkStart, 10),
					chunk_end_offset: parseInt(chunkEnd, 10),
					title: urlParams.get('title') || this.t('astrolabe', 'Chunk Viewer'),
					metadata: {},
				}

				// Add optional metadata
				const path = urlParams.get('path')
				if (path) {
					result.metadata.path = path
				}
				const pageNumber = urlParams.get('page_number')
				if (pageNumber) {
					result.page_number = parseInt(pageNumber, 10)
				}
				const boardId = urlParams.get('board_id')
				if (boardId) {
					result.metadata.board_id = boardId
				}

				// Open the chunk viewer
				this.$nextTick(() => {
					this.viewChunk(result)
				})

				// Clear URL parameters to avoid reopening on navigation
				const newUrl = window.location.pathname
				window.history.replaceState({}, '', newUrl)
			}
		},

		toggleDocType(docTypeId, checked) {
			if (checked && !this.selectedDocTypes.includes(docTypeId)) {
				this.selectedDocTypes.push(docTypeId)
			} else if (!checked) {
				const index = this.selectedDocTypes.indexOf(docTypeId)
				if (index > -1) {
					this.selectedDocTypes.splice(index, 1)
				}
			}
		},

		async performSearch() {
			const queryText = this.query.trim()
			if (!queryText) {
				return
			}

			this.loading = true
			this.error = null
			this.searched = true
			this.coordinates = []
			this.queryCoords = []
			this.expandedExcerpts = {}

			try {
				const url = generateUrl('/apps/astrolabe/api/search')
				const params = {
					query: queryText,
					algorithm: this.algorithm,
					limit: parseInt(this.limit) || 20,
					include_pca: true,
				}

				if (this.selectedDocTypes.length > 0) {
					params.doc_types = this.selectedDocTypes.join(',')
				}

				const response = await axios.get(url, { params })

				if (response.data.success) {
					this.results = response.data.results || []
					this.algorithmUsed = response.data.algorithm_used || this.algorithm
					this.coordinates = response.data.coordinates_3d || []
					this.queryCoords = response.data.query_coords || []

					// Render visualization after DOM updates
					if (this.coordinates.length > 0) {
						this.$nextTick(() => {
							this.renderPlot()
						})
					}
				} else {
					this.error = response.data.error || this.t('astrolabe', 'Search failed')
					this.results = []
				}
			} catch (err) {
				console.error('Search error:', err)
				// Check if this is an HTTP error with a response
				if (err.response && err.response.data && err.response.data.error) {
					// Use the specific error message from the backend
					this.error = err.response.data.error
				} else if (err.response && err.response.status === 401) {
					// Unauthorized - user needs to authorize the app
					this.error = this.t('astrolabe', 'Authorization required. Please complete Step 1 in Settings → Astrolabe.')
				} else if (err.response && err.response.status === 503) {
					// Service unavailable - MCP server not reachable
					this.error = this.t('astrolabe', 'Search service unavailable. Please try again later.')
				} else {
					// Actual network error or unknown error
					this.error = this.t('astrolabe', 'Network error. Please try again.')
				}
				this.results = []
			} finally {
				this.loading = false
			}
		},

		async loadVectorStatus() {
			this.statusLoading = true
			this.statusError = null

			try {
				const url = generateUrl('/apps/astrolabe/api/vector-status')
				const response = await axios.get(url)

				if (response.data.success) {
					this.vectorStatus = response.data.status
				} else {
					this.statusError = response.data.error || this.t('astrolabe', 'Failed to load status')
				}
			} catch (err) {
				console.error('Status error:', err)
				// Extract error message from response if available
				if (err.response && err.response.data && err.response.data.error) {
					this.statusError = err.response.data.error
				} else if (err.response && err.response.status === 401) {
					this.statusError = this.t('astrolabe', 'Authorization required. Please complete Step 1 in Settings → Astrolabe.')
				} else {
					this.statusError = this.t('astrolabe', 'Network error. Please try again.')
				}
			} finally {
				this.statusLoading = false
			}
		},

		formatScore(score) {
			return Math.round((score || 0) * 100)
		},

		toggleExcerpt(index) {
			this.expandedExcerpts[index] = !this.expandedExcerpts[index]
		},

		truncateExcerpt(text, maxLength = 150) {
			if (!text || text.length <= maxLength) return text
			return text.substring(0, maxLength).trim() + '...'
		},

		getDocumentUrl(result) {
			const docType = result.doc_type || 'unknown'
			const id = result.id || result.note_id
			const metadata = result.metadata || {}

			switch (docType) {
			case 'note':
				return generateUrl(`/apps/notes/#/note/${id}`)
			case 'file':
				if (id) {
					return generateUrl(`/apps/files/files/${id}?dir=/&editing=false&openfile=true`)
				}
				return generateUrl('/apps/files/')
			case 'deck_card':
				if (metadata.board_id && id) {
					return generateUrl(`/apps/deck/board/${metadata.board_id}/card/${id}`)
				}
				return generateUrl('/apps/deck/')
			case 'calendar':
			case 'calendar_event':
				return generateUrl('/apps/calendar/')
			case 'news_item':
				// Use external article URL if available, otherwise fall back to News app
				if (metadata.url) {
					return metadata.url
				}
				return generateUrl('/apps/news/')
			case 'contact':
				return generateUrl('/apps/contacts/')
			default:
				return generateUrl('/apps/astrolabe/')
			}
		},

		navigateToDocument(result) {
			const url = this.getDocumentUrl(result)
			window.open(url, '_blank')
		},

		goToSettings() {
			window.location.href = generateUrl('/settings/user/astrolabe')
		},

		renderPlot() {
			const container = document.getElementById('viz-plot-container')
			if (!container) return

			const width = container.clientWidth
			const height = container.clientHeight || 400

			const coordinates = this.coordinates
			const queryCoords = this.queryCoords
			const results = this.results

			const scores = results.map(r => r.score)

			// Trace 1: Document results (always visible)
			const documentTrace = {
				x: coordinates.map(c => c[0]),
				y: coordinates.map(c => c[1]),
				z: coordinates.map(c => c[2]),
				mode: 'markers',
				type: 'scatter3d',
				name: 'Documents',
				visible: true,
				customdata: results.map((r, i) => ({
					title: r.title,
					raw_score: r.original_score || r.score,
					relative_score: r.score,
					x: coordinates[i][0],
					y: coordinates[i][1],
					z: coordinates[i][2],
				})),
				hovertemplate:
					'<b>%{customdata.title}</b><br>'
					+ 'Raw Score: %{customdata.raw_score:.3f} (%{customdata.relative_score:.0%} relative)<br>'
					+ '(x=%{customdata.x}, y=%{customdata.y}, z=%{customdata.z})'
					+ '<extra></extra>',
				hoverlabel: {
					bgcolor: '#0082c9',
					bordercolor: '#0082c9',
					font: {
						size: 15,
						color: 'white',
					},
				},
				marker: {
					size: results.map(r => 4 + (Math.pow(r.score, 2) * 10)),
					opacity: results.map(r => 0.3 + (r.score * 0.7)),
					color: scores,
					colorscale: 'Viridis',
					showscale: true,
					colorbar: {
						title: { text: 'Relative Score' },
						x: 1.02,
						xanchor: 'left',
						thickness: 20,
						len: 0.8,
					},
					cmin: 0,
					cmax: 1,
				},
			}

			// Trace 2: Query point (visibility controlled by toggle)
			const queryTrace = {
				x: [queryCoords[0]],
				y: [queryCoords[1]],
				z: [queryCoords[2]],
				mode: 'markers',
				type: 'scatter3d',
				name: 'Query',
				visible: this.showQueryPoint,
				hovertemplate:
					'<b>Search Query</b><br>'
					+ `(x=${queryCoords[0]}, y=${queryCoords[1]}, z=${queryCoords[2]})`
					+ '<extra></extra>',
				marker: {
					size: 10,
					color: '#ef5350', // Subdued red (Material Design Red 400)
					line: {
						color: '#c62828', // Darker red border (Material Design Red 800)
						width: 1,
					},
				},
			}

			const layout = {
				title: { text: `Vector Space (PCA 3D) - ${results.length} results` },
				width,
				height,
				scene: {
					xaxis: { title: { text: 'PC1' } },
					yaxis: { title: { text: 'PC2' } },
					zaxis: { title: { text: 'PC3' } },
					camera: {
						eye: { x: 1.5, y: 1.5, z: 1.5 },
					},
					domain: {
						x: [0, 1],
						y: [0, 1],
					},
				},
				hovermode: 'closest',
				autosize: true,
				showlegend: false,
				margin: { l: 0, r: 100, t: 40, b: 0 },
			}

			const traces = [documentTrace, queryTrace]

			const config = {
				responsive: true,
				displayModeBar: true,
			}

			Plotly.newPlot('viz-plot', traces, layout, config)

			// Register click event handler for result points
			const plotDiv = document.getElementById('viz-plot')
			if (plotDiv) {
				plotDiv.on('plotly_click', this.handlePlotClick)
			}
		},

		updatePlot() {
			// Toggle query point visibility without recreating the plot
			if (this.coordinates.length > 0 && this.queryCoords.length > 0 && this.results.length > 0) {
				const plotDiv = document.getElementById('viz-plot')

				if (plotDiv && plotDiv.data && plotDiv.data.length >= 2) {
					// Trace index 1 is the query point
					Plotly.restyle('viz-plot', { visible: this.showQueryPoint }, [1])
				} else {
					// Plot doesn't exist yet, render it
					this.renderPlot()
				}
			}
		},

		async viewChunk(result) {
			// Guard against concurrent loading
			if (this.viewerLoading) {
				return
			}

			this.showViewer = true
			this.viewerLoading = true
			this.viewerTitle = result.title || 'Chunk Viewer'
			this.currentResult = result // Store result for document linking

			try {
				// Fetch chunk context
				const url = generateUrl('/apps/astrolabe/api/chunk-context')
				const params = {
					doc_type: result.doc_type,
					doc_id: result.id,
					start: result.chunk_start_offset,
					end: result.chunk_end_offset,
				}

				const response = await axios.get(url, { params })

				if (response.data.success) {
					// Determine viewer type and setup
					if (result.doc_type === 'file' && response.data.page_number) {
						this.viewerType = 'pdf'
						this.currentPdfPath = result.metadata?.path || ''
						this.viewerPage = response.data.page_number
					} else {
						this.viewerType = 'text'
						this.viewerContext = {
							chunk: response.data.chunk_text,
							before: response.data.before_context,
							after: response.data.after_context,
						}
					}
				} else {
					console.error('Failed to load chunk:', response.data.error)
					this.closeViewer()
				}
			} catch (err) {
				console.error('Error loading chunk:', err)
				this.closeViewer()
			} finally {
				this.viewerLoading = false
			}
		},

		handlePdfError(error) {
			console.error('PDF viewer error:', error)
			this.viewerType = 'text'
		},

		handlePdfLoaded(event) {
			this.pdfTotalPages = event.totalPages || 0
		},

		getMarkdownContent() {
			// Combine before/chunk/after context into single markdown string
			let content = ''

			if (this.viewerContext.before) {
				content += this.viewerContext.before + '\n\n'
			}

			if (this.viewerContext.chunk) {
				// Highlight the main chunk with a separator
				content += '---\n\n'
				content += this.viewerContext.chunk
				content += '\n\n---'
			}

			if (this.viewerContext.after) {
				content += '\n\n' + this.viewerContext.after
			}

			return content
		},

		closeViewer() {
			this.showViewer = false
			this.pdfTotalPages = 0
			this.currentResult = null
		},

		handlePlotClick(eventData) {
			// Only handle clicks on trace 0 (document results)
			// Trace 1 is the query point - ignore clicks on it
			if (!eventData.points || eventData.points.length === 0) {
				return
			}

			const point = eventData.points[0]
			const traceIndex = point.curveNumber // 0 = documents, 1 = query
			const pointIndex = point.pointNumber // Index in trace data

			// Ignore clicks on query point (trace 1)
			if (traceIndex !== 0) {
				return
			}

			// Access full result object using pointIndex
			// Results array is 1:1 with coordinates array (guaranteed by API)
			const result = this.results[pointIndex]

			if (!result) {
				console.warn('Click handler: result not found for index', pointIndex)
				return
			}

			// Call existing viewChunk method
			this.viewChunk(result)
		},
	},
}
</script>

<style scoped lang="scss">
.mcp-section {
	/* Standard Nextcloud app padding - matches Deck/core spacing */
	padding: 44px 24px 24px var(--default-clickable-area);
	/* Remove max-width to allow content to fill available space like Notes app */
	min-height: calc(100vh - 150px); /* Ensure content extends to bottom of viewport */
}

.mcp-section-header {
	margin-bottom: 24px;

	h2 {
		margin: 0 0 8px 0;
		font-size: 22px;
		font-weight: 600;
	}

	.mcp-description {
		color: var(--color-text-maxcontrast);
		margin: 0;
	}
}

// Search card
.mcp-search-card {
	background: var(--color-background-hover);
	border-radius: var(--border-radius-large);
	padding: 20px;
	margin-bottom: 24px;
}

.mcp-search-row {
	display: flex;
	gap: 12px;
	align-items: flex-end;
	flex-wrap: wrap;
}

.mcp-search-input {
	flex: 1;
	min-width: 250px;
}

.mcp-algorithm-select {
	min-width: 150px;
}

.mcp-advanced-toggle {
	margin-top: 12px;
}

.mcp-advanced-options {
	margin-top: 16px;
	padding-top: 16px;
	border-top: 1px solid var(--color-border);
}

.mcp-advanced-grid {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
	gap: 20px;
}

.mcp-option-group {
	label {
		display: block;
		font-weight: 600;
		margin-bottom: 8px;
		color: var(--color-text-maxcontrast);
	}
}

.mcp-checkbox-grid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 8px;
}

// Loading and error states
.mcp-loading {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 12px;
	padding: 48px;
	color: var(--color-text-maxcontrast);
}

.mcp-error {
	margin: 16px 0;
}

// Visualization
.mcp-viz-container {
	background: var(--color-background-hover);
	border-radius: var(--border-radius-large);
	padding: 16px;
	margin-bottom: 24px;
}

.mcp-viz-header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	margin-bottom: 12px;

	h3 {
		margin: 0;
		font-size: 16px;
		font-weight: 600;
	}
}

.mcp-viz-plot-container {
	width: 100%;
	height: 400px;
	background: var(--color-main-background);
	border-radius: var(--border-radius);
}

#viz-plot {
	width: 100%;
	height: 100%;

	// Pointer cursor for clickable result points (trace 0)
	:deep(.scatterlayer .trace:first-child .point) {
		cursor: pointer !important;
	}

	// Default cursor for query point (trace 1)
	:deep(.scatterlayer .trace:nth-child(2) .point) {
		cursor: default !important;
	}
}

// Results
.mcp-results {
	margin-top: 24px;
}

.mcp-results-header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	margin-bottom: 16px;
	padding-bottom: 12px;
	border-bottom: 1px solid var(--color-border);
	color: var(--color-text-maxcontrast);
}

.mcp-algorithm-badge {
	padding: 4px 10px;
	border-radius: 12px;
	font-size: 12px;
	font-weight: 600;
	text-transform: uppercase;
	background: var(--color-background-dark);
}

.mcp-filter-info {
	font-size: 12px;
	color: var(--color-text-lighter);
	font-weight: normal;
}

.mcp-results-list {
	display: flex;
	flex-direction: column;
	gap: 12px;
}

.mcp-result-item {
	padding: 16px;
	background: var(--color-background-hover);
	border-radius: var(--border-radius-large);
	border-left: 4px solid var(--color-primary-element);
	transition: transform 0.15s, box-shadow 0.15s;

	&:hover {
		transform: translateX(4px);
		box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
	}
}

.mcp-result-header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	margin-bottom: 8px;
}

.mcp-result-type {
	padding: 2px 8px;
	border-radius: 10px;
	font-size: 11px;
	font-weight: 600;
	text-transform: uppercase;
	background: var(--color-primary-element-light);
	color: var(--color-primary-element);
}

// Document type colors
.mcp-doc-type-note {
	border-left-color: #1565c0;
	.mcp-result-type { background: #e3f2fd; color: #1565c0; }
}
.mcp-doc-type-file {
	border-left-color: #2e7d32;
	.mcp-result-type { background: #e8f5e9; color: #2e7d32; }
}
.mcp-doc-type-deck_card {
	border-left-color: #ef6c00;
	.mcp-result-type { background: #fff3e0; color: #ef6c00; }
}
.mcp-doc-type-calendar {
	border-left-color: #c2185b;
	.mcp-result-type { background: #fce4ec; color: #c2185b; }
}
.mcp-doc-type-contact {
	border-left-color: #7b1fa2;
	.mcp-result-type { background: #f3e5f5; color: #7b1fa2; }
}
.mcp-doc-type-news_item {
	border-left-color: #00838f;
	.mcp-result-type { background: #e0f7fa; color: #00838f; }
}

.mcp-result-score {
	font-size: 13px;
	font-weight: 600;
	color: var(--color-text-maxcontrast);
}

.mcp-result-title {
	font-weight: 600;
	font-size: 15px;
	color: var(--color-main-text);
	margin-bottom: 6px;
	line-height: 1.4;
	text-decoration: none;
	display: block;

	&:hover {
		color: var(--color-primary-element);
	}
}

a.mcp-result-title {
	cursor: pointer;
}

.mcp-result-metadata {
	font-size: 12px;
	color: var(--color-text-maxcontrast);
	margin-bottom: 6px;
	line-height: 1.4;
}

.mcp-result-actions {
	display: flex;
	align-items: center;
	gap: 8px;
}

.mcp-external-icon {
	opacity: 0.5;
	margin-left: 4px;
	vertical-align: middle;
}

.mcp-result-title:hover .mcp-external-icon {
	opacity: 1;
}

.mcp-score-slider {
	width: 100%;
	margin-top: 8px;
	accent-color: var(--color-primary-element);
}

// Status section
.mcp-status-cards {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
	gap: 16px;
	margin-bottom: 24px;
}

.mcp-status-card {
	background: var(--color-background-hover);
	border-radius: var(--border-radius-large);
	padding: 20px;
	text-align: center;
}

.mcp-status-label {
	font-size: 13px;
	color: var(--color-text-maxcontrast);
	margin-bottom: 8px;
}

.mcp-status-value {
	font-size: 24px;
	font-weight: 600;
	color: var(--color-main-text);

	&.status-idle { color: var(--color-success); }
	&.status-syncing { color: var(--color-warning); }
	&.status-error { color: var(--color-error); }
}

// Navigation footer
.app-navigation-entry__settings {
	height: auto !important;
	overflow: hidden !important;
	padding-top: 0 !important;
	flex: 0 0 auto;
	padding: 3px;
	margin: 0 3px;
}

// Modal
.mcp-modal-overlay {
	position: fixed;
	top: 0;
	left: 0;
	right: 0;
	bottom: 0;
	background: rgba(0, 0, 0, 0.5);
	display: flex;
	align-items: center;
	justify-content: center;
	z-index: 10000;
}

.mcp-modal {
	background: var(--color-main-background);
	border-radius: var(--border-radius-large);
	width: 90%;
	max-width: 900px;
	height: 80vh;
	display: flex;
	flex-direction: column;
	box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
}

.mcp-modal-header {
	padding: 16px 20px;
	border-bottom: 1px solid var(--color-border);
	display: flex;
	justify-content: space-between;
	align-items: center;

	h3 {
		margin: 0;
		font-size: 18px;
		font-weight: 600;
		flex: 1;
		min-width: 0; // Allow text truncation if needed
	}
}

.mcp-modal-title-link {
	color: var(--color-main-text);
	text-decoration: none;
	display: inline-flex;
	align-items: center;
	gap: 6px;
	transition: color 0.15s;

	&:hover {
		color: var(--color-primary-element);

		.mcp-modal-title-icon {
			opacity: 1;
		}
	}
}

.mcp-modal-title-icon {
	opacity: 0.5;
	transition: opacity 0.15s;
	flex-shrink: 0;
}

.mcp-modal-body {
	flex: 1;
	overflow: auto;
	padding: 20px;
	position: relative;
}

.mcp-modal-footer {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 16px;
	padding: 16px 20px;
	border-top: 1px solid var(--color-border);
	background: var(--color-main-background);
	flex-shrink: 0;

	.mcp-page-info {
		font-size: 14px;
		color: var(--color-text-maxcontrast);
		min-width: 150px;
		text-align: center;
	}
}

.mcp-viewer-loading {
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	height: 100%;
	color: var(--color-text-lighter);
	gap: 16px;
}

.mcp-text-viewer {
	font-family: monospace;
	line-height: 1.6;
	white-space: pre-wrap;
}

.mcp-context-text {
	color: var(--color-text-lighter);
}

.mcp-highlighted-chunk {
	background: #fff9c4;
	color: #000;
	padding: 4px;
	border-radius: 2px;
	font-weight: bold;
}

@media (max-width: 768px) {
	.mcp-search-row {
		flex-direction: column;
		align-items: stretch;
	}

	.mcp-search-input,
	.mcp-algorithm-select {
		min-width: 100%;
	}

	.mcp-checkbox-grid {
		grid-template-columns: 1fr;
	}

	.mcp-modal {
		width: 100%;
		height: 100%;
		border-radius: 0;
	}
}
</style>

<style lang="scss">
/* Fix for double margin/padding issue when nested in #content */
#content-vue {
	margin-top: 0 !important;
	margin-left: 0 !important;
}
</style>
