<template>
	<div class="pdf-viewer">
		<div v-if="loading" class="loading-indicator">
			<NcLoadingIcon :size="64" />
			<p>{{ t('astrolabe', 'Loading PDF...') }}</p>
		</div>
		<div v-else-if="error" class="error-message">
			<AlertCircle :size="48" />
			<p>{{ error }}</p>
		</div>
		<div v-else ref="container" class="pdf-canvas-container">
			<canvas ref="canvas" />
		</div>
	</div>
</template>

<script>
import * as pdfjsLib from 'pdfjs-dist'
import { generateUrl } from '@nextcloud/router'
import { translate as t } from '@nextcloud/l10n'
import NcLoadingIcon from '@nextcloud/vue/components/NcLoadingIcon'
import AlertCircle from 'vue-material-design-icons/AlertCircle.vue'

export default {
	name: 'PDFViewer',
	components: {
		NcLoadingIcon,
		AlertCircle,
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
		async loading(newLoading) {
			// When loading completes, wait for canvas to be available and render
			if (!newLoading && this.pdfDoc && !this.error) {
				// Wait for Vue to update DOM
				await this.$nextTick()
				// Canvas should now be rendered (v-else condition)
				if (this.$refs.canvas) {
					await this.renderPage(this.pageNumber)
				}
			}
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

				// Set loading to false - the watcher will handle rendering
				this.loading = false
			} catch (err) {
				console.error('PDF load error:', err)

				// Provide user-friendly error messages
				if (err.name === 'MissingPDFException') {
					this.error = t('astrolabe', 'PDF file not found')
				} else if (err.name === 'InvalidPDFException') {
					this.error = t('astrolabe', 'Invalid or corrupted PDF file')
				} else if (err.message?.includes('NetworkError') || err.message?.includes('Network')) {
					this.error = t('astrolabe', 'Network error loading PDF')
				} else if (err.message?.includes('404')) {
					this.error = t('astrolabe', 'PDF file not found')
				} else {
					this.error = t('astrolabe', 'Unable to load PDF file')
				}

				this.$emit('error', err)
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
					this.error = t('astrolabe', 'Canvas element not available')
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
				this.error = t('astrolabe', 'Error rendering PDF page')
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

@media (max-width: 768px) {
	.pdf-viewer {
		padding: 8px;
	}
}
</style>
