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
						<span>{{ results.length }} {{ t('astroglobe', 'results found') }}</span>
						<span class="mcp-algorithm-badge">{{ algorithmUsed }}</span>
					</div>

					<div class="mcp-results-list">
						<div
							v-for="result in results"
							:key="result.id"
							class="mcp-result-item"
							:class="'mcp-doc-type-' + (result.doc_type || 'unknown')">
							<div class="mcp-result-header">
								<span class="mcp-result-type">{{ result.doc_type || 'unknown' }}</span>
								<span class="mcp-result-score">{{ formatScore(result.score) }}%</span>
							</div>
							<a v-if="result.link" :href="result.link" target="_blank" class="mcp-result-title">
								{{ result.title || t('astroglobe', 'Untitled') }}
							</a>
							<div v-else class="mcp-result-title">
								{{ result.title || t('astroglobe', 'Untitled') }}
							</div>
							<div class="mcp-result-excerpt">{{ result.excerpt }}</div>
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

import axios from '@nextcloud/axios'
import { generateUrl } from '@nextcloud/router'

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
		Magnify,
		ChartBox,
		Cog,
		ChevronDown,
		ChevronUp,
		Refresh,
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
			loading: false,
			error: null,
			results: [],
			algorithmUsed: '',
			searched: false,
			// Vector status state
			vectorStatus: null,
			statusLoading: false,
			statusError: null,
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

			try {
				const url = generateUrl('/apps/astroglobe/api/search')
				const params = {
					query: queryText,
					algorithm: this.algorithm,
					limit: parseInt(this.limit) || 20,
				}

				if (this.selectedDocTypes.length > 0) {
					params.doc_types = this.selectedDocTypes.join(',')
				}

				const response = await axios.get(url, { params })

				if (response.data.success) {
					this.results = response.data.results || []
					this.algorithmUsed = response.data.algorithm_used || this.algorithm
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

		goToSettings() {
			window.location.href = generateUrl('/settings/user/mcp')
		},
	},
}
</script>

<style scoped lang="scss">
.mcp-section {
	padding: 24px;
	max-width: 1000px;
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

.mcp-result-excerpt {
	font-size: 13px;
	color: var(--color-text-maxcontrast);
	line-height: 1.5;
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	overflow: hidden;
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
}
</style>
