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
		<div v-else class="pdf-image-container">
			<img
				:src="`data:image/png;base64,${imageData}`"
				class="pdf-page-image"
				alt="PDF page" />
		</div>
	</div>
</template>

<script setup>
/**
 * PDFViewer - Server-side PDF rendering component.
 *
 * Displays PDF pages as server-rendered PNG images, avoiding client-side
 * PDF.js issues with CSP worker restrictions and ES private field access
 * in Chromium browsers.
 *
 * The server uses PyMuPDF to render PDF pages to PNG images, which are
 * returned as base64-encoded data.
 */
import { ref, watch, onMounted } from 'vue'
import axios from '@nextcloud/axios'
import { generateUrl } from '@nextcloud/router'
import { translate as t } from '@nextcloud/l10n'
import { NcLoadingIcon } from '@nextcloud/vue'
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
		default: 2.0,
	},
})

const emit = defineEmits(['loaded', 'error', 'page-rendered'])

// Reactive state
const loading = ref(true)
const error = ref(null)
const imageData = ref(null)
const totalPages = ref(0)

/**
 * Fetch a PDF page from the server as a PNG image.
 */
async function loadPage() {
	loading.value = true
	error.value = null

	try {
		// Build request URL
		const url = generateUrl('/apps/astrolabe/api/pdf-preview')
		const params = {
			file_path: props.filePath,
			page: props.pageNumber,
			scale: props.scale,
		}

		const response = await axios.get(url, { params })

		if (!response.data.success) {
			throw new Error(response.data.error || 'Failed to load PDF page')
		}

		const data = response.data

		// Update state
		imageData.value = data.image
		totalPages.value = data.total_pages

		// Emit loaded event - App.vue uses this for navigation controls
		emit('loaded', { totalPages: data.total_pages })
		emit('page-rendered', { pageNumber: props.pageNumber })

		loading.value = false
	} catch (err) {
		console.error('PDF load error:', err)

		// Provide user-friendly error messages based on axios error structure
		const status = err.response?.status
		const serverError = err.response?.data?.error

		if (status === 404) {
			error.value = t('astrolabe', 'PDF file not found')
		} else if (status === 401 || status === 403) {
			error.value = serverError || t('astrolabe', 'Authorization required to view PDF')
		} else if (err.code === 'ERR_NETWORK' || err.message?.includes('Network')) {
			error.value = t('astrolabe', 'Network error loading PDF')
		} else if (serverError) {
			error.value = serverError
		} else {
			error.value = t('astrolabe', 'Unable to load PDF page')
		}

		emit('error', err)
		loading.value = false
	}
}

// Re-fetch when file path or page number changes
watch(() => [props.filePath, props.pageNumber], loadPage)

// Initial load
onMounted(() => {
	loadPage()
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

.pdf-image-container {
	position: relative;
	border: 1px solid var(--color-border);
	box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
	background: var(--color-main-background);
	max-width: 100%;
	overflow: auto;
}

.pdf-page-image {
	display: block;
	max-width: 100%;
	height: auto;
}

@media (max-width: 768px) {
	.pdf-viewer {
		padding: 8px;
	}
}
</style>
