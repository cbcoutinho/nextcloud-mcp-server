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
		<div v-else ref="containerRef" class="pdf-canvas-container">
			<canvas ref="canvasRef" />
		</div>
	</div>
</template>

<script setup>
import { ref, shallowRef, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { generateUrl } from '@nextcloud/router'
import { translate as t } from '@nextcloud/l10n'
import { NcLoadingIcon } from '@nextcloud/vue'

// Use global pdfjsLib loaded by pdfjs-loader.mjs (external, not bundled)
// This avoids Vite transforming ES private fields which breaks fake worker compatibility
const pdfjsLib = window.pdfjsLib
import AlertCircle from 'vue-material-design-icons/AlertCircle.vue'

const props = defineProps({
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
})

const emit = defineEmits(['loaded', 'error', 'page-rendered'])

// Reactive state
// Use shallowRef for pdfDoc because PDFDocumentProxy uses ES private fields
// which can't be accessed through Vue's Proxy wrapper
const pdfDoc = shallowRef(null)
const loading = ref(true)
const error = ref(null)
const totalPages = ref(0)
const canvasRef = ref(null)
const containerRef = ref(null)

// Methods
async function loadPDF() {
	loading.value = true
	error.value = null

	try {
		// Clean and encode the file path
		const cleanPath = props.filePath.startsWith('/')
			? props.filePath.substring(1)
			: props.filePath
		const encodedPath = cleanPath.split('/').map(encodeURIComponent).join('/')
		const downloadUrl = generateUrl(`/remote.php/webdav/${encodedPath}`)

		// Set worker source using OC.linkTo for correct app webroot path
		// Must be done here (not at module load time) because _oc_appswebroots isn't populated until after page load
		pdfjsLib.GlobalWorkerOptions.workerSrc = window.OC.linkTo('astrolabe', 'js/pdf.worker.mjs')

		// Load PDF document
		const loadingTask = pdfjsLib.getDocument({
			url: downloadUrl,
			withCredentials: true,
			useWorkerFetch: false, // Disable worker fetch for CSP compliance
			isEvalSupported: false, // Disable eval for CSP
		})

		pdfDoc.value = await loadingTask.promise
		totalPages.value = pdfDoc.value.numPages
		emit('loaded', { totalPages: totalPages.value })

		// Set loading to false - the watcher will handle rendering
		loading.value = false
	} catch (err) {
		console.error('PDF load error:', err)

		// Provide user-friendly error messages
		if (err.name === 'MissingPDFException') {
			error.value = t('astrolabe', 'PDF file not found')
		} else if (err.name === 'InvalidPDFException') {
			error.value = t('astrolabe', 'Invalid or corrupted PDF file')
		} else if (err.message?.includes('NetworkError') || err.message?.includes('Network')) {
			error.value = t('astrolabe', 'Network error loading PDF')
		} else if (err.message?.includes('404')) {
			error.value = t('astrolabe', 'PDF file not found')
		} else {
			error.value = t('astrolabe', 'Unable to load PDF file')
		}

		emit('error', err)
		loading.value = false
	}
}

async function renderPage(pageNum) {
	if (!pdfDoc.value) {
		return
	}

	try {
		const page = await pdfDoc.value.getPage(pageNum)
		const canvas = canvasRef.value

		if (!canvas) {
			console.error('PDF canvas ref not found')
			error.value = t('astrolabe', 'Canvas element not available')
			return
		}

		const context = canvas.getContext('2d')

		// Use scale for better resolution on high-DPI screens
		const viewport = page.getViewport({ scale: props.scale })

		canvas.height = viewport.height
		canvas.width = viewport.width

		// Render page to canvas
		const renderContext = {
			canvasContext: context,
			viewport,
		}

		await page.render(renderContext).promise

		emit('page-rendered', { pageNumber: pageNum })
	} catch (err) {
		console.error('PDF render error:', err)
		error.value = t('astrolabe', 'Error rendering PDF page')
		emit('error', err)
	}
}

// Watchers
watch(() => props.pageNumber, (newPage) => {
	if (pdfDoc.value && newPage > 0 && newPage <= totalPages.value) {
		renderPage(newPage)
	}
})

watch(() => props.filePath, () => {
	// Reload PDF if file path changes
	loadPDF()
})

watch(loading, async (newLoading) => {
	// When loading completes, wait for canvas to be available and render
	if (!newLoading && pdfDoc.value && !error.value) {
		// Wait for Vue to update DOM
		await nextTick()
		// Canvas should now be rendered (v-else condition)
		if (canvasRef.value) {
			await renderPage(props.pageNumber)
		}
	}
})

// Lifecycle hooks
onMounted(() => {
	loadPDF()
})

onBeforeUnmount(() => {
	if (pdfDoc.value) {
		pdfDoc.value.destroy()
	}
})
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
