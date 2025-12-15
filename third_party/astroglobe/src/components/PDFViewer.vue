<template>
	<div class="pdf-viewer">
		<div v-if="loading" class="loading-indicator">
			<NcLoadingIcon :size="64" />
			<p>{{ t('astroglobe', 'Loading PDF...') }}</p>
		</div>
		<div v-else-if="error" class="error-message">
			<AlertCircle :size="48" />
			<p>{{ error }}</p>
		</div>
		<div v-else ref="container" class="pdf-canvas-container">
			<canvas ref="canvas" />
		</div>
		<div v-if="!loading && !error && totalPages > 0" class="pdf-controls">
			<NcButton
				:disabled="pageNumber <= 1"
				@click="$emit('prev-page')">
				<template #icon>
					<ChevronLeft :size="20" />
				</template>
				{{ t('astroglobe', 'Previous') }}
			</NcButton>
			<span class="page-info">
				{{ t('astroglobe', 'Page {current} of {total}', { current: pageNumber, total: totalPages }) }}
			</span>
			<NcButton
				:disabled="pageNumber >= totalPages"
				@click="$emit('next-page')">
				<template #icon>
					<ChevronRight :size="20" />
				</template>
				{{ t('astroglobe', 'Next') }}
			</NcButton>
		</div>
	</div>
</template>

<script>
import * as pdfjsLib from 'pdfjs-dist'
import { generateUrl } from '@nextcloud/router'
import { translate as t } from '@nextcloud/l10n'
import NcLoadingIcon from '@nextcloud/vue/dist/Components/NcLoadingIcon.js'
import NcButton from '@nextcloud/vue/dist/Components/NcButton.js'
import AlertCircle from 'vue-material-design-icons/AlertCircle.vue'
import ChevronLeft from 'vue-material-design-icons/ChevronLeft.vue'
import ChevronRight from 'vue-material-design-icons/ChevronRight.vue'

export default {
	name: 'PDFViewer',
	components: {
		NcLoadingIcon,
		NcButton,
		AlertCircle,
		ChevronLeft,
		ChevronRight,
	},
	props: {
		filePath: {
			type: String,
			required: true,
		},
		pageNumber: {
			type: Number,
			default: 1,
		},
		scale: {
			type: Number,
			default: 1.5,
		},
	},
	data() {
		return {
			pdfDoc: null,
			loading: true,
			error: null,
			totalPages: 0,
		}
	},
	watch: {
		pageNumber(newPage) {
			if (this.pdfDoc && newPage > 0 && newPage <= this.totalPages) {
				this.renderPage(newPage)
			}
		},
		filePath() {
			// Reload PDF if file path changes
			this.loadPDF()
		},
	},
	async mounted() {
		await this.loadPDF()
	},
	beforeUnmount() {
		if (this.pdfDoc) {
			this.pdfDoc.destroy()
		}
	},
	methods: {
		t,
		async loadPDF() {
			this.loading = true
			this.error = null

			try {
				// Clean and encode the file path
				const cleanPath = this.filePath.startsWith('/')
					? this.filePath.substring(1)
					: this.filePath
				const encodedPath = cleanPath.split('/').map(encodeURIComponent).join('/')
				const downloadUrl = generateUrl(`/remote.php/webdav/${encodedPath}`)

				// Load PDF document
				const loadingTask = pdfjsLib.getDocument({
					url: downloadUrl,
					withCredentials: true,
					useWorkerFetch: false, // Disable worker fetch for CSP compliance
					isEvalSupported: false, // Disable eval for CSP
				})

				this.pdfDoc = await loadingTask.promise
				this.totalPages = this.pdfDoc.numPages
				this.$emit('loaded', { totalPages: this.totalPages })

				// Wait for canvas to be in DOM
				await this.$nextTick()

				// Canvas should be available now (mounted lifecycle guarantees it)
				if (!this.$refs.canvas) {
					throw new Error('Canvas element not available after mount')
				}

				// Render the requested page
				await this.renderPage(this.pageNumber)
			} catch (err) {
				console.error('PDF load error:', err)

				// Provide user-friendly error messages
				if (err.name === 'MissingPDFException') {
					this.error = t('astroglobe', 'PDF file not found')
				} else if (err.name === 'InvalidPDFException') {
					this.error = t('astroglobe', 'Invalid or corrupted PDF file')
				} else if (err.message?.includes('NetworkError') || err.message?.includes('Network')) {
					this.error = t('astroglobe', 'Network error loading PDF')
				} else if (err.message?.includes('404')) {
					this.error = t('astroglobe', 'PDF file not found')
				} else {
					this.error = t('astroglobe', 'Unable to load PDF file')
				}

				this.$emit('error', err)
			} finally {
				this.loading = false
			}
		},
		async renderPage(pageNum) {
			if (!this.pdfDoc) {
				return
			}

			try {
				const page = await this.pdfDoc.getPage(pageNum)
				const canvas = this.$refs.canvas

				if (!canvas) {
					console.error('PDF canvas ref not found')
					this.error = t('astroglobe', 'Canvas element not available')
					return
				}

				const context = canvas.getContext('2d')

				// Use scale for better resolution on high-DPI screens
				const viewport = page.getViewport({ scale: this.scale })

				canvas.height = viewport.height
				canvas.width = viewport.width

				// Render page to canvas
				const renderContext = {
					canvasContext: context,
					viewport,
				}

				await page.render(renderContext).promise

				this.$emit('page-rendered', { pageNumber: pageNum })
			} catch (err) {
				console.error('PDF render error:', err)
				this.error = t('astroglobe', 'Error rendering PDF page')
				this.$emit('error', err)
			}
		},
	},
}
</script>

<style scoped lang="scss">
.pdf-viewer {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 16px;
	padding: 16px;
}

.loading-indicator {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 16px;
	padding: 48px;

	p {
		color: var(--color-text-maxcontrast);
		font-size: 14px;
	}
}

.error-message {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 16px;
	padding: 48px;
	color: var(--color-error);

	p {
		font-size: 14px;
		text-align: center;
	}
}

.pdf-canvas-container {
	position: relative;
	border: 1px solid var(--color-border);
	box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
	background: var(--color-main-background);
	max-width: 100%;
	overflow: auto;

	canvas {
		display: block;
		max-width: 100%;
		height: auto;
	}
}

.pdf-controls {
	display: flex;
	align-items: center;
	gap: 16px;
	padding: 8px;
	background: var(--color-background-dark);
	border-radius: var(--border-radius-large);

	.page-info {
		font-size: 14px;
		color: var(--color-text-maxcontrast);
		min-width: 120px;
		text-align: center;
	}
}

@media (max-width: 768px) {
	.pdf-viewer {
		padding: 8px;
	}

	.pdf-controls {
		flex-direction: column;
		gap: 8px;

		.page-info {
			order: -1;
		}
	}
}
</style>
