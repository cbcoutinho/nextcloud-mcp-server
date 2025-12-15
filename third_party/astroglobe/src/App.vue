<template>
	<NcContent app-name="astroglobe">
		<NcAppNavigation>
			<template #list>
				<NcAppNavigationItem
					:name="t('astroglobe', 'Semantic Search')"
					:active="activeSection === 'search'"
					@click="activeSection = 'search'">
					<template #icon>
						<Magnify :size="20" />
					</template>
				</NcAppNavigationItem>

				<NcAppNavigationItem
					:name="t('astroglobe', 'Index Status')"
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
						:name="t('astroglobe', 'Settings')"
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
					<h2>{{ t('astroglobe', 'Semantic Search') }}</h2>
					<p class="mcp-description">
						{{ t('astroglobe', 'Search your indexed content using semantic similarity. Find documents by meaning, not just keywords.') }}
					</p>
				</div>

				<!-- Search Controls -->
				<div class="mcp-search-card">
					<div class="mcp-search-row">
						<NcTextField
							:value.sync="query"
							:label="t('astroglobe', 'Search query')"
							:placeholder="t('astroglobe', 'Enter your search query...')"
							class="mcp-search-input"
							@keyup.enter="performSearch" />

						<NcSelect
							v-model="selectedAlgorithmOption"
							:options="algorithmOptions"
							:placeholder="t('astroglobe', 'Algorithm')"
							class="mcp-algorithm-select"
							@input="algorithm = $event ? $event.id : 'hybrid'" />

						<NcButton
							type="primary"
							:disabled="!query.trim() || loading"
							@click="performSearch">
							<template #icon>
								<Magnify :size="20" />
							</template>
							{{ t('astroglobe', 'Search') }}
						</NcButton>
					</div>

					<!-- Advanced Options Toggle -->
					<NcButton
						type="tertiary"
						class="mcp-advanced-toggle"
						@click="showAdvanced = !showAdvanced">
						<template #icon>
							<ChevronDown v-if="!showAdvanced" :size="20" />
							<ChevronUp v-else :size="20" />
						</template>
						{{ showAdvanced ? t('astroglobe', 'Hide advanced') : t('astroglobe', 'Advanced options') }}
					</NcButton>

					<!-- Advanced Options -->
					<div v-show="showAdvanced" class="mcp-advanced-options">
						<div class="mcp-advanced-grid">
							<div class="mcp-option-group">
								<label>{{ t('astroglobe', 'Document Types') }}</label>
								<div class="mcp-checkbox-grid">
									<NcCheckboxRadioSwitch
										v-for="docType in docTypeOptions"
										:key="docType.id"
										:checked.sync="selectedDocTypes"
										:value="docType.id"
										type="checkbox">
										{{ docType.label }}
									</NcCheckboxRadioSwitch>
								</div>
							</div>

							<div class="mcp-option-group">
								<label>{{ t('astroglobe', 'Result Limit') }}</label>
								<NcTextField
									:value.sync="limit"
									type="number"
									:min="1"
									:max="100" />
							</div>

							<div class="mcp-option-group">
								<label>{{ t('astroglobe', 'Minimum Score') }}: {{ scoreThreshold }}%</label>
								<input
									v-model="scoreThreshold"
									type="range"
									min="0"
									max="100"
									step="5"
									class="mcp-score-slider" />
							</div>
						</div>
					</div>
				</div>

				<!-- Loading State -->
				<div v-if="loading" class="mcp-loading">
					<NcLoadingIcon :size="32" />
					<span>{{ t('astroglobe', 'Searching...') }}</span>
				</div>

				<!-- Error State -->
				<NcNoteCard v-if="error" type="error" class="mcp-error">
					{{ error }}
				</NcNoteCard>

				<!-- Results -->
				<div v-if="results.length > 0 && !loading" class="mcp-results">
					<div class="mcp-results-header">
						<span>
							{{ filteredResults.length }} {{ t('astroglobe', 'results') }}
							<span v-if="filteredResults.length !== results.length" class="mcp-filter-info">
								({{ results.length - filteredResults.length }} {{ t('astroglobe', 'filtered by score') }})
							</span>
						</span>
						<span class="mcp-algorithm-badge">{{ algorithmUsed }}</span>
					</div>

					<!-- 3D Visualization -->
					<div v-if="coordinates.length > 0" class="mcp-viz-container">
						<div class="mcp-viz-header">
							<h3>{{ t('astroglobe', 'Vector Space Visualization') }}</h3>
							<NcCheckboxRadioSwitch
								:checked.sync="showQueryPoint"
								type="switch"
								@update:checked="updatePlot">
								{{ t('astroglobe', 'Show query point') }}
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
										type="tertiary"
										:aria-label="t('astroglobe', 'Show Chunk')"
										@click="viewChunk(result)">
										<template #icon>
											<Eye :size="18" />
										</template>
										{{ t('astroglobe', 'Show Chunk') }}
									</NcButton>
									<span class="mcp-result-score">{{ formatScore(result.score) }}%</span>
								</div>
							</div>
							<a
								:href="getDocumentUrl(result)"
								class="mcp-result-title"
								@click.prevent="navigateToDocument(result)">
								{{ result.title || t('astroglobe', 'Untitled') }}
								<OpenInNew :size="14" class="mcp-external-icon" />
							</a>
							<div class="mcp-result-metadata">
								<span v-if="result.chunk_index !== undefined && result.total_chunks">
									{{ t('astroglobe', 'Chunk {chunk}/{total}', { chunk: result.chunk_index + 1, total: result.total_chunks }) }}
								</span>
								<span v-if="result.page_number && result.page_count" class="mcp-metadata-separator">
									· {{ t('astroglobe', 'Page {page}/{total}', { page: result.page_number, total: result.page_count }) }}
								</span>
							</div>
							<div
								class="mcp-result-excerpt">
								{{ result.excerpt }}
							</div>
						</div>
					</div>
				</div>

				<!-- No Results -->
				<NcEmptyContent
					v-if="searched && results.length === 0 && !loading && !error"
					:name="t('astroglobe', 'No results found')"
					:description="t('astroglobe', 'Try a different query or search algorithm.')">
					<template #icon>
						<Magnify />
					</template>
				</NcEmptyContent>

				<!-- Initial State -->
				<NcEmptyContent
					v-if="!searched && !loading"
					:name="t('astroglobe', 'Semantic Search')"
					:description="t('astroglobe', 'Enter a query above to search your indexed content.')">
					<template #icon>
						<Magnify />
					</template>
				</NcEmptyContent>
			</div>

			<!-- Index Status Section -->
			<div v-show="activeSection === 'status'" class="mcp-section">
				<div class="mcp-section-header">
					<h2>{{ t('astroglobe', 'Index Status') }}</h2>
					<p class="mcp-description">
						{{ t('astroglobe', 'View the status of your vector index and sync progress.') }}
					</p>
				</div>

				<div v-if="statusLoading" class="mcp-loading">
					<NcLoadingIcon :size="32" />
					<span>{{ t('astroglobe', 'Loading status...') }}</span>
				</div>

				<NcNoteCard v-else-if="statusError" type="error">
					{{ statusError }}
				</NcNoteCard>

				<div v-else-if="vectorStatus" class="mcp-status-cards">
					<div class="mcp-status-card">
						<div class="mcp-status-label">{{ t('astroglobe', 'Sync Status') }}</div>
						<div class="mcp-status-value" :class="'status-' + vectorStatus.status">
							{{ vectorStatus.status }}
						</div>
					</div>

					<div class="mcp-status-card">
						<div class="mcp-status-label">{{ t('astroglobe', 'Indexed Documents') }}</div>
						<div class="mcp-status-value">{{ vectorStatus.indexed_documents || 0 }}</div>
					</div>

					<div class="mcp-status-card">
						<div class="mcp-status-label">{{ t('astroglobe', 'Pending Documents') }}</div>
						<div class="mcp-status-value">{{ vectorStatus.pending_documents || 0 }}</div>
					</div>

					<div v-if="vectorStatus.last_sync_time" class="mcp-status-card">
						<div class="mcp-status-label">{{ t('astroglobe', 'Last Sync') }}</div>
						<div class="mcp-status-value">{{ vectorStatus.last_sync_time }}</div>
					</div>
				</div>

				<NcButton type="secondary" @click="loadVectorStatus" :disabled="statusLoading">
					<template #icon>
						<Refresh :size="20" />
					</template>
					{{ t('astroglobe', 'Refresh') }}
				</NcButton>
			</div>
		</NcAppContent>

		<!-- PDF/Chunk Viewer Modal -->
		<div v-if="showViewer" class="mcp-modal-overlay" @click.self="closeViewer">
			<div class="mcp-modal">
				<div class="mcp-modal-header">
					<h3>{{ viewerTitle }}</h3>
					<NcButton type="tertiary" @click="closeViewer">
						<template #icon>
							<Close :size="20" />
						</template>
					</NcButton>
				</div>
				<div class="mcp-modal-body">
					<!-- Loading State -->
					<div v-if="viewerLoading" class="mcp-viewer-loading">
						<NcLoadingIcon :size="32" />
						<span>{{ t('astroglobe', 'Loading content...') }}</span>
					</div>

					<!-- PDF Viewer -->
					<PDFViewer
						v-else-if="viewerType === 'pdf'"
						:file-path="currentPdfPath"
						:page-number="viewerPage"
						@prev-page="viewerPage--"
						@next-page="viewerPage++"
						@error="handlePdfError" />

					<!-- Text Viewer (for non-PDFs) -->
					<div v-else class="mcp-text-viewer">
						<div v-if="viewerContext.before" class="mcp-context-text">{{ viewerContext.before }}</div>
						<div class="mcp-highlighted-chunk">{{ viewerContext.chunk }}</div>
						<div v-if="viewerContext.after" class="mcp-context-text">{{ viewerContext.after }}</div>
					</div>
				</div>
			</div>
		</div>
	</NcContent>
</template>

<script>
import NcContent from '@nextcloud/vue/dist/Components/NcContent.js'
import NcAppNavigation from '@nextcloud/vue/dist/Components/NcAppNavigation.js'
import NcAppNavigationItem from '@nextcloud/vue/dist/Components/NcAppNavigationItem.js'
import NcAppContent from '@nextcloud/vue/dist/Components/NcAppContent.js'
import NcButton from '@nextcloud/vue/dist/Components/NcButton.js'
import NcTextField from '@nextcloud/vue/dist/Components/NcTextField.js'
import NcSelect from '@nextcloud/vue/dist/Components/NcSelect.js'
import NcLoadingIcon from '@nextcloud/vue/dist/Components/NcLoadingIcon.js'
import NcNoteCard from '@nextcloud/vue/dist/Components/NcNoteCard.js'
import NcEmptyContent from '@nextcloud/vue/dist/Components/NcEmptyContent.js'
import NcCheckboxRadioSwitch from '@nextcloud/vue/dist/Components/NcCheckboxRadioSwitch.js'

import Magnify from 'vue-material-design-icons/Magnify.vue'
import ChartBox from 'vue-material-design-icons/ChartBox.vue'
import Cog from 'vue-material-design-icons/Cog.vue'
import ChevronDown from 'vue-material-design-icons/ChevronDown.vue'
import ChevronUp from 'vue-material-design-icons/ChevronUp.vue'
import Refresh from 'vue-material-design-icons/Refresh.vue'
import TextBoxOutline from 'vue-material-design-icons/TextBoxOutline.vue'
import TextBoxRemoveOutline from 'vue-material-design-icons/TextBoxRemoveOutline.vue'
import OpenInNew from 'vue-material-design-icons/OpenInNew.vue'
import Eye from 'vue-material-design-icons/Eye.vue'
import Close from 'vue-material-design-icons/Close.vue'

import PDFViewer from './components/PDFViewer.vue'

import axios from '@nextcloud/axios'
import { generateUrl } from '@nextcloud/router'
import Plotly from 'plotly.js-dist-min'
import * as pdfjsLib from 'pdfjs-dist'

// Set worker source with error handling
try {
	pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
		'pdfjs-dist/build/pdf.worker.mjs',
		import.meta.url
	).toString()
} catch (e) {
	console.warn('Failed to set PDF.js worker, will use fallback', e)
	// PDF.js will use fake worker automatically
}

// App name for translations
const APP_NAME = 'astroglobe'

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
		Magnify,
		ChartBox,
		Cog,
		ChevronDown,
		ChevronUp,
		Refresh,
		TextBoxOutline,
		TextBoxRemoveOutline,
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
			limit: '20',
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
			currentPdfPath: '',
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
				{ id: 'hybrid', label: this.t('astroglobe', 'Hybrid') },
				{ id: 'semantic', label: this.t('astroglobe', 'Semantic') },
				{ id: 'bm25', label: this.t('astroglobe', 'Keyword (BM25)') },
			]
		},
		docTypeOptions() {
			return [
				{ id: 'note', label: this.t('astroglobe', 'Notes') },
				{ id: 'file', label: this.t('astroglobe', 'Files') },
				{ id: 'deck_card', label: this.t('astroglobe', 'Deck Cards') },
				{ id: 'calendar', label: this.t('astroglobe', 'Calendar') },
				{ id: 'contact', label: this.t('astroglobe', 'Contacts') },
				{ id: 'news_item', label: this.t('astroglobe', 'News') },
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
	methods: {
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
				const url = generateUrl('/apps/astroglobe/api/search')
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
					this.error = response.data.error || this.t('astroglobe', 'Search failed')
					this.results = []
				}
			} catch (err) {
				console.error('Search error:', err)
				this.error = this.t('astroglobe', 'Network error. Please try again.')
				this.results = []
			} finally {
				this.loading = false
			}
		},

		async loadVectorStatus() {
			this.statusLoading = true
			this.statusError = null

			try {
				const url = generateUrl('/apps/astroglobe/api/vector-status')
				const response = await axios.get(url)

				if (response.data.success) {
					this.vectorStatus = response.data.status
				} else {
					this.statusError = response.data.error || this.t('astroglobe', 'Failed to load status')
				}
			} catch (err) {
				console.error('Status error:', err)
				this.statusError = this.t('astroglobe', 'Network error. Please try again.')
			} finally {
				this.statusLoading = false
			}
		},

		formatScore(score) {
			return Math.round((score || 0) * 100)
		},

		toggleExcerpt(index) {
			this.$set(this.expandedExcerpts, index, !this.expandedExcerpts[index])
		},

		truncateExcerpt(text, maxLength = 150) {
			if (!text || text.length <= maxLength) return text
			return text.substring(0, maxLength).trim() + '...'
		},

		getDocumentUrl(result) {
			const docType = result.doc_type || 'unknown'
			const id = result.id || result.note_id

			switch (docType) {
			case 'note':
				return generateUrl(`/apps/notes/#/note/${id}`)
			case 'file':
				if (id) {
					return generateUrl(`/apps/files/files/${id}?dir=/&editing=false&openfile=true`)
				}
				return generateUrl('/apps/files/')
			case 'deck_card':
				if (result.board_id && id) {
					return generateUrl(`/apps/deck/board/${result.board_id}/card/${id}`)
				}
				return generateUrl('/apps/deck/')
			case 'calendar':
			case 'calendar_event':
				return generateUrl('/apps/calendar/')
			case 'news_item':
				return generateUrl('/apps/news/')
			case 'contact':
				return generateUrl('/apps/contacts/')
			default:
				return generateUrl('/apps/astroglobe/')
			}
		},

		navigateToDocument(result) {
			const url = this.getDocumentUrl(result)
			window.open(url, '_blank')
		},

		goToSettings() {
			window.location.href = generateUrl('/settings/user/astroglobe')
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
				marker: {
					size: results.map(r => 4 + (Math.pow(r.score, 2) * 10)),
					opacity: results.map(r => 0.3 + (r.score * 0.7)),
					color: scores,
					colorscale: 'Viridis',
					showscale: true,
					colorbar: {
						title: 'Relative Score',
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
				title: `Vector Space (PCA 3D) - ${results.length} results`,
				width,
				height,
				scene: {
					xaxis: { title: 'PC1' },
					yaxis: { title: 'PC2' },
					zaxis: { title: 'PC3' },
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
			this.showViewer = true
			this.viewerLoading = true
			this.viewerTitle = result.title || 'Chunk Viewer'

			try {
				// Fetch chunk context
				const url = generateUrl('/apps/astroglobe/api/chunk-context')
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

		closeViewer() {
			this.showViewer = false
		}
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

.mcp-result-excerpt {
	font-size: 13px;
	color: var(--color-text-maxcontrast);
	line-height: 1.5;
	white-space: pre-wrap;
	word-wrap: break-word;

	&--expanded {
		display: block;
		-webkit-line-clamp: unset;
		background: var(--color-background-dark);
		padding: 12px;
		border-radius: var(--border-radius);
		margin-top: 8px;
		white-space: pre-wrap;
		word-break: break-word;
	}
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
	}
}

.mcp-modal-body {
	flex: 1;
	overflow: auto;
	padding: 20px;
	position: relative;
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
